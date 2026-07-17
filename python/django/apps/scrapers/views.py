import asyncio
import os
import queue
import threading
from contextlib import redirect_stdout
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import (
    F, ExpressionWrapper, Exists, FloatField, OuterRef, Q, Count, Sum,
)
from django.http import StreamingHttpResponse, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, LinkAfiliadoUsuario, Produto,
    Publicacao, ReceitaAfiliado, RelatorioSync, FonteIngestao, CupomNormalizado,
)
from apps.scrapers.scraper_mercadolivre.scraper import main as scrapper_main


def staff_required(view):
    """Restringe a view a administradores (is_staff).

    A raspagem (e o login/sessão de ML compartilhada) é controlada só pelo admin;
    usuários comuns usam Promoções, Envios e Conexões. 403 em vez de redirect p/
    proteger também as chamadas diretas aos endpoints SSE (não só esconder no menu).
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied("Apenas administradores controlam a raspagem.")
        return view(request, *args, **kwargs)
    return _wrapped


def superadmin_required(view):
    """Restringe a view ao superadmin (is_superuser).

    Workspace do superadmin: lista de usuários, uso/máquinas, cotas, suspensão e
    impersonação. 403 (não redirect) p/ proteger chamadas diretas aos endpoints.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Apenas o superadmin acessa este painel.")
        return view(request, *args, **kwargs)
    return _wrapped


def throttle_sse(max_por_min=10):
    """Limita quantas vezes/min um usuário dispara um endpoint SSE pesado.

    Cada stream sobe uma thread (Playwright/HTTP) na MÁQUINA COMPARTILHADA;
    sem teto, um tenant satura CPU/RAM/Chromium dos demais. Ao estourar, devolve um
    stream curto de erro (EventSource-friendly) em vez de rodar o job.
    """
    def deco(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            from django.core.cache import cache
            from apps.scrapers.eventos import log_event
            uid = getattr(request.user, "id", None) or "anon"
            key = f"sse-throttle:{view.__name__}:{uid}"
            if cache.get(key, 0) >= max_por_min:
                log_event("sistema", "sse_throttled", "Endpoint pesado limitado.",
                          level="warning", usuario=request.user,
                          contexto={"view": view.__name__})
                def _err():
                    yield "data: [ERRO] Muitas execuções seguidas. Aguarde ~1 minuto.\n\n"
                    yield "data: __DONE__\n\n"
                resp = StreamingHttpResponse(_err(), content_type="text/event-stream")
                resp["Cache-Control"] = "no-cache"
                return resp
            cache.set(key, cache.get(key, 0) + 1, 60)
            return view(request, *args, **kwargs)
        return _wrapped
    return deco


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


def operations_dashboard(request):
    """Centro operacional e de receita do afiliado."""
    from datetime import timedelta
    from apps.scrapers.monitor_conexao import ml_conectado, wa_conectado

    from apps.scrapers.relatorios import resumo_financeiro

    desde = timezone.now() - timedelta(days=30)
    pubs = Publicacao.objects.filter(usuario=request.user, criada_em__gte=desde)
    resumo = pubs.aggregate(
        enviados=Count("id", filter=Q(status="enviado"), distinct=True),
        falhas=Count("id", filter=Q(status="falhou"), distinct=True),
        pendentes=Count("id", filter=Q(status="pendente"), distinct=True),
        cliques=Count("cliques"),
    )
    # Snapshot mais recente por loja, não Sum de 30 dias: ver resumo_financeiro.
    financeiro = resumo_financeiro(request.user)
    comissao = financeiro.get("comissao") or 0
    posts = resumo.get("enviados") or 0
    financeiro["comissao_por_post"] = comissao / posts if posts else 0
    melhores_categorias = list(
        pubs.filter(status="enviado").values("categoria")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-cliques", "-envios")[:5]
    )
    melhores_destinos = list(
        pubs.filter(status="enviado").values("destino_nome", "destino_id")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-cliques", "-envios")[:5]
    )
    from apps.scrapers.conexoes import estado_ml, estado_whatsapp

    perfil = request.user.perfil
    configs = ConfiguracaoEnvio.objects.filter(owner=request.user)
    alertas = []
    est_ml = estado_ml(request.user)
    est_wa = estado_whatsapp(request.user, session=perfil.sessao_whatsapp())
    ml_ok, wa_ok = est_ml.conectado, est_wa.conectado
    if not ml_ok and not perfil.amazon_conectado():
        # O motivo vem do estado: "sessão expirou" e "nunca conectou" pedem ações
        # diferentes, e o texto fixo dizia a mesma coisa para os dois.
        alertas.append(("Loja desconectada",
                        est_ml.motivo or "Conecte Mercado Livre ou Amazon para gerar links comissionados.",
                        "scraper-conta"))
    if not wa_ok and not perfil.telegram_conectado():
        alertas.append(("Nenhum canal conectado", "Conecte WhatsApp ou Telegram antes de ativar envios.", "scraper-whatsapp"))
    pausadas = configs.filter(ativo=False).exclude(motivo_pausa="").count()
    if pausadas:
        alertas.append((f"{pausadas} regra(s) pausada(s)", "Revise as falhas consecutivas e reative quando estiver pronto.", "scraper-configuracoes"))
    if not configs.exists():
        alertas.append(("Crie sua primeira automação", "Personalize um destino e faça um envio de teste.", "scraper-configuracoes"))
    syncs = {
        s.marketplace: s for s in RelatorioSync.objects.filter(usuario=request.user)
    }
    for marketplace in ("mercadolivre", "amazon"):
        syncs.setdefault(marketplace, RelatorioSync(
            usuario=request.user, marketplace=marketplace))
    for sync in syncs.values():
        # "nao_configurado" fica de fora: não é incidente e não há ação do usuário —
        # alertar sobre isso era um aviso permanente que ele não podia resolver. O
        # estado aparece na lista de sincronizações, que é onde ele pertence.
        if sync.status in {"erro", "acao"}:
            alertas.append((
                f"Relatório {sync.marketplace} precisa de atenção",
                sync.erro or "Sincronização automática não concluiu.",
                # Reconectar a conta é o que resolve; "home" apontava pra esta mesma
                # página, então clicar no alerta não levava a lugar nenhum.
                "scraper-conta",
            ))
    return render(request, "home.html", {
        "resumo": resumo, "financeiro": financeiro,
        "melhores_categorias": melhores_categorias,
        "melhores_destinos": melhores_destinos,
        "publicacoes": pubs.select_related("produto", "configuracao").order_by("-criada_em")[:10],
        "alertas": alertas, "configs": configs, "syncs": list(syncs.values()),
        "ml_ok": ml_ok, "wa_ok": wa_ok,
        "est_ml": est_ml, "est_wa": est_wa,
        "tg_ok": perfil.telegram_conectado(),
    })


def _responder_clique(publicacao):
    """Registra somente o evento de clique e redireciona ao link afiliado."""
    destino = publicacao.link_afiliado or ""
    # Defesa: só redireciona p/ http(s). Barra esquemas perigosos (javascript:, data:)
    # caso um link corrompido chegue ao banco.
    if not destino.startswith(("https://", "http://")):
        return HttpResponse("Link inválido ou indisponível.", status=404)
    CliquePublicacao.objects.create(publicacao=publicacao)
    response = redirect(destino)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@login_not_required
def redirect_rastreado(request, token):
    """Formato antigo (token assinado): mantém válidos os links já publicados."""
    try:
        payload = signing.loads(token, salt="click")
        publicacao = Publicacao.objects.get(id_publico=payload["p"], status="enviado")
    except (signing.BadSignature, KeyError, Publicacao.DoesNotExist):
        return HttpResponse("Link inválido ou indisponível.", status=404)
    return _responder_clique(publicacao)


@login_not_required
def redirect_curto(request, slug):
    """Formato curto (/r/<slug>/) que entra nas mensagens novas."""
    try:
        publicacao = Publicacao.objects.get(slug_curto=slug, status="enviado")
    except Publicacao.DoesNotExist:
        return HttpResponse("Link inválido ou indisponível.", status=404)
    return _responder_clique(publicacao)


@require_POST
def sincronizar_receitas(request):
    """Agenda a sincronização dos relatórios do marketplace selecionado.

    Agenda, não executa: o sync sobe um Chromium (Playwright, goto de 45s) e fazer
    isso DENTRO do request punha um browser inteiro no processo do gunicorn, contra o
    timeout de 120s e disputando a CPU com o resto do painel. Quem executa é o worker
    "relatorios" do Procfile, que já roda sync_due_reports; aqui só marcamos o
    registro como vencido, e ele pega no próximo poll (~1min).
    """
    marketplace = (request.POST.get("marketplace") or "").lower()
    if marketplace not in {"mercadolivre", "amazon"}:
        messages.error(request, "Marketplace inválido para sincronização.")
        return redirect("home")
    sync, _ = RelatorioSync.objects.get_or_create(
        usuario=request.user, marketplace=marketplace)
    RelatorioSync.objects.filter(pk=sync.pk).update(proxima_execucao=timezone.now())
    messages.success(
        request, f"{marketplace}: sincronização agendada. "
                 "O resultado aparece aqui em instantes.")
    return redirect("home")


@staff_required
def dashboard(request):
    """Painel + checklist de primeiros passos (onboarding orientado a conexões)."""
    from apps.scrapers.monitor_conexao import ml_conectado, wa_conectado

    user = request.user
    perfil = getattr(user, "perfil", None)

    ml_ok = ml_conectado(user)
    tag_ok = ml_ok
    wa_ok = wa_conectado(perfil.sessao_whatsapp() if perfil else str(user.id))
    tg_ok = bool(perfil and perfil.telegram_conectado())
    canal_ok = wa_ok or tg_ok
    regra_ok = ConfiguracaoEnvio.objects.filter(owner=user).exists()

    # Uma etapa = {título, feito, CTA}. Ordem = caminho até o "aha" (enviar 1 oferta).
    passos = [
        {"key": "ml", "titulo": "Conectar sua conta do Mercado Livre", "feito": ml_ok,
         "desc": "Login seguro na própria página — gera seus links de afiliado.",
         "cta": "Conectar", "url": "/scrapers/ml/", "icon": "shopping-bag"},
        {"key": "tag", "titulo": "Mercado Livre afiliado conectado", "feito": tag_ok,
         "desc": "O Link Builder usa a conta logada para gerar o link comissionado.",
         "cta": "Conectar", "url": "/scrapers/ml/", "icon": "badge-dollar-sign"},
        {"key": "canal", "titulo": "Conectar um canal de envio", "feito": canal_ok,
         "desc": "WhatsApp (QR) ou Telegram (bot) — por onde as ofertas saem.",
         "cta": "Conectar", "url": "/scrapers/whatsapp/", "icon": "message-circle"},
        {"key": "regra", "titulo": "Criar uma regra de envio", "feito": regra_ok,
         "desc": "Nicho → canal → intervalo. Depois é só ligar o automático.",
         "cta": "Criar regra", "url": "/scrapers/config/", "icon": "list-checks"},
    ]
    feitos = sum(1 for p in passos if p["feito"])
    return render(request, "scrapers/dashboard.html", {
        "passos": passos,
        "passos_feitos": feitos,
        "passos_total": len(passos),
        "onboarding_completo": feitos == len(passos),
    })


def comecar(request):
    """Checklist de onboarding self-serve: cada passo lê o estado real e mostra ✓/todo.

    Objetivo: um usuário novo consegue ficar operacional sozinho (tags → conexão →
    regra → ligar envio) sem depender do suporte."""
    from apps.scrapers.conexoes import estado_ml, estado_whatsapp

    perfil = getattr(request.user, "perfil", None)
    # Estado ao vivo, não perfil.wa_estado/ml_estado: aquelas colunas são o último
    # estado visto pelo watchdog e ficam `None` até ele rodar a primeira vez. Esta
    # tela mostrava "desconectado" para quem estava conectado, enquanto o dashboard
    # ao lado mostrava conectado.
    wa_ok = estado_whatsapp(request.user).conectado
    loja_ok = estado_ml(request.user).conectado or bool(perfil and perfil.amazon_conectado())
    tem_config = ConfiguracaoEnvio.objects.filter(owner=request.user).exists()
    teste_ok = Publicacao.objects.filter(usuario=request.user, status="enviado").exists()
    envio_ligado = ConfiguracaoEnvio.objects.filter(owner=request.user, ativo=True).exists()

    passos = [
        {"titulo": "Conectar WhatsApp", "feito": wa_ok,
         "desc": "Pareie seu aparelho pelo QR Code para disparar as ofertas.",
         "url": "scraper-whatsapp", "cta": "Conectar WhatsApp"},
        {"titulo": "Conectar loja (login no ML / Amazon)", "feito": loja_ok,
         "desc": "Faça login no Mercado Livre e salve a sessão no robô (ou conecte a Amazon Creators).",
         "url": "scraper-conta", "cta": "Conectar loja"},
        {"titulo": "Descrever o público de um grupo", "feito": tem_config,
         "desc": "Cada grupo pode ter nichos, descontos, horários e voz próprios.",
         "url": "scraper-configuracoes", "cta": "Criar regra"},
        {"titulo": "Publicar uma oferta de teste", "feito": teste_ok,
         "desc": "Valide produto, preço, cupom, mensagem e link antes de automatizar.",
         "url": "scraper-configuracoes", "cta": "Fazer teste"},
        {"titulo": "Ativar uma automação", "feito": envio_ligado and teste_ok,
         "desc": "Depois do teste, mantenha ativa somente a regra que estiver pronta.",
         "url": "scraper-configuracoes", "cta": "Ir para Envios"},
    ]
    feitos = sum(1 for p in passos if p["feito"])
    return render(request, "scrapers/comecar.html", {
        "passos": passos, "feitos": feitos, "total": len(passos),
        "completo": feitos == len(passos),
    })


def _tem_sessao_ml(user) -> bool:
    """True se a sessão do ML deste usuário existe E o ML ainda a aceita.

    Antes só checava a existência do arquivo, ignorando a validade — então a tela de
    Conta dizia "sessão ok" com um auth de 30 dias enquanto o dashboard, que aplicava
    a regra de staleness, dizia "loja desconectada".
    """
    from apps.scrapers.conexoes import estado_ml
    return estado_ml(user).conectado


def configurar_conta(request):
    """Conta do afiliado: tags de comissão (ML/Amazon), credenciais Amazon Creators
    e sessão do Mercado Livre. Cada usuário configura a PRÓPRIA conta (multi-tenant).

    A tag é o que garante a comissão do usuário; a sessão ML é o "login salvo no robô"
    (conectada pela web em Conexão Mercado Livre — browser remoto com live view)."""
    perfil = getattr(request.user, "perfil", None)
    if perfil and request.method == "POST":
        perfil.afiliado_tag_ml = (request.POST.get("afiliado_tag_ml") or "").strip()
        perfil.afiliado_tag_amazon = (request.POST.get("afiliado_tag_amazon") or "").strip()
        perfil.amazon_credential_id = (request.POST.get("amazon_credential_id") or "").strip()
        perfil.amazon_creators_host = (request.POST.get("amazon_creators_host") or "").strip()
        perfil.nome_marca = (request.POST.get("nome_marca") or perfil.nome_marca).strip()[:80]
        perfil.tom_marca = (request.POST.get("tom_marca") or perfil.tom_marca).strip()[:20]
        perfil.chamada_acao = (request.POST.get("chamada_acao") or perfil.chamada_acao).strip()[:120]
        perfil.divulgacao_afiliado = (
            request.POST.get("divulgacao_afiliado") or perfil.divulgacao_afiliado
        ).strip()[:180]
        perfil.template_a = (request.POST.get("template_a") or "").strip()
        perfil.template_b = (request.POST.get("template_b") or "").strip()
        # Secret só sobrescreve se o campo veio preenchido (em branco mantém o atual).
        novo_secret = (request.POST.get("amazon_credential_secret") or "").strip()
        campos = ["afiliado_tag_ml", "afiliado_tag_amazon", "amazon_credential_id",
                  "amazon_creators_host", "nome_marca", "tom_marca", "chamada_acao",
                  "divulgacao_afiliado", "template_a", "template_b"]
        if novo_secret:
            perfil.amazon_credential_secret = novo_secret
            campos.append("amazon_credential_secret")
        perfil.save(update_fields=campos)
        messages.success(request, "Conta atualizada.")
        return redirect("scraper-conta")

    from apps.scrapers.conexoes import estado_amazon_relatorios
    return render(request, "scrapers/conta.html", {
        "perfil": perfil,
        "tem_secret": bool(perfil and perfil.amazon_credential_secret),
        "ml_sessao_ok": _tem_sessao_ml(request.user),
        "amazon_conectado": bool(perfil and perfil.amazon_conectado()),
        # Ortogonal ao "conectado": a Creators API é upgrade opcional, exibido só
        # como informação. Não pode voltar a virar requisito de conexão.
        "amazon_creators_ativa": bool(perfil and perfil.amazon_creators_ativa()),
        "amazon_relatorio_conectado": bool(estado_amazon_relatorios(request.user).conectado),
        "billing_checkout_url": settings.BILLING_CHECKOUT_URL,
        "billing_portal_url": settings.BILLING_PORTAL_URL,
    })


def _wa_session(request):
    """Sessão WhatsApp DESTE usuário (multi-tenant). Cada um pareia a própria conta."""
    perfil = getattr(request.user, "perfil", None)
    return perfil.sessao_whatsapp() if perfil else str(request.user.id)


def whatsapp_painel(request):
    """Tela de conexão do WhatsApp: status + QR Code para parear pelo navegador."""
    from apps.scrapers import whatsapp_client
    session = _wa_session(request)
    whatsapp_client.iniciar_sessao(session)
    return render(request, "scrapers/whatsapp.html", {
        "status": whatsapp_client.status(session),
    })


@require_GET
def whatsapp_status_json(request):
    """JSON de status para polling do front."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.status(_wa_session(request)))


@require_POST
def whatsapp_refresh_grupos(request):
    """Força re-sincronização da lista de grupos no Node e devolve o resultado.

    POST porque dispara trabalho pesado (getChats no Chromium, 45s de timeout).
    Em GET a rota ficava sem proteção CSRF — acionável por um <img src> de
    qualquer site — e sujeita a pré-fetch do navegador. Espelha
    whatsapp_desconectar e o POST /api/grupos/refresh do próprio Node.
    """
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.refresh_grupos(_wa_session(request)))


@require_GET
def whatsapp_grupos_json(request):
    """Lista grupos (GET leve) para o front carregar via AJAX sem travar o render."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.listar_grupos(_wa_session(request)))


@require_POST
def whatsapp_desconectar(request):
    """Desfaz o pareamento do WhatsApp deste usuário (espelha telegram_desconectar)."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.desconectar(_wa_session(request)))


# --- Conexão web do Mercado Livre (login via browser remoto, sem script local) ---

def ml_conexao_painel(request):
    """Tela de conexão do ML: o usuário loga no ML dentro de um live view embutido."""
    from apps.scrapers import ml_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": ml_conexao.status(request.user.id),
        "marketplace_nome": "Mercado Livre", "conexao_prefix": "/scrapers/ml",
    })


@require_GET
def ml_conexao_status_json(request):
    """JSON de status para polling do front (fase, live_view_url, auth_valido)."""
    from apps.scrapers import ml_conexao
    return JsonResponse(ml_conexao.status(request.user.id))


def amazon_conexao_painel(request):
    """Login interativo da Amazon Associates, exclusivo para relatórios."""
    from apps.scrapers import amazon_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": amazon_conexao.status(request.user.id),
        "marketplace_nome": "Amazon Associados", "conexao_prefix": "/scrapers/amazon",
        "relatorio": True,
    })


@require_GET
def amazon_conexao_status_json(request):
    from apps.scrapers import amazon_conexao
    return JsonResponse(amazon_conexao.status(request.user.id))


@require_POST
def amazon_conexao_start(request):
    from apps.scrapers import amazon_conexao
    return JsonResponse(amazon_conexao.criar_sessao(request.user))


@require_POST
def amazon_conexao_salvar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.salvar_agora(request.user.id)
    return JsonResponse(amazon_conexao.status(request.user.id))


@require_POST
def amazon_conexao_cancelar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


@require_GET
def amazon_conexao_frames(request):
    from apps.scrapers import amazon_conexao
    def _stream():
        yield from (f"data: {frame}\n\n" for frame in amazon_conexao.frames(request.user.id))
        yield "data: __DONE__\n\n"
    return StreamingHttpResponse(_stream(), content_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@require_POST
def amazon_conexao_input(request):
    import json
    from apps.scrapers import amazon_conexao
    try:
        events = json.loads((request.body or b"").decode() or "{}").get("events")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.enfileirar_input(request.user.id, events))


@require_POST
def ml_conexao_start(request):
    """Abre (ou reaproveita) a sessão remota de login do ML e devolve o estado."""
    from apps.scrapers import ml_conexao
    return JsonResponse(ml_conexao.criar_sessao(request.user))


@require_POST
def ml_conexao_salvar(request):
    """'Já entrei' — força a captura da sessão sem esperar o auto-detect."""
    from apps.scrapers import ml_conexao
    ml_conexao.salvar_agora(request.user.id)
    return JsonResponse(ml_conexao.status(request.user.id))


@require_POST
def ml_conexao_cancelar(request):
    """Cancela a sessão de login em andamento."""
    from apps.scrapers import ml_conexao
    ml_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


@require_GET
def ml_conexao_frames(request):
    """SSE — transmite os frames (JPEG base64) do Chromium local pro <canvas> do front.

    Live view self-hosted: o worker (ml_conexao) roda o browser e captura a tela via
    CDP screencast; aqui só empurramos o último frame de CADA usuário (fila isolada por
    request.user.id — um tenant nunca vê a tela do outro)."""
    from apps.scrapers import ml_conexao

    def _stream():
        for frame in ml_conexao.frames(request.user.id):
            yield f"data: {frame}\n\n"
        yield "data: __DONE__\n\n"

    resp = StreamingHttpResponse(_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@require_POST
def ml_conexao_input(request):
    """Recebe eventos de mouse/teclado do front e encaminha pro browser de login.

    Body JSON: {"events": [{"t":"down","x":..,"y":..}, {"t":"char","text":"a"}, ...]}.
    A validação/limites ficam em ml_conexao.enfileirar_input (dados do cliente)."""
    import json
    from apps.scrapers import ml_conexao
    try:
        payload = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_conexao.enfileirar_input(request.user.id, payload.get("events")))


@require_GET
def whatsapp_qr_png(request):
    """Renderiza o QR do WhatsApp como PNG (vindo do serviço Node)."""
    import qrcode
    from io import BytesIO
    from apps.scrapers import whatsapp_client

    info = whatsapp_client.qrcode(_wa_session(request))
    qr = info.get("qr")
    if not qr:
        # 204 = sem QR (já conectado ou ainda gerando)
        return HttpResponse(status=204)
    buf = BytesIO()
    qrcode.make(qr).save(buf, format="PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


def telegram_painel(request):
    """Tela de conexão do Telegram: o usuário cola o token do próprio bot (via web)."""
    return render(request, "scrapers/telegram.html")


def _token_telegram_valido(token: str) -> bool:
    """Formato canônico do BotFather: <id numérico>:<segredo>. Rejeita qualquer coisa
    com '/', espaço ou caracteres fora do alfabeto — evita truques de path na URL do
    getMe (f'.../bot{token}/getMe') e chamadas malformadas."""
    import re
    return bool(re.fullmatch(r"\d{3,}:[A-Za-z0-9_-]{30,}", token or ""))


def _telegram_getme(token: str) -> dict:
    """Valida um token via getMe (só HTTP, sem browser). Não levanta."""
    import requests as _rq
    if not token:
        return {"token": False, "ok": False}
    if not _token_telegram_valido(token):
        return {"token": True, "ok": False, "erro": "Formato de token inválido."}
    try:
        r = _rq.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        d = r.json()
        if d.get("ok"):
            info = d.get("result", {})
            return {"token": True, "ok": True,
                    "username": info.get("username"), "nome": info.get("first_name")}
        return {"token": True, "ok": False, "erro": d.get("description") or "getMe falhou"}
    except Exception as e:
        return {"token": True, "ok": False, "erro": str(e)}


@require_GET
def telegram_status_json(request):
    """Status do bot do usuário (token no Perfil; fallback global) via getMe."""
    from apps.scrapers.senders.telegram import resolver_token
    return JsonResponse(_telegram_getme(resolver_token(request.user)))


@require_POST
def telegram_conectar(request):
    """Salva o token do bot do usuário no Perfil — depois de validar via getMe."""
    token = (request.POST.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "erro": "Cole o token do seu bot."}, status=400)
    res = _telegram_getme(token)
    if not res.get("ok"):
        return JsonResponse({"ok": False, "erro": res.get("erro") or "Token inválido."}, status=400)
    perfil = request.user.perfil
    perfil.telegram_bot_token = token
    perfil.save(update_fields=["telegram_bot_token"])
    return JsonResponse({"ok": True, **res})


@require_POST
def telegram_desconectar(request):
    """Remove o token do bot do usuário."""
    perfil = request.user.perfil
    perfil.telegram_bot_token = ""
    perfil.save(update_fields=["telegram_bot_token"])
    return JsonResponse({"ok": True})


def configuracoes(request):
    """Painel do afiliado: cria/edita/remove regras de divulgação (nicho→grupo→intervalo)."""
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "delete":
            # Só apaga regra do próprio usuário (isolamento multi-tenant).
            ConfiguracaoEnvio.objects.filter(
                id=request.POST.get("id"), owner=request.user).delete()
        elif acao == "perfil":
            # Identidade de afiliado + credenciais Amazon por-usuário (via web, não .env).
            perfil = request.user.perfil
            perfil.afiliado_tag_ml = (request.POST.get("afiliado_tag_ml") or "").strip()
            perfil.afiliado_tag_amazon = (request.POST.get("afiliado_tag_amazon") or "").strip()
            perfil.amazon_credential_id = (request.POST.get("amazon_credential_id") or "").strip()
            perfil.amazon_creators_host = (request.POST.get("amazon_creators_host") or "").strip()
            # Secret só sobrescreve se o usuário digitou algo (campo vem mascarado/vazio).
            novo_secret = (request.POST.get("amazon_credential_secret") or "").strip()
            campos = ["afiliado_tag_ml", "afiliado_tag_amazon",
                      "amazon_credential_id", "amazon_creators_host"]
            if novo_secret:
                perfil.amazon_credential_secret = novo_secret
                campos.append("amazon_credential_secret")
            perfil.save(update_fields=campos)
        else:
            cfg_id = request.POST.get("id")
            # Sub-nichos: multi-select -> junta as strings de termos (OR no filtro)
            termos = [t.strip() for t in request.POST.getlist("termo_busca") if t.strip()]
            canal = (request.POST.get("canal") or "whatsapp").strip()
            if canal not in {"whatsapp", "telegram"}:
                messages.error(request, "Canal de envio inválido.")
                return redirect("scraper-configuracoes")
            # Telegram usa o campo de chat_id digitado; WhatsApp usa o grupo escolhido.
            grupo_id = (request.POST.get("telegram_chat_id") if canal == "telegram"
                        else request.POST.get("grupo_id")) or ""
            if not grupo_id.strip():
                messages.error(request, "Escolha ou informe um grupo de destino.")
                return redirect("scraper-configuracoes")
            try:
                intervalo = int(request.POST.get("intervalo_minutos") or 60)
                janela_inicio = int(request.POST.get("janela_inicio") or 8)
                janela_fim = int(request.POST.get("janela_fim") or 20)
                desconto = float(request.POST.get("min_desconto_percent") or 15)
                max_envios_dia = int(request.POST.get("max_envios_dia") or 20)
                pausar_apos_falhas = int(request.POST.get("pausar_apos_falhas") or 5)
            except (TypeError, ValueError):
                messages.error(request, "Intervalo, horários ou desconto possuem valor inválido.")
                return redirect("scraper-configuracoes")
            if intervalo < 1 or not (0 <= janela_inicio <= 23 and 0 <= janela_fim <= 23):
                messages.error(request, "Use intervalo positivo e horários entre 0 e 23.")
                return redirect("scraper-configuracoes")
            if not (0 <= desconto <= 100):
                messages.error(request, "O desconto mínimo deve ficar entre 0% e 100%.")
                return redirect("scraper-configuracoes")
            if max_envios_dia < 1 or pausar_apos_falhas < 1:
                messages.error(request, "Limites diários e de falhas devem ser positivos.")
                return redirect("scraper-configuracoes")
            campos = dict(
                macro_categoria=request.POST.get("macro_categoria", "").strip(),
                termo_busca=", ".join(termos),
                canal=canal,
                marketplace=(request.POST.get("marketplace") or "").strip(),
                grupo_id=grupo_id.strip(),
                grupo_nome=request.POST.get("grupo_nome", "").strip(),
                intervalo_minutos=intervalo,
                janela_inicio=janela_inicio,
                janela_fim=janela_fim,
                min_desconto_percent=desconto,
                max_envios_dia=max_envios_dia,
                pausar_apos_falhas=pausar_apos_falhas,
                variante_template=(request.POST.get("variante_template") or "alternar"),
                nome_marca=(request.POST.get("nome_marca") or "").strip()[:80],
                tom_marca=(request.POST.get("tom_marca") or "").strip()[:20],
                chamada_acao=(request.POST.get("chamada_acao") or "").strip()[:120],
                divulgacao_afiliado=(request.POST.get("divulgacao_afiliado") or "").strip()[:180],
                template_a=(request.POST.get("template_a") or "").strip(),
                template_b=(request.POST.get("template_b") or "").strip(),
                ativo=bool(request.POST.get("ativo")),
            )
            if cfg_id:
                # update() não dispara validação, mas o filtro por owner garante posse.
                ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).update(**campos)
            else:
                # Cota de regras por usuário (protege a máquina compartilhada).
                perfil = getattr(request.user, "perfil", None)
                limite = perfil.cota_max_configs() if perfil else 0
                atuais = ConfiguracaoEnvio.objects.filter(owner=request.user).count()
                if limite and atuais >= limite:
                    messages.error(
                        request,
                        f"Limite de {limite} regras atingido. Remova uma ou peça mais ao suporte.")
                    return redirect("scraper-configuracoes")
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
        "perfil": request.user.perfil,
    })


@require_GET
@throttle_sse(6)
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
                        configuracao=cfg,
                        destino_nome=cfg.grupo_nome,
                    )
                    if r.get("sucesso"):
                        from django.utils import timezone
                        cfg.ultimo_envio = timezone.now()
                        cfg.save(update_fields=["ultimo_envio"])
                        print(f"OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
                    else:
                        print(f"[ERRO] {r.get('motivo')}")
                        if r.get("precisa_login_ml"):
                            print("__ML_LOGIN__")
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
@throttle_sse(6)
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
        # Isolamento multi-tenant: só o pool compartilhado (owner=None, ex: ML) ou
        # itens privados DESTE usuário (Amazon dele). Impede enviar item de outro dono.
        prod = Produto.objects.filter(
            Q(owner__isnull=True) | Q(owner_id=uid), id=prod_id).first()
        if not prod:
            print("[ERRO] Produto não encontrado.")
            return
        from datetime import timedelta
        recente = Publicacao.objects.filter(
            produto_id=prod.id, usuario_id=uid, destino_id=grupo_id,
            status__in=("enviado", "incerto"),
            criada_em__gte=timezone.now() - timedelta(hours=24),
        ).order_by("-enviada_em").first()
        if recente and prod.preco_com_cupom > recente.preco_final * .95:
            if recente.status == "incerto":
                print("[ERRO] O envio anterior não foi confirmado. Confira o grupo antes de tentar novamente.")
            else:
                print("[ERRO] Este destino recebeu a oferta nas últimas 24h.")
            return
        print(f"Enviando '{prod.nome[:60]}' → {grupo_nome or grupo_id} ({canal})...")
        r = enviar_oferta_de_produto(
            prod, grupo_id, verificar=True, canal=canal, usuario=usuario,
            destino_nome=grupo_nome)
        if r.get("sucesso"):
            print(f"__SENT__ OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
        else:
            print(f"[ERRO] {r.get('motivo')}")
            if r.get("precisa_login_ml"):
                print("__ML_LOGIN__")  # a UI troca por um botão "Reconectar Mercado Livre"

    return _sse_runner(_job)


@require_GET
@throttle_sse(6)
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

    filtros_key = "top_promocoes_filtros"
    if request.GET.get("reset") == "1":
        request.session.pop(filtros_key, None)
        return redirect("scraper-top")

    tem_filtros_na_url = any(
        chave in request.GET
        for chave in ("macro", "categoria", "loja", "ordenar", "q", "min_desconto",
                      "tipo", "fonte", "confianca", "atualizado_desde")
    )
    if tem_filtros_na_url:
        filtros = {
            "macro": request.GET.getlist("macro"),
            "categoria": request.GET.getlist("categoria"),
            "loja": (request.GET.get("loja") or "").strip(),
            "ordenar": "valor" if request.GET.get("ordenar") == "valor" else "percent",
            "q": (request.GET.get("q") or "").strip()[:120],
            "min_desconto": (request.GET.get("min_desconto") or "").strip(),
            "tipo": "cupom" if request.GET.get("tipo") == "cupom" else "oferta",
            "fonte": (request.GET.get("fonte") or "").strip()[:80],
            "confianca": (request.GET.get("confianca") or "").strip()[:20],
            "atualizado_desde": (request.GET.get("atualizado_desde") or "").strip(),
        }
        request.session[filtros_key] = filtros
    else:
        filtros = request.session.get(filtros_key, {})

    macros_selecionados = filtros.get("macro", [])
    categorias_selecionadas = filtros.get("categoria", [])
    loja_selecionada = filtros.get("loja", "")
    busca = filtros.get("q", "")
    tipo = filtros.get("tipo", "oferta")
    fonte_selecionada = filtros.get("fonte", "")
    confianca_selecionada = filtros.get("confianca", "")
    try:
        atualizado_desde = max(0, min(168, int(filtros.get("atualizado_desde") or 0)))
    except (TypeError, ValueError):
        atualizado_desde = 0
    try:
        min_desconto = max(0, min(100, int(float(filtros.get("min_desconto") or 0))))
    except (TypeError, ValueError):
        min_desconto = 0

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
    ordenar = "valor" if filtros.get("ordenar") == "valor" else "percent"

    from django.db.models import Q
    qs = Produto.objects.filter(preco_sem_desconto__gt=0).exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).filter(
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
    if busca:
        qs = qs.filter(
            Q(nome__icontains=busca)
            | Q(categoria__icontains=busca)
            | Q(macro_categoria__icontains=busca)
        )
    if min_desconto:
        qs = qs.filter(percent__gte=min_desconto)
    if fonte_selecionada:
        qs = qs.filter(fonte=fonte_selecionada)
    if confianca_selecionada:
        qs = qs.filter(confianca=confianca_selecionada)
    if atualizado_desde:
        qs = qs.filter(ultima_observacao__gte=timezone.now() - timezone.timedelta(hours=atualizado_desde))

    ordem = "-economia" if ordenar == "valor" else "-percent"
    produtos = list(qs.order_by(ordem)[:20])
    cupons_qs = CupomNormalizado.objects.select_related("fonte").filter(
        estado="ativo"
    ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
    if loja_selecionada:
        cupons_qs = cupons_qs.filter(marketplace=loja_selecionada)
    if fonte_selecionada:
        cupons_qs = cupons_qs.filter(fonte__slug=fonte_selecionada)
    if confianca_selecionada:
        cupons_qs = cupons_qs.filter(confianca=confianca_selecionada)
    if busca:
        cupons_qs = cupons_qs.filter(Q(titulo__icontains=busca) | Q(codigo__icontains=busca))
    cupons_catalogo = list(cupons_qs.order_by("-ultima_observacao")[:50])
    perfil = getattr(request.user, "perfil", None)
    fontes_qs = FonteIngestao.objects.filter(habilitada=True).order_by("marketplace", "nome")
    # Fontes Amazon are account-specific. Do not present an adapter that cannot
    # run for this user as an operational incident.
    if not perfil or not perfil.afiliado_tag_amazon:
        fontes_qs = fontes_qs.exclude(marketplace="amazon")
    else:
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
        if not creds_de_usuario(request.user).completo():
            fontes_qs = fontes_qs.exclude(slug="amazon-creators-api")
    fontes = list(fontes_qs)
    amazon_count = qs.filter(marketplace="amazon").count()
    if amazon_count:
        amazon_diagnostico = "Amazon ativa para sua conta."
    elif perfil and not perfil.afiliado_tag_amazon:
        amazon_diagnostico = "Cadastre sua tag de afiliado Amazon para habilitar o catálogo."
    elif perfil and perfil.amazon_elegivel is False:
        amazon_diagnostico = "Creators API inelegível; o fallback público tentará alimentar sua conta."
    else:
        amazon_diagnostico = "Nenhuma oferta Amazon confirmada no último ciclo."
    cupons_map = {
        c.campanha_id: c
        for c in Cupom.objects.filter(
            campanha_id__in=[p.campanha_id for p in produtos],
            estado="ativo",
        ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
    }
    # Marca itens já enviados POR ESTE usuário (manual OU automático): bloqueia reenvio na UI.
    ja_enviados = set(
        HistoricoEnvio.objects.filter(
            produto_id__in=[p.id for p in produtos], usuario=request.user)
        .values_list("produto_id", flat=True)
    )
    # Atribuição é regra de cada loja (ver Marketplace.preparar_exibicao). Em lote e
    # agrupado por loja: por item seria uma query por produto da página.
    from apps.scrapers.marketplaces.registry import get_marketplace
    por_loja = {}
    for p in produtos:
        por_loja.setdefault(p.marketplace, []).append(p)
    for slug, itens in por_loja.items():
        get_marketplace(slug).preparar_exibicao(itens, request.user)

    # Histórico de preço de todos os itens da página numa query só (era uma por item).
    from apps.scrapers.precos import chave_produto, stats_em_lote
    historico = stats_em_lote(produtos, dias=30)

    for p in produtos:
        p.cupom = cupons_map.get(p.campanha_id)
        p.ja_enviado = p.id in ja_enviados
        p.motivos_score = [f"{p.percent:.0f}% de desconto"]
        hist_preco = historico.get(chave_produto(p))
        if hist_preco and hist_preco["n"] >= 3 and p.preco_com_cupom <= hist_preco["minimo"] * 1.02:
            p.motivos_score.append("mínima de 30 dias")

    # base da querystring (mantém filtros ao trocar a ordenação)
    from urllib.parse import urlencode
    qs_pairs = [("macro", m) for m in macros_selecionados] + [("categoria", c) for c in categorias_selecionadas]
    if busca:
        qs_pairs.append(("q", busca))
    if min_desconto:
        qs_pairs.append(("min_desconto", min_desconto))
    qs_pairs.append(("tipo", tipo))
    if fonte_selecionada:
        qs_pairs.append(("fonte", fonte_selecionada))
    if confianca_selecionada:
        qs_pairs.append(("confianca", confianca_selecionada))
    if atualizado_desde:
        qs_pairs.append(("atualizado_desde", atualizado_desde))
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
        "busca": busca,
        "min_desconto": min_desconto,
        "tipo": tipo,
        "fontes": fontes,
        "fonte_selecionada": fonte_selecionada,
        "confianca_selecionada": confianca_selecionada,
        "atualizado_desde": atualizado_desde,
        "cupons_catalogo": cupons_catalogo,
        "amazon_diagnostico": amazon_diagnostico,
        "filtros_ativos": len(macros_selecionados) + len(categorias_selecionadas)
            + bool(loja_selecionada) + bool(busca) + bool(min_desconto)
            + bool(fonte_selecionada) + bool(confianca_selecionada) + bool(atualizado_desde),
        "qs_base": qs_base,
        "qs_base_sem_loja": qs_base_sem_loja,
    })


@staff_required
@require_GET
@throttle_sse(10)
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


@staff_required
@require_GET
@throttle_sse(10)
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

    # Fora da thread de propósito: _run roda noutra thread e não pode tocar request.
    usuario = request.user

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
                            # O pool é compartilhado, mas a sessão do ML é do usuário:
                            # sem isto, lia-se um auth.json que a tela nunca grava.
                            gerar_links_em_lote(pendentes, usuario=usuario)
                        from apps.scrapers.afiliado import frase_resumo_afiliacao
                        print(frase_resumo_afiliacao(usuario))
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

    # Os workers (scrape/envio) são loops GLOBAIS compartilhados por todos os tenants:
    # ligar/desligar afeta todo mundo, então é controle de infra (staff). O usuário
    # comum liga/desliga o PRÓPRIO envio pelo flag `ativo` de cada regra, sem derrubar
    # o worker dos demais. GET (status) segue liberado para o polling do front.
    if request.method == "POST" and not request.user.is_staff:
        raise PermissionDenied("Apenas administradores controlam os workers de automação.")

    if request.method != "POST":
        habilitada = st.is_enabled(tipo)
        worker_vivo = st.worker_alive(tipo)
        estado = st.read_state(tipo) if habilitada else {}
        # O estado é global e pode conter diagnóstico gravado por versões antigas.
        # Nunca exponha traceback, caminhos do servidor ou detalhes do banco no UI.
        if estado.get("erro"):
            estado = {**estado, "erro": "Falha temporária no serviço. Uma nova tentativa será feita no próximo ciclo."}
        fase = estado.get("fase", "")
        degradada = fase == "degradado" or bool(estado.get("erro"))
        saudavel = habilitada and worker_vivo and not degradada
        return JsonResponse({
            # Compatibilidade com os clientes antigos: rodando agora significa
            # que o loop foi habilitado E há heartbeat recente.
            "rodando": habilitada and worker_vivo,
            "habilitada": habilitada,
            "worker_vivo": worker_vivo,
            "saudavel": saudavel,
            "tipo": tipo,
            "estado": estado,
        })

    # As telas de Scraper/Envios chamam por fetch e leem o JSON. A Saúde usa um form
    # comum (sem JS), então precisa voltar para uma página: com `next`, redireciona.
    def _responder(payload, msg_ok):
        destino = request.POST.get("next")
        if destino and url_has_allowed_host_and_scheme(
                destino, allowed_hosts={request.get_host()},
                require_https=request.is_secure()):
            messages.success(request, msg_ok)
            return redirect(destino)
        return JsonResponse(payload)

    acao = request.POST.get("acao")
    if acao == "stop":
        st.parar(tipo)
        return _responder({"rodando": False, "tipo": tipo, "msg": "Parado."},
                          f"Worker '{tipo}' desligado.")

    # start — liga o flag; garante que exista um worker. Em prod (honcho) o worker
    # já roda (heartbeat fresco) e o spawn é no-op; em dev (runserver) sobe um
    # subprocess destacado cross-platform. O loop trabalha no próximo ciclo.
    if st.is_running(tipo):
        st.spawn_worker(tipo)  # religa o worker se tiver morrido (dev)
        return _responder({"rodando": True, "tipo": tipo, "msg": "Já estava ligado."},
                          f"Worker '{tipo}' já estava ligado.")
    st.iniciar(tipo)
    st.spawn_worker(tipo)
    return _responder({"rodando": True, "tipo": tipo, "msg": "Ligado."},
                      f"Worker '{tipo}' ligado. O primeiro ciclo roda em instantes.")


@staff_required
@require_GET
@throttle_sse(10)
def scrape_cupons_codigo_stream(request):
    """SSE — raspa /ofertas/cupons (produtos + códigos de checkout)."""
    from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
    return _sse_runner(mapear_cupons_codigo)


@require_GET
@throttle_sse(6)
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


@staff_required
@require_GET
@throttle_sse(10)
def gerar_links_stream(request):
    """SSE endpoint — gera links de afiliado em lote para produtos sem link."""
    from apps.scrapers.marketplaces.registry import get_marketplace

    try:
        limite = int(request.GET.get("limite", 50))
    except (TypeError, ValueError):
        limite = 50

    # Fora da thread de propósito: _run roda noutra thread e não pode tocar request.
    usuario = request.user

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    # Pendente é por USUÁRIO: cada um afilia com a conta dele, então o
                    # link vive em LinkAfiliadoUsuario e não no Produto. Filtrar por
                    # link_afiliado="" (campo global) listava como pendente item que
                    # este usuário já tem, e como pronto item que ele não tem.
                    ja_tem = LinkAfiliadoUsuario.objects.filter(
                        usuario=usuario, produto=OuterRef("pk")).exclude(link_afiliado="")
                    base_qs = (
                        Produto.objects
                        .filter(Q(owner__isnull=True) | Q(owner=usuario))
                        .exclude(Exists(ja_tem))
                    )
                    pendentes = list(base_qs[:limite])
                    restantes = base_qs.count()
                    print(f"{restantes} produto(s) sem link pronto. Gerando até {limite}...")
                    # Agrupa por loja: cada marketplace gera seus links (ML=Playwright,
                    # Amazon=puro Python). Evita rodar o Link Builder do ML num ASIN.
                    if pendentes:
                        por_loja = {}
                        for p in pendentes:
                            por_loja.setdefault(p.marketplace or "mercadolivre", []).append(p)
                        for slug, grupo in por_loja.items():
                            get_marketplace(slug).prefetch_links(grupo, usuario=usuario)
                    from apps.scrapers.afiliado import frase_resumo_afiliacao
                    print(frase_resumo_afiliacao(usuario))
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
