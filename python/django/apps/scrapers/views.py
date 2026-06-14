import asyncio
import os
import queue
import threading
from contextlib import redirect_stdout

from django.conf import settings
from django.db.models import F, ExpressionWrapper, FloatField
from django.http import StreamingHttpResponse, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_GET

from apps.scrapers.models import Cupom, Produto, ConfiguracaoEnvio
from apps.scrapers.scraper_mercadolivre.scraper import main as scrapper_main


class _QueueWriter:
    """File-like object that feeds lines into a Queue for SSE streaming."""

    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._q.put(line)

    def flush(self):
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


def dashboard(request):
    return render(request, "scrapers/dashboard.html")


def whatsapp_painel(request):
    """Tela de conexão do WhatsApp: status + QR Code para parear pelo navegador."""
    from apps.scrapers import whatsapp_client
    return render(request, "scrapers/whatsapp.html", {
        "status": whatsapp_client.status(),
    })


@require_GET
def whatsapp_status_json(request):
    """JSON de status para polling do front."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.status())


@require_GET
def whatsapp_refresh_grupos(request):
    """Força re-sincronização da lista de grupos no Node e devolve o resultado."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.refresh_grupos())


@require_GET
def whatsapp_grupos_json(request):
    """Lista grupos (GET leve) para o front carregar via AJAX sem travar o render."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.listar_grupos())


@require_GET
def whatsapp_qr_png(request):
    """Renderiza o QR do WhatsApp como PNG (vindo do serviço Node)."""
    import qrcode
    from io import BytesIO
    from apps.scrapers import whatsapp_client

    info = whatsapp_client.qrcode()
    qr = info.get("qr")
    if not qr:
        # 204 = sem QR (já conectado ou ainda gerando)
        return HttpResponse(status=204)
    buf = BytesIO()
    qrcode.make(qr).save(buf, format="PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


def telegram_painel(request):
    """Tela de conexão do Telegram: status do bot + checklist de setup."""
    return render(request, "scrapers/telegram.html")


@require_GET
def telegram_status_json(request):
    """Verifica o bot via getMe. Sem chamadas de browser — só HTTP."""
    import requests as _rq
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        return JsonResponse({"token": False, "ok": False})
    try:
        r = _rq.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        d = r.json()
        if d.get("ok"):
            info = d.get("result", {})
            return JsonResponse({"token": True, "ok": True,
                                 "username": info.get("username"), "nome": info.get("first_name")})
        return JsonResponse({"token": True, "ok": False, "erro": d.get("description") or "getMe falhou"})
    except Exception as e:
        return JsonResponse({"token": True, "ok": False, "erro": str(e)})


def configuracoes(request):
    """Painel do afiliado: cria/edita/remove regras de divulgação (nicho→grupo→intervalo)."""
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "delete":
            # Só apaga regra do próprio usuário (isolamento multi-tenant).
            ConfiguracaoEnvio.objects.filter(
                id=request.POST.get("id"), owner=request.user).delete()
        else:
            cfg_id = request.POST.get("id")
            # Sub-nichos: multi-select -> junta as strings de termos (OR no filtro)
            termos = [t.strip() for t in request.POST.getlist("termo_busca") if t.strip()]
            canal = (request.POST.get("canal") or "whatsapp").strip()
            # Telegram usa o campo de chat_id digitado; WhatsApp usa o grupo escolhido.
            grupo_id = (request.POST.get("telegram_chat_id") if canal == "telegram"
                        else request.POST.get("grupo_id")) or ""
            campos = dict(
                macro_categoria=request.POST.get("macro_categoria", "").strip(),
                termo_busca=", ".join(termos),
                canal=canal,
                marketplace=(request.POST.get("marketplace") or "").strip(),
                grupo_id=grupo_id.strip(),
                grupo_nome=request.POST.get("grupo_nome", "").strip(),
                intervalo_minutos=int(request.POST.get("intervalo_minutos") or 60),
                janela_inicio=int(request.POST.get("janela_inicio") or 8),
                janela_fim=int(request.POST.get("janela_fim") or 20),
                min_desconto_percent=float(request.POST.get("min_desconto_percent") or 15),
                ativo=bool(request.POST.get("ativo")),
            )
            if cfg_id:
                # update() não dispara validação, mas o filtro por owner garante posse.
                ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).update(**campos)
            else:
                ConfiguracaoEnvio.objects.create(owner=request.user, **campos)
        return redirect("scraper-configuracoes")

    macros = list(
        Produto.objects
        .exclude(macro_categoria__isnull=True).exclude(macro_categoria="")
        .values_list("macro_categoria", flat=True).distinct().order_by("macro_categoria")
    )
    # Grupos do WhatsApp NÃO são buscados aqui: a chamada ao Node pode travar o render
    # (até 15s) quando o serviço está offline. Carregados via AJAX (ver whatsapp_grupos_json).

    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import SUBNICHOS
    subnichos = [{"macro": m, "itens": [{"label": l, "termos": t} for l, t in itens]}
                 for m, itens in SUBNICHOS.items()]

    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.senders.registry import SENDERS

    return render(request, "scrapers/configuracoes.html", {
        "configs": ConfiguracaoEnvio.objects.filter(owner=request.user).order_by("macro_categoria"),
        "macros": macros,
        "subnichos": subnichos,
        "marketplaces": list(MARKETPLACES.keys()),
        "canais": list(SENDERS.keys()),
    })


@require_GET
def enviar_agora_stream(request):
    """SSE — dispara um envio de teste para uma ConfiguracaoEnvio (?config=ID)."""
    from apps.scrapers.ofertas import selecionar_e_enviar

    cfg_id = request.GET.get("config")
    uid = request.user.id  # capturado fora da thread (request.user não cruza thread)

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    # Só a própria regra do usuário (isolamento multi-tenant).
                    cfg = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner_id=uid).first()
                    if not cfg:
                        print("[ERRO] Configuração não encontrada.")
                        return
                    macros = [cfg.macro_categoria] if cfg.macro_categoria else None
                    alvo = cfg.termo_busca or cfg.macro_categoria or 'qualquer/ofertas'
                    print(f"Selecionando item de '{alvo}'...")
                    r = selecionar_e_enviar(
                        macros, cfg.grupo_id,
                        min_desconto_percent=cfg.min_desconto_percent,
                        horas_cooldown=cfg.horas_cooldown,
                        verificar=True,
                        termo=cfg.termo_busca,
                        canal=getattr(cfg, "canal", "whatsapp"),
                        marketplace=getattr(cfg, "marketplace", "") or None,
                        usuario=cfg.owner,
                    )
                    if r.get("sucesso"):
                        from django.utils import timezone
                        cfg.ultimo_envio = timezone.now()
                        cfg.save(update_fields=["ultimo_envio"])
                        print(f"OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
                    else:
                        print(f"[ERRO] {r.get('motivo')}")
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
def enviar_produto_stream(request):
    """SSE — envia UM produto específico (tela Promoções) p/ o destino escolhido no popup.

    Reusa enviar_oferta_de_produto -> grava HistoricoEnvio em sucesso, então o item
    fica permanentemente bloqueado p/ o envio automático (anti-repetição global).
    """
    from apps.scrapers.ofertas import enviar_oferta_de_produto
    from apps.scrapers.models import HistoricoEnvio

    prod_id = request.GET.get("produto")
    grupo_id = (request.GET.get("grupo") or "").strip()
    grupo_nome = (request.GET.get("grupo_nome") or "").strip()
    canal = (request.GET.get("canal") or "whatsapp").strip()
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        usuario = get_user_model().objects.filter(id=uid).first()
        if not grupo_id:
            print("[ERRO] Nenhum destino informado (grupo/chat).")
            return
        prod = Produto.objects.filter(id=prod_id).first()
        if not prod:
            print("[ERRO] Produto não encontrado.")
            return
        # Dedup POR usuário: este usuário já enviou este produto?
        if HistoricoEnvio.objects.filter(produto_id=prod.id, usuario_id=uid).exists():
            print("[ERRO] Você já enviou este produto antes — bloqueado p/ não repetir.")
            return
        print(f"Enviando '{prod.nome[:60]}' → {grupo_nome or grupo_id} ({canal})...")
        r = enviar_oferta_de_produto(prod, grupo_id, verificar=True, canal=canal, usuario=usuario)
        if r.get("sucesso"):
            print(f"__SENT__ OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
        else:
            print(f"[ERRO] {r.get('motivo')}")

    return _sse_runner(_job)


@require_GET
def buscar_promocoes_stream(request):
    """SSE — busca itens por termo em TODAS as lojas (ML + Amazon) p/ a tela Promoções."""
    from apps.scrapers.marketplaces.registry import MARKETPLACES, get_marketplace

    termo = (request.GET.get("termo") or "").strip()
    uid = request.user.id
    try:
        min_desc = int(float(request.GET.get("min_desconto") or 15))
    except (TypeError, ValueError):
        min_desc = 15

    def _job():
        from django.contrib.auth import get_user_model
        usuario = get_user_model().objects.filter(id=uid).first()
        if not termo:
            print("[ERRO] Digite um termo de busca.")
            return
        total = 0
        for slug in MARKETPLACES:
            mp = get_marketplace(slug)
            try:
                print(f"Buscando '{termo}' em {slug}...")
                # Amazon usa a conta do usuário (itens privados); ML é compartilhado.
                n = mp.buscar_por_termo(termo, min_desconto=min_desc, usuario=usuario) or 0
                total += n
                print(f"  {slug}: {n} item(ns).")
            except Exception as e:
                print(f"  {slug} falhou: {e}")
        print(f"Concluído. {total} item(ns) novos no total.")

    return _sse_runner(_job)


def top_promocoes(request):
    from apps.scrapers.models import HistoricoEnvio
    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.senders.registry import SENDERS

    macros_selecionados = request.GET.getlist("macro")
    categorias_selecionadas = request.GET.getlist("categoria")
    loja_selecionada = (request.GET.get("loja") or "").strip()

    macro_categorias = (
        Produto.objects
        .exclude(macro_categoria__isnull=True)
        .exclude(macro_categoria="")
        .values_list("macro_categoria", flat=True)
        .distinct()
        .order_by("macro_categoria")
    )

    categorias_por_macro = {}
    for row in (
        Produto.objects
        .exclude(macro_categoria__isnull=True).exclude(macro_categoria="")
        .exclude(categoria__isnull=True).exclude(categoria="").exclude(categoria="DESCONHECIDO")
        .values("macro_categoria", "categoria")
        .distinct()
        .order_by("macro_categoria", "categoria")
    ):
        categorias_por_macro.setdefault(row["macro_categoria"], []).append(row["categoria"])

    # Ordenação: 'percent' (padrão — melhor p/ deal bot) ou 'valor' (R$ absoluto economizado).
    ordenar = "valor" if request.GET.get("ordenar") == "valor" else "percent"

    from django.db.models import Q
    qs = Produto.objects.filter(preco_sem_desconto__gt=0).filter(
        # Pool compartilhado (ML, owner=None) + itens privados do usuário (Amazon dele).
        Q(owner__isnull=True) | Q(owner=request.user)
    ).annotate(
        economia=ExpressionWrapper(F("preco_sem_desconto") - F("preco_com_cupom"), output_field=FloatField()),
        percent=ExpressionWrapper(
            (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0 / F("preco_sem_desconto"),
            output_field=FloatField(),
        ),
    )
    if macros_selecionados:
        qs = qs.filter(macro_categoria__in=macros_selecionados)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)
    if loja_selecionada:
        qs = qs.filter(marketplace=loja_selecionada)

    ordem = "-economia" if ordenar == "valor" else "-percent"
    produtos = list(qs.order_by(ordem)[:20])
    cupons_map = {
        c.campanha_id: c
        for c in Cupom.objects.filter(campanha_id__in=[p.campanha_id for p in produtos])
    }
    # Marca itens já enviados POR ESTE usuário (manual OU automático): bloqueia reenvio na UI.
    ja_enviados = set(
        HistoricoEnvio.objects.filter(
            produto_id__in=[p.id for p in produtos], usuario=request.user)
        .values_list("produto_id", flat=True)
    )
    for p in produtos:
        p.cupom = cupons_map.get(p.campanha_id)
        p.ja_enviado = p.id in ja_enviados

    # base da querystring (mantém filtros ao trocar a ordenação)
    from urllib.parse import urlencode
    qs_pairs = [("macro", m) for m in macros_selecionados] + [("categoria", c) for c in categorias_selecionadas]
    # base p/ os chips de loja: preserva macro/categoria/ordem, troca só a loja.
    qs_sem_loja = list(qs_pairs)
    if ordenar == "valor":
        qs_sem_loja.append(("ordenar", "valor"))
    qs_base_sem_loja = (urlencode(qs_sem_loja) + "&") if qs_sem_loja else ""
    if loja_selecionada:
        qs_pairs.append(("loja", loja_selecionada))
    qs_base = (urlencode(qs_pairs) + "&") if qs_pairs else ""

    return render(request, "scrapers/top_promocoes.html", {
        "produtos": produtos,
        "macro_categorias": macro_categorias,
        "categorias_por_macro": categorias_por_macro,
        "macros_selecionados": macros_selecionados,
        "categorias_selecionadas": categorias_selecionadas,
        "loja_selecionada": loja_selecionada,
        "lojas": list(MARKETPLACES.keys()),
        "canais": list(SENDERS.keys()),
        "ordenar": ordenar,
        "qs_base": qs_base,
        "qs_base_sem_loja": qs_base_sem_loja,
    })


@require_GET
def run_scraper_stream(request):
    """SSE endpoint — streams every print() from the scraper to the browser."""

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            # Playwright's sync API leaves asyncio's event loop in a running state
            # on the calling thread, which trips Django's ORM async-safety check.
            # DJANGO_ALLOW_ASYNC_UNSAFE is the documented bypass for this scenario.
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    scrapper_main()
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)  # sentinel

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
def scrape_ofertas_stream(request):
    """SSE endpoint — raspa as ofertas (de/por) do ML e já pré-gera os links de afiliado."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote
    try:
        paginas = int(request.GET.get("paginas", 10))
    except (TypeError, ValueError):
        paginas = 10
    try:
        links_limite = int(request.GET.get("links", 60))
    except (TypeError, ValueError):
        links_limite = 60

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    mapear_ofertas(max_paginas=paginas)
                    if links_limite > 0:
                        # Pool ML compartilhado (owner=None). Amazon não entra aqui.
                        pend_qs = Produto.objects.filter(link_afiliado="", owner__isnull=True)
                        pendentes = list(pend_qs[:links_limite])
                        if pendentes:
                            print(f"\nGerando links de afiliado para {len(pendentes)} oferta(s)...")
                            gerar_links_em_lote(pendentes)
                        sobra = pend_qs.count()
                        if sobra:
                            print(f"{sobra} produto(s) ainda sem link (serão gerados no próximo scrape ou no envio).")
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _sse_runner(fn):
    """Roda fn() capturando prints e streamando via SSE (reusa o padrão _QueueWriter)."""
    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    fn()
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        threading.Thread(target=_run, daemon=True).start()
        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def automacao_control(request):
    """Liga/desliga loops independentes. ?tipo=scrape|envio. POST acao=start|stop.

    scrape = raspagem 24/7 (tela Scraper);  envio = envio pelas regras (tela Envios).
    Um não afeta o outro — processos e PID files separados.
    """
    import sys
    import subprocess
    from apps.scrapers import automacao_state as st

    tipo = request.GET.get("tipo") or request.POST.get("tipo") or "scrape"
    if tipo not in st.JOBS:
        tipo = "scrape"

    if request.method != "POST":
        rodando = st.is_running(tipo)
        estado = st.read_state(tipo) if rodando else {}
        return JsonResponse({"rodando": rodando, "tipo": tipo, "estado": estado})

    acao = request.POST.get("acao")
    if acao == "stop":
        st.parar(tipo)
        return JsonResponse({"rodando": False, "tipo": tipo, "msg": "Parado."})

    # start
    if st.is_running(tipo):
        return JsonResponse({"rodando": True, "tipo": tipo, "msg": "Já estava rodando."})

    base_dir = settings.BASE_DIR  # .../django
    manage = os.path.join(base_dir, "manage.py")
    log = open(st.logfile(tipo), "a", encoding="utf-8")
    # Usa pythonw.exe (sem console) quando existir; cai no python.exe normal se não.
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    # CREATE_NO_WINDOW(0x08000000): roda sem janela de terminal |
    # CREATE_NEW_PROCESS_GROUP(0x200): grupo próprio, sobrevive ao request.
    # (DETACHED_PROCESS é mutuamente exclusivo com CREATE_NO_WINDOW — não usar junto.)
    flags = 0x08000000 | 0x00000200
    args = [py, manage, "automacao", "--modo", tipo]
    args += ["--scrape-horas", "3"] if tipo == "scrape" else ["--tick", "5"]
    p = subprocess.Popen(
        args, cwd=base_dir, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    st.save_pid(tipo, p.pid)
    return JsonResponse({"rodando": True, "tipo": tipo, "msg": f"Iniciado (pid {p.pid})."})


@require_GET
def scrape_cupons_codigo_stream(request):
    """SSE — raspa /ofertas/cupons (produtos + códigos de checkout)."""
    from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
    return _sse_runner(mapear_cupons_codigo)


@require_GET
def buscar_termo_stream(request):
    """SSE — busca direcionada por termo de uma config (?config=ID)."""
    from apps.scrapers.marketplaces.registry import get_marketplace
    cfg_id = request.GET.get("config")
    uid = request.user.id

    def _job():
        cfg = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner_id=uid).first()
        if not cfg or not cfg.termo_busca:
            print("[ERRO] Config sem termo de busca.")
            return
        macro = cfg.macro_categoria or None
        # Busca na loja da config (Amazon=Creators API do dono, ML=Playwright compartilhado).
        mp = get_marketplace(cfg.marketplace or "mercadolivre")
        mp.buscar_por_termo(cfg.termo_busca, min_desconto=int(cfg.min_desconto_percent),
                            macro=macro, usuario=cfg.owner)

    return _sse_runner(_job)


@require_GET
def gerar_links_stream(request):
    """SSE endpoint — gera links de afiliado em lote para produtos sem link."""
    from apps.scrapers.marketplaces.registry import get_marketplace

    try:
        limite = int(request.GET.get("limite", 50))
    except (TypeError, ValueError):
        limite = 50

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    # Só o pool COMPARTILHADO (owner=None, ex: ML). Links de itens
                    # privados (Amazon) são gerados por usuário, na hora do envio.
                    base_qs = Produto.objects.filter(link_afiliado="", owner__isnull=True)
                    pendentes = list(base_qs[:limite])
                    restantes = base_qs.count()
                    print(f"{restantes} produto(s) sem link. Gerando até {limite}...")
                    # Agrupa por loja: cada marketplace gera seus links (ML=Playwright,
                    # Amazon=puro Python). Evita rodar o Link Builder do ML num ASIN.
                    if pendentes:
                        por_loja = {}
                        for p in pendentes:
                            por_loja.setdefault(p.marketplace or "mercadolivre", []).append(p)
                        for slug, grupo in por_loja.items():
                            get_marketplace(slug).prefetch_links(grupo)
                    sobra = Produto.objects.filter(link_afiliado="").count()
                    print(f"Sobraram {sobra} produto(s) sem link.")
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
def auth_stream(request):
    """SSE endpoint — abre browser visível para login no ML e salva auth.json."""
    from apps.scrapers.auxiliar import iniciar_browser, BrowserError

    # Sessão de ML por usuário (auth_{id}.json) — cada um conecta a própria conta.
    auth_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scraper_mercadolivre", f"auth_{request.user.id}.json"
    )

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    with iniciar_browser(auth_path=auth_path, headless=True) as (page, context):
                        pass  # só valida/salva a sessão
            except BrowserError as exc:
                msg = str(exc)
                if "LOGIN_REQUIRED" in msg or "login" in msg.lower():
                    q.put("LOGIN_REQUIRED")
                else:
                    q.put(f"[ERRO] {exc}")
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
