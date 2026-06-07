import asyncio
import os
import queue
import threading
from contextlib import redirect_stdout

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


def configuracoes(request):
    """Painel do afiliado: cria/edita/remove regras de divulgação (nicho→grupo→intervalo)."""
    from apps.scrapers import whatsapp_client

    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "delete":
            ConfiguracaoEnvio.objects.filter(id=request.POST.get("id")).delete()
        else:
            cfg_id = request.POST.get("id")
            campos = dict(
                macro_categoria=request.POST.get("macro_categoria", "").strip(),
                grupo_id=request.POST.get("grupo_id", "").strip(),
                grupo_nome=request.POST.get("grupo_nome", "").strip(),
                intervalo_minutos=int(request.POST.get("intervalo_minutos") or 60),
                min_desconto_percent=float(request.POST.get("min_desconto_percent") or 15),
                horas_cooldown=int(request.POST.get("horas_cooldown") or 24),
                ativo=bool(request.POST.get("ativo")),
            )
            if cfg_id:
                ConfiguracaoEnvio.objects.filter(id=cfg_id).update(**campos)
            else:
                ConfiguracaoEnvio.objects.create(**campos)
        return redirect("scraper-configuracoes")

    macros = list(
        Produto.objects
        .exclude(macro_categoria__isnull=True).exclude(macro_categoria="")
        .values_list("macro_categoria", flat=True).distinct().order_by("macro_categoria")
    )
    grupos_resp = whatsapp_client.listar_grupos()
    grupos = grupos_resp.get("grupos", []) if isinstance(grupos_resp, dict) else []

    return render(request, "scrapers/configuracoes.html", {
        "configs": ConfiguracaoEnvio.objects.all().order_by("macro_categoria"),
        "macros": macros,
        "grupos": grupos,
        "grupos_erro": grupos_resp.get("erro") if isinstance(grupos_resp, dict) else None,
    })


@require_GET
def enviar_agora_stream(request):
    """SSE — dispara um envio de teste para uma ConfiguracaoEnvio (?config=ID)."""
    from apps.scrapers.ofertas import selecionar_e_enviar

    cfg_id = request.GET.get("config")

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    cfg = ConfiguracaoEnvio.objects.filter(id=cfg_id).first()
                    if not cfg:
                        print("[ERRO] Configuração não encontrada.")
                        return
                    macros = [cfg.macro_categoria] if cfg.macro_categoria else None
                    print(f"Selecionando item de '{cfg.macro_categoria or 'qualquer/ofertas'}'...")
                    r = selecionar_e_enviar(
                        macros, cfg.grupo_id,
                        min_desconto_percent=cfg.min_desconto_percent,
                        horas_cooldown=cfg.horas_cooldown,
                        verificar=True,
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


def top_promocoes(request):
    macros_selecionados = request.GET.getlist("macro")
    categorias_selecionadas = request.GET.getlist("categoria")

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

    qs = Produto.objects.annotate(
        economia=ExpressionWrapper(F("preco_sem_desconto") - F("preco_com_cupom"), output_field=FloatField())
    )
    if macros_selecionados:
        qs = qs.filter(macro_categoria__in=macros_selecionados)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)

    produtos = list(qs.order_by("-economia")[:10])
    cupons_map = {
        c.campanha_id: c
        for c in Cupom.objects.filter(campanha_id__in=[p.campanha_id for p in produtos])
    }
    for p in produtos:
        p.cupom = cupons_map.get(p.campanha_id)
    return render(request, "scrapers/top_promocoes.html", {
        "produtos": produtos,
        "macro_categorias": macro_categorias,
        "categorias_por_macro": categorias_por_macro,
        "macros_selecionados": macros_selecionados,
        "categorias_selecionadas": categorias_selecionadas,
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
    """SSE endpoint — raspa as ofertas (de/por) do ML."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    try:
        paginas = int(request.GET.get("paginas", 10))
    except (TypeError, ValueError):
        paginas = 10

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    mapear_ofertas(max_paginas=paginas)
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
def gerar_links_stream(request):
    """SSE endpoint — gera links de afiliado em lote para produtos sem link."""
    from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote

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
                    pendentes = list(
                        Produto.objects.filter(link_afiliado="")[:limite]
                    )
                    restantes = Produto.objects.filter(link_afiliado="").count()
                    print(f"{restantes} produto(s) sem link. Gerando até {limite}...")
                    if pendentes:
                        gerar_links_em_lote(pendentes)
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

    auth_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scrapper_mercadolivre", "auth.json"
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
