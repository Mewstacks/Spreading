import asyncio
import logging
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
from django.core.paginator import Paginator
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
    IntegracaoAfiliado, ProgramaAfiliado,
)
from apps.scrapers.progresso import emitir_fase
from apps.scrapers.scraper_mercadolivre.scraper import main as scrapper_main

logger = logging.getLogger(__name__)


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


# Espelha as heurísticas de causa de ofertas.falhar(): o texto cru da exceção fica
# em Publicacao.erro (admin/Saúde); na home entra só a versão para o usuário.
_ERROS_PUBLICACAO = [
    (("link de afiliado", "link builder"),
     "Não foi possível gerar o link de afiliado — verifique a conexão com a loja."),
    (("link reprovado",), "O link foi reprovado na verificação de afiliação."),
    (("módulos internos", "recarregando", "frame"),
     "O WhatsApp Web recarregou durante o envio."),
    (("timeout", "demorou"), "O WhatsApp demorou para responder ao envio."),
    (("confirma", "ack"), "O envio saiu, mas não veio confirmação do WhatsApp."),
    (("login", "sessão", "sessao"), "A sessão da loja expirou — reconecte na aba Conta."),
]


def _erro_publicacao(texto):
    t = (texto or "").lower()
    if not t:
        return ""
    for chaves, msg in _ERROS_PUBLICACAO:
        if any(c in t for c in chaves):
            return msg
    return "Falha no envio — verifique as conexões se persistir."


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
    )
    # Snapshot mais recente por loja, não Sum de 30 dias: ver resumo_financeiro.
    financeiro = resumo_financeiro(request.user)
    comissao = financeiro.get("comissao") or 0
    posts = resumo.get("enviados") or 0
    financeiro["comissao_por_post"] = comissao / posts if posts else 0
    # Envios primeiro: com o link direto da loja na mensagem, cliques internos
    # pararam de contar — ordenar por eles fossilizaria o ranking no legado.
    melhores_categorias = list(
        pubs.filter(status="enviado").values("categoria")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-envios", "-cliques")[:5]
    )
    melhores_destinos = list(
        pubs.filter(status="enviado").values("destino_nome", "destino_id")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-envios", "-cliques")[:5]
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
    # "conectando" é o worker religando após deploy — piscar "Nenhum canal
    # conectado" nesses segundos assustava sem haver o que fazer.
    if not wa_ok and est_wa.detalhe != "conectando" and not perfil.telegram_conectado():
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
                sync.erro_publico or "Sincronização automática não concluiu.",
                # Reconectar a conta é o que resolve; "home" apontava pra esta mesma
                # página, então clicar no alerta não levava a lugar nenhum.
                "scraper-conta",
            ))
    publicacoes = list(
        pubs.select_related("produto", "configuracao", "cupom_normalizado")
        .order_by("-criada_em")[:10])
    for p in publicacoes:
        p.erro_publico = _erro_publicacao(p.erro)
    return render(request, "home.html", {
        "resumo": resumo, "financeiro": financeiro,
        "melhores_categorias": melhores_categorias,
        "melhores_destinos": melhores_destinos,
        "publicacoes": publicacoes,
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

    from apps.scrapers.conexoes import estado_amazon_relatorios, estado_ml_relatorios
    awin_integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    return render(request, "scrapers/conta.html", {
        "perfil": perfil,
        "tem_secret": bool(perfil and perfil.amazon_credential_secret),
        "ml_sessao_ok": _tem_sessao_ml(request.user),
        "amazon_conectado": bool(perfil and perfil.amazon_conectado()),
        # Ortogonal ao "conectado": a Creators API é upgrade opcional, exibido só
        # como informação. Não pode voltar a virar requisito de conexão.
        "amazon_creators_ativa": bool(perfil and perfil.amazon_creators_ativa()),
        "amazon_relatorio_conectado": bool(estado_amazon_relatorios(request.user).conectado),
        "ml_relatorio_conectado": bool(estado_ml_relatorios(request.user).conectado),
        "billing_checkout_url": settings.BILLING_CHECKOUT_URL,
        "billing_portal_url": settings.BILLING_PORTAL_URL,
        "awin_enabled": getattr(settings, "AWIN_INTEGRATION_ENABLED", False),
        "awin_integracao": awin_integracao,
        "awin_programas": list(awin_integracao.programas.order_by("nome"))
        if awin_integracao else [],
        # Mantém a escolha disponível após refresh/erro de formulário; ela é
        # removida somente quando uma conta é efetivamente selecionada.
        "awin_contas": request.session.get("awin_contas", []),
    })


@require_POST
def awin_conectar(request):
    if not getattr(settings, "AWIN_INTEGRATION_ENABLED", False):
        return JsonResponse({"erro": "Integração Awin indisponível."}, status=404)
    from apps.scrapers.awin import AwinError, listar_contas, sincronizar_integracao
    token = (request.POST.get("token") or "").strip()
    if len(token) < 20:
        messages.error(request, "Cole um token Awin válido.")
        return redirect("scraper-conta")
    try:
        contas = listar_contas(token)
    except AwinError as exc:
        messages.error(request, exc.public_message)
        return redirect("scraper-conta")
    integracao, _ = IntegracaoAfiliado.objects.get_or_create(
        owner=request.user, provedor="awin")
    integracao.token = token
    integracao.habilitada = True
    integracao.status = "pendente"
    integracao.erro_publico = ""
    integracao.save(update_fields=["token", "habilitada", "status", "erro_publico"])
    if len(contas) > 1:
        request.session["awin_contas"] = contas
        messages.info(request, "Token validado. Escolha qual conta Publisher usar.")
        return redirect("scraper-conta")
    conta = contas[0]
    integracao.identificador_conta = conta["id"]
    integracao.nome_conta = conta["nome"]
    integracao.status = "conectada"
    integracao.save(update_fields=["identificador_conta", "nome_conta", "status"])
    try:
        sincronizar_integracao(integracao, forcar_programas=True)
        messages.success(request, "Awin conectada e sincronizada.")
    except AwinError as exc:
        messages.warning(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_selecionar_conta(request):
    from apps.scrapers.awin import AwinError, listar_contas, sincronizar_integracao
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    if not integracao or not integracao.token:
        messages.error(request, "Conecte a Awin novamente.")
        return redirect("scraper-conta")
    selected = (request.POST.get("publisher_id") or "").strip()
    try:
        conta = next((c for c in listar_contas(integracao.token) if c["id"] == selected), None)
        if not conta:
            raise AwinError("A conta escolhida não pertence a este token.")
        integracao.identificador_conta = conta["id"]
        integracao.nome_conta = conta["nome"]
        integracao.status = "conectada"
        integracao.habilitada = True
        integracao.save(update_fields=[
            "identificador_conta", "nome_conta", "status", "habilitada"])
        request.session.pop("awin_contas", None)
        sincronizar_integracao(integracao, forcar_programas=True)
        messages.success(request, "Conta Awin selecionada e sincronizada.")
    except AwinError as exc:
        messages.error(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_sincronizar(request):
    from apps.scrapers.awin import AwinError, sincronizar_integracao
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin", habilitada=True).first()
    if not integracao:
        messages.error(request, "Awin não conectada.")
    else:
        try:
            result = sincronizar_integracao(integracao, forcar_programas=True)
            messages.success(request, f"Awin sincronizada: {result['coupons']} campanha(s).")
        except AwinError as exc:
            messages.error(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_programa_toggle(request, programa_id):
    programa = ProgramaAfiliado.objects.filter(
        pk=programa_id, integracao__owner=request.user,
        integracao__provedor="awin").first()
    if not programa:
        raise PermissionDenied("Programa não pertence a esta conta.")
    programa.habilitado = not programa.habilitado
    programa.save(update_fields=["habilitado"])
    messages.success(request, f"{programa.nome}: {'ativo' if programa.habilitado else 'pausado'}.")
    return redirect("scraper-conta")


@require_POST
def awin_desconectar(request):
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    if integracao:
        integracao.token = ""
        integracao.habilitada = False
        integracao.status = "desativada"
        integracao.proxima_sincronizacao = None
        integracao.erro_publico = ""
        integracao.save(update_fields=[
            "token", "habilitada", "status", "proxima_sincronizacao", "erro_publico"])
        CupomNormalizado.objects.filter(owner=request.user, integracao=integracao).update(
            estado="inativo")
    messages.success(request, "Awin desconectada. O histórico foi preservado.")
    return redirect("scraper-conta")


def _data_form_aware(value, *, fim=False):
    from datetime import datetime, time
    from django.utils.dateparse import parse_date, parse_datetime
    raw = (value or "").strip()
    parsed = parse_datetime(raw)
    if parsed is None:
        day = parse_date(raw)
        if day:
            parsed = datetime.combine(day, time(23, 59, 59) if fim else time.min)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _url_manual_valida(marketplace, url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if marketplace == "mercadolivre":
        return host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")
    if marketplace == "amazon":
        return host == "amazon.com.br" or host.endswith(".amazon.com.br")
    return marketplace == "awin"


@require_POST
def cupom_manual_salvar(request, cupom_id=None):
    import uuid
    from apps.scrapers.awin import AwinError, gerar_deeplink, url_permitida
    from apps.scrapers.coupon_rules import derivar_categoria_cupom, normalizar_regras_cupom

    coupon = None
    if cupom_id:
        coupon = CupomNormalizado.objects.filter(
            pk=cupom_id, owner=request.user, fonte__slug="manual-private").first()
        if not coupon:
            raise PermissionDenied("Cupom não pertence a esta conta.")
    marketplace = (request.POST.get("marketplace") or "").strip().lower()
    if marketplace not in {"mercadolivre", "amazon", "awin"}:
        messages.error(request, "Escolha uma loja conectada.")
        return redirect("scraper-top")
    original_url = (request.POST.get("url") or "").strip()
    if not _url_manual_valida(marketplace, original_url):
        messages.error(request, "Informe uma URL HTTPS válida da loja selecionada.")
        return redirect("scraper-top")
    code = (request.POST.get("codigo") or "").strip()[:120]
    title = (request.POST.get("titulo") or "").strip()[:255]
    if not title:
        messages.error(request, "Informe um título para o cupom.")
        return redirect("scraper-top")
    integration = program = None
    affiliate_url = original_url
    state = "ativo"
    if marketplace == "awin":
        try:
            program_id = int(request.POST.get("programa") or 0)
        except (TypeError, ValueError):
            program_id = 0
        program = ProgramaAfiliado.objects.select_related("integracao").filter(
            pk=program_id, integracao__owner=request.user,
            integracao__provedor="awin", habilitado=True,
            status_vinculo="joined", link_status="online").first()
        if not program or not url_permitida(program, original_url):
            messages.error(request, "A URL não pertence ao anunciante Awin escolhido.")
            return redirect("scraper-top")
        integration = program.integracao
        try:
            affiliate_url = gerar_deeplink(integration, program, original_url)
        except AwinError as exc:
            state = "rascunho"
            messages.warning(request, f"Cupom salvo como rascunho: {exc.public_message}")

    source, _ = FonteIngestao.objects.get_or_create(
        slug="manual-private",
        defaults={"marketplace": "multiloja", "nome": "Cupons privados do afiliado",
                  "status": "ok", "habilitada": True})
    rules = normalizar_regras_cupom({
        "tipo_desconto": request.POST.get("tipo_desconto"),
        "valor_desconto": request.POST.get("valor_desconto"),
        "valor_minimo": request.POST.get("valor_minimo"),
        "desconto_maximo": request.POST.get("desconto_maximo"),
        "modo_resgate": "codigo" if code else "ativacao",
        "escopo": (request.POST.get("condicoes") or "").strip()[:500],
        "dia_inicio": request.POST.get("inicio"), "dia_fim": request.POST.get("validade"),
    }, external_id=coupon.external_id if coupon else "manual", codigo=code)
    values = {
        "owner": request.user, "integracao": integration, "programa": program,
        "marketplace": marketplace, "tipo_conteudo": "voucher" if code else "promotion",
        "anunciante_nome": program.nome if program else (
            "Mercado Livre" if marketplace == "mercadolivre" else "Amazon"),
        "titulo": title, "codigo": code, "regras": rules,
        "categoria": derivar_categoria_cupom(title, rules), "link": affiliate_url[:1000],
        "inicio": _data_form_aware(request.POST.get("inicio")),
        "validade": _data_form_aware(request.POST.get("validade"), fim=True),
        "restrito": bool(request.POST.get("restrito")),
        "relampago": bool(request.POST.get("relampago")), "estado": state,
        "confianca": "media", "evidencia": {"manual": True, "url_original": original_url},
    }
    if coupon:
        for field, value in values.items():
            setattr(coupon, field, value)
        coupon.save()
    else:
        coupon = CupomNormalizado.objects.create(
            fonte=source, external_id=f"manual:{uuid.uuid4().hex}", **values)
    from apps.scrapers.coupon_products import atualizar_chave_cupom
    atualizar_chave_cupom(coupon)
    if state == "ativo":
        messages.success(request, "Cupom salvo e enviado para validação automática de produtos.")
    return redirect("scraper-top")


@require_POST
def cupom_manual_desativar(request, cupom_id):
    updated = CupomNormalizado.objects.filter(
        pk=cupom_id, owner=request.user, fonte__slug="manual-private").update(estado="inativo")
    if not updated:
        raise PermissionDenied("Cupom não pertence a esta conta.")
    messages.success(request, "Cupom privado desativado.")
    return redirect("scraper-top")


def _wa_session(request):
    """Sessão WhatsApp DESTE usuário (multi-tenant). Cada um pareia a própria conta."""
    perfil = getattr(request.user, "perfil", None)
    return perfil.sessao_whatsapp() if perfil else str(request.user.id)


def whatsapp_painel(request):
    """Tela de conexão do WhatsApp: status + QR Code para parear pelo navegador.

    O GET não revive a sessão: consultar não pode ter efeito colateral (era a
    metade "otimista" da divergência com a Saúde). Reviver segue existindo, mas
    como intenção explícita: o front dá POST em whatsapp/iniciar/ quando vê uma
    fase terminal.
    """
    from apps.scrapers import whatsapp_client
    session = _wa_session(request)
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
def whatsapp_iniciar(request):
    """Revive/inicia a sessão de WhatsApp deste usuário.

    POST /api/sessoes é o único caminho que tira uma sessão de fase terminal no
    worker Node (expirado, falha_auth, recuperacao_pausada, ausente do Map).
    Antes isso acontecia como efeito colateral do GET da tela — o que a tornava
    otimista e divergente da Saúde. POST espelha whatsapp_desconectar (CSRF).
    """
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.iniciar_sessao(_wa_session(request)))


@require_POST
def whatsapp_desconectar(request):
    """Desfaz o pareamento do WhatsApp deste usuário (espelha telegram_desconectar)."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.desconectar(_wa_session(request)))


@require_POST
def whatsapp_cancelar_reconexao(request):
    """Aborta a recuperação em curso e começa do zero, com QR novo.

    Saída manual do loop de reconexão: o worker tenta 6 vezes, purga a
    credencial, tenta de novo e para numa fase terminal — e cada F5 na tela
    reviveu esse mesmo ciclo. Sem este botão o usuário não tinha como interromper
    (o "Desconectar" só aparece conectado, que é justamente o estado que falta).

    O worker executa a transição atomicamente para que o polling não consiga
    reviver a credencial antiga entre a limpeza e a criação da sessão nova.
    """
    from apps.scrapers import whatsapp_client
    session = _wa_session(request)
    return JsonResponse(whatsapp_client.reiniciar_com_qr(session))


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


# --- Conexão do portal de RELATÓRIOS do ML (afiliados), separada do site principal ---

def ml_relatorio_conexao_painel(request):
    """Login interativo no portal de afiliados do ML, exclusivo para relatórios."""
    from apps.scrapers import ml_relatorio_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": ml_relatorio_conexao.status(request.user.id),
        "marketplace_nome": "Relatórios Mercado Livre",
        "conexao_prefix": "/scrapers/ml-relatorio",
        "relatorio": True,
    })


@require_GET
def ml_relatorio_conexao_status_json(request):
    from apps.scrapers import ml_relatorio_conexao
    return JsonResponse(ml_relatorio_conexao.status(request.user.id))


@require_POST
def ml_relatorio_conexao_start(request):
    from apps.scrapers import ml_relatorio_conexao
    return JsonResponse(ml_relatorio_conexao.criar_sessao(request.user))


@require_POST
def ml_relatorio_conexao_salvar(request):
    from apps.scrapers import ml_relatorio_conexao
    ml_relatorio_conexao.salvar_agora(request.user.id)
    return JsonResponse(ml_relatorio_conexao.status(request.user.id))


@require_POST
def ml_relatorio_conexao_cancelar(request):
    from apps.scrapers import ml_relatorio_conexao
    ml_relatorio_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


@require_GET
def ml_relatorio_conexao_frames(request):
    from apps.scrapers import ml_relatorio_conexao
    def _stream():
        yield from (f"data: {frame}\n\n" for frame in ml_relatorio_conexao.frames(request.user.id))
        yield "data: __DONE__\n\n"
    return StreamingHttpResponse(_stream(), content_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@require_POST
def ml_relatorio_conexao_input(request):
    import json
    from apps.scrapers import ml_relatorio_conexao
    try:
        events = json.loads((request.body or b"").decode() or "{}").get("events")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_relatorio_conexao.enfileirar_input(request.user.id, events))


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
                incluir_restritos=bool(request.POST.get("incluir_restritos")),
                incluir_sem_desconto=bool(request.POST.get("incluir_sem_desconto")),
                ativo=bool(request.POST.get("ativo")),
            )
            program_ids = list(ProgramaAfiliado.objects.filter(
                id__in=request.POST.getlist("programas"),
                integracao__owner=request.user, habilitado=True,
            ).values_list("id", flat=True))
            if cfg_id:
                # update() não dispara validação, mas o filtro por owner garante posse.
                ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).update(**campos)
                cfg_obj = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).first()
                if cfg_obj:
                    cfg_obj.programas.set(program_ids)
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
                cfg_obj = ConfiguracaoEnvio.objects.create(owner=request.user, **campos)
                cfg_obj.programas.set(program_ids)
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

    configs_qs = ConfiguracaoEnvio.objects.filter(owner=request.user).prefetch_related(
        "programas").order_by("macro_categoria")
    configs = list(configs_qs)
    from apps.scrapers.content_ranking import previa_melhor_conteudo
    for config in configs:
        config.previa_conteudo = previa_melhor_conteudo(config) if config.ativo else None
    return render(request, "scrapers/configuracoes.html", {
        "configs": configs,
        "macros": macros,
        "subnichos": subnichos,
        "marketplaces": list(MARKETPLACES.keys()),
        "canais": list(SENDERS.keys()),
        "perfil": request.user.perfil,
        "awin_programas": ProgramaAfiliado.objects.filter(
            integracao__owner=request.user, integracao__status="conectada",
            habilitado=True, status_vinculo="joined", link_status="online").order_by("nome"),
    })


@require_POST
@throttle_sse(6)
def enviar_agora_stream(request):
    """SSE via POST — dispara um envio de teste para uma ConfiguracaoEnvio."""
    from apps.scrapers.ofertas import selecionar_e_enviar

    try:
        cfg_id = int(request.POST.get("config") or 0)
    except (TypeError, ValueError):
        cfg_id = 0
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
                logger.exception("Falha inesperada no envio de teste")
                q.put("[ERRO] Falha inesperada ao preparar o envio.")
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


def _imagem_upload_b64(arquivo, max_bytes=5 * 1024 * 1024):
    """Foto anexada no envio -> base64 JPEG, ou None. Valida decodificando via PIL
    (rejeita não-imagem) e reconverte p/ JPEG (formato que o worker aceita)."""
    if not arquivo:
        return None
    try:
        if getattr(arquivo, "size", 0) and arquivo.size > max_bytes:
            return None
        import base64
        from io import BytesIO
        from PIL import Image
        img = Image.open(arquivo).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


@require_POST
@throttle_sse(6)
def enviar_produto_stream(request):
    """SSE — envia UM produto específico (tela Promoções) p/ o destino escolhido no popup.

    Reusa enviar_oferta_de_produto -> grava HistoricoEnvio em sucesso, então o item
    fica permanentemente bloqueado p/ o envio automático (anti-repetição global).
    """
    from apps.scrapers.ofertas import enviar_oferta_de_produto
    try:
        prod_id = int(request.POST.get("produto") or 0)
    except (TypeError, ValueError):
        prod_id = 0
    grupo_id = (request.POST.get("grupo") or "").strip()[:100]
    grupo_nome = (request.POST.get("grupo_nome") or "").strip()[:255]
    canal = (request.POST.get("canal") or "whatsapp").strip().lower()
    # Foto opcional: lida AQUI (presa ao request), fora da thread _job.
    imagem_custom = _imagem_upload_b64(request.FILES.get("foto"))
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        usuario = get_user_model().objects.filter(id=uid).first()
        if not usuario:
            print("[ERRO] Usuário não encontrado ou sessão encerrada.")
            return
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
        print(f"Enviando '{prod.nome[:60]}' → {grupo_nome or grupo_id} ({canal})...")
        r = enviar_oferta_de_produto(
            prod, grupo_id, verificar=True, canal=canal, usuario=usuario,
            destino_nome=grupo_nome, imagem_b64_custom=imagem_custom)
        if r.get("sucesso"):
            print(f"__SENT__ OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
        else:
            print(f"[ERRO] {r.get('motivo')}")
            if r.get("precisa_login_ml"):
                print("__ML_LOGIN__")  # a UI troca por um botão "Reconectar Mercado Livre"
            elif r.get("precisa_login_wa"):
                print("__WA_LOGIN__")  # a UI troca por "Reconectar WhatsApp"

    return _sse_runner(_job)


@require_POST
@throttle_sse(6)
def enviar_cupom_stream(request):
    """SSE — envia um cupom afiliado, auditado e deduplicado por 24 horas."""
    from apps.scrapers.ofertas import enviar_cupom

    try:
        cupom_id = int(request.POST.get("cupom") or 0)
    except (TypeError, ValueError):
        cupom_id = 0
    grupo_id = (request.POST.get("grupo") or "").strip()[:100]
    grupo_nome = (request.POST.get("grupo_nome") or "").strip()[:255]
    canal = (request.POST.get("canal") or "whatsapp").strip().lower()
    imagem_custom = _imagem_upload_b64(request.FILES.get("foto"))
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.coupon_rules import codigo_publicavel
        usuario = get_user_model().objects.filter(id=uid).first()
        if not usuario:
            print("[ERRO] Usuário não encontrado ou sessão encerrada.")
            return
        if not grupo_id:
            print("[ERRO] Nenhum destino informado (grupo/chat).")
            return
        cupom = CupomNormalizado.objects.filter(
            Q(owner__isnull=True) | Q(owner=usuario), id=cupom_id, estado="ativo").first()
        if not cupom:
            print("[ERRO] Cupom não encontrado ou inativo.")
            return
        rotulo = codigo_publicavel(cupom) or "Ativar no link"
        print(f"Enviando cupom '{rotulo}' → {grupo_nome or grupo_id} ({canal})...")
        resultado = enviar_cupom(
            cupom, grupo_id, canal=canal, usuario=usuario, destino_nome=grupo_nome,
            imagem_b64_custom=imagem_custom)
        if resultado.get("sucesso"):
            print(f"__SENT__ OK Cupom enviado (via {resultado.get('via', canal)}).")
        else:
            print(f"[ERRO] {resultado.get('motivo') or 'falha ao enviar o cupom'}")
            if resultado.get("precisa_login_ml"):
                print("__ML_LOGIN__")  # a UI troca por "Reconectar Mercado Livre"
            elif resultado.get("precisa_login_wa"):
                print("__WA_LOGIN__")  # a UI troca por "Reconectar WhatsApp"

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


# Itens por tela em Promoções (ofertas e cupons).
POR_PAGINA = 20


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
                      "tipo", "fonte", "confianca", "atualizado_desde", "afiliado",
                      "categoria_cupom", "como_usar", "anunciante")
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
            # Filtros exclusivos da aba Cupons.
            "categoria_cupom": (request.GET.get("categoria_cupom") or "").strip()[:100],
            "anunciante": (request.GET.get("anunciante") or "").strip()[:100],
            "como_usar": (request.GET.get("como_usar")
                          if request.GET.get("como_usar") in ("codigo", "ativacao")
                          else ""),
            # Default = só afiliados: enviar item sem link de afiliado não comissiona.
            # "todos" existe só para diagnóstico (ver o que está travado na fila).
            "afiliado": "todos" if request.GET.get("afiliado") == "todos" else "prontos",
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
    categoria_cupom_selecionada = filtros.get("categoria_cupom", "")
    anunciante_selecionado = filtros.get("anunciante", "")
    como_usar_selecionado = filtros.get("como_usar", "")
    so_afiliados = filtros.get("afiliado", "prontos") != "todos"
    try:
        atualizado_desde = max(0, min(168, int(filtros.get("atualizado_desde") or 0)))
    except (TypeError, ValueError):
        atualizado_desde = 0
    try:
        min_desconto = max(0, min(100, int(float(filtros.get("min_desconto") or 0))))
    except (TypeError, ValueError):
        min_desconto = 0

    # A página vem só da URL, nunca da sessão de filtros: guardá-la faria uma
    # visita nova cair na página 12 de uma lista que já mudou. Fora de
    # `tem_filtros_na_url` de propósito — paginar não pode reescrever os filtros.
    pagina = request.GET.get("pagina")

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
    # A afiliação é resolvida antes da paginação. Não pode haver um corte de ranking
    # aqui: ele fazia a tela anunciar centenas de links prontos no resumo, mas
    # paginava somente os afiliados que por acaso estivessem entre os 200 maiores
    # descontos. O conjunto cabe em memória e cada marketplace resolve os links em
    # lote, portanto todos os produtos afiliados podem entrar na paginação.
    candidatos = list(qs.order_by(ordem))
    cupons_visiveis = Q(owner__isnull=True) | Q(owner=request.user)
    cupons_qs = CupomNormalizado.objects.select_related(
        "fonte", "integracao", "programa").filter(
        cupons_visiveis, estado="ativo"
    ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
    if loja_selecionada:
        cupons_qs = cupons_qs.filter(marketplace=loja_selecionada)
    if fonte_selecionada:
        cupons_qs = cupons_qs.filter(fonte__slug=fonte_selecionada)
    # Confiança não é mais filtrável na aba Cupons (todos são "media"); ignora um
    # valor herdado da aba Ofertas para não zerar a lista sem querer.
    if categoria_cupom_selecionada:
        cupons_qs = cupons_qs.filter(categoria=categoria_cupom_selecionada)
    if anunciante_selecionado:
        cupons_qs = cupons_qs.filter(anunciante_nome=anunciante_selecionado)
    if busca:
        cupons_qs = cupons_qs.filter(Q(titulo__icontains=busca) | Q(codigo__icontains=busca))
    # "Como usar" (código vs. ativar no link) vem da normalização de `regras`, não
    # de coluna — então materializa, calcula por cupom e filtra em Python, igual ao
    # corte de afiliação das ofertas. O conjunto de cupons ativos é pequeno.
    from apps.scrapers.coupon_rules import (
        codigo_publicavel, cupom_publicavel, regras_do_cupom, score_cupom,
    )
    # Base por recência (desempate estável); depois ordena por qualidade do cupom —
    # feedback da cliente: bons cupons vendem mais, então os melhores vêm primeiro.
    cupons_lista = list(cupons_qs.order_by("-ultima_observacao"))
    for cupom_catalogo in cupons_lista:
        cupom_catalogo.codigo_publico = codigo_publicavel(cupom_catalogo)
        cupom_catalogo.modo_resgate = regras_do_cupom(cupom_catalogo)["modo_resgate"]
    from apps.scrapers.coupon_products import ids_cupons_prontos
    ids_prontos = ids_cupons_prontos(request.user, cupons_lista)
    cupons_lista = [
        c for c in cupons_lista if c.id in ids_prontos and cupom_publicavel(c)
    ]
    # Os filtros tambem devem refletir somente o catalogo realmente publicavel.
    cupom_categorias = sorted({c.categoria for c in cupons_lista if c.categoria})
    cupom_anunciantes = sorted({c.anunciante_nome for c in cupons_lista
                                if c.anunciante_nome})
    cupons_lista.sort(key=score_cupom, reverse=True)
    if como_usar_selecionado == "codigo":
        cupons_lista = [c for c in cupons_lista if c.codigo_publico]
    elif como_usar_selecionado == "ativacao":
        cupons_lista = [c for c in cupons_lista if not c.codigo_publico]
    cupons_page = Paginator(cupons_lista, POR_PAGINA).get_page(pagina)
    cupons_catalogo = list(cupons_page)
    perfil = getattr(request.user, "perfil", None)
    fontes_qs = FonteIngestao.objects.filter(habilitada=True).exclude(
        slug="manual-private").order_by("marketplace", "nome")
    # Fontes Amazon are account-specific. Do not present an adapter that cannot
    # run for this user as an operational incident.
    if not perfil or not perfil.afiliado_tag_amazon:
        fontes_qs = fontes_qs.exclude(marketplace="amazon")
    else:
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
        if not creds_de_usuario(request.user).completo():
            fontes_qs = fontes_qs.exclude(slug="amazon-creators-api")
    fontes = list(fontes_qs)
    from apps.scrapers.afiliado import resumo_afiliacao
    afiliacao = resumo_afiliacao(request.user)
    afiliacao_ultimo_erro = (
        LinkAfiliadoUsuario.objects
        .filter(usuario=request.user, estado="erro")
        .exclude(ultimo_erro="")
        .order_by("-ultima_tentativa", "-id")
        .values_list("ultimo_erro", flat=True)
        .first()
    ) or ""
    amazon_count = qs.filter(marketplace="amazon").count()
    if amazon_count:
        amazon_diagnostico = "Amazon ativa para sua conta."
    elif perfil and not perfil.afiliado_tag_amazon:
        amazon_diagnostico = "Cadastre sua tag de afiliado Amazon para habilitar o catálogo."
    elif perfil and perfil.amazon_elegivel is False:
        amazon_diagnostico = "Creators API inelegível; o fallback público tentará alimentar sua conta."
    else:
        amazon_diagnostico = "Nenhuma oferta Amazon confirmada no último ciclo."
    # Atribuição é regra de cada loja (ver Marketplace.preparar_exibicao). Em lote e
    # agrupado por loja: por item seria uma query por produto da página.
    from apps.scrapers.marketplaces.registry import get_marketplace
    por_loja = {}
    for p in candidatos:
        por_loja.setdefault(p.marketplace, []).append(p)
    for slug, itens in por_loja.items():
        get_marketplace(slug).preparar_exibicao(itens, request.user)

    # O corte só acontece AQUI: item sem link de afiliado não vai para a tela de
    # envio, porque enviá-lo não comissiona nada. O filtro é em Python, e não em SQL,
    # de propósito — `afiliado_pronto` é contrato de cada loja (na Amazon sai de tag +
    # ASIN, não de linha no banco), então só `preparar_exibicao` sabe respondê-lo.
    pendentes_ocultos = 0
    if so_afiliados:
        prontos = [p for p in candidatos if getattr(p, "afiliado_pronto", False)]
        pendentes_ocultos = len(candidatos) - len(prontos)
        candidatos = prontos
    # Pagina a lista já materializada, e não o queryset: `afiliado_pronto` é
    # decidido em Python (acima), então uma paginação em SQL contaria itens que a
    # tela nunca mostra. As queries por item abaixo continuam recebendo só a
    # página corrente.
    page_obj = Paginator(candidatos, POR_PAGINA).get_page(pagina)
    produtos = list(page_obj)

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
    if categoria_cupom_selecionada:
        qs_pairs.append(("categoria_cupom", categoria_cupom_selecionada))
    if anunciante_selecionado:
        qs_pairs.append(("anunciante", anunciante_selecionado))
    if como_usar_selecionado:
        qs_pairs.append(("como_usar", como_usar_selecionado))
    # base p/ o chip de afiliação: preserva o resto dos filtros e troca só ele.
    qs_base_sem_afiliado = urlencode(qs_pairs + (
        [("loja", loja_selecionada)] if loja_selecionada else []))
    qs_base_sem_afiliado = (qs_base_sem_afiliado + "&") if qs_base_sem_afiliado else ""
    if not so_afiliados:
        qs_pairs.append(("afiliado", "todos"))
    # base p/ os chips de loja: preserva macro/categoria/ordem, troca só a loja.
    qs_sem_loja = list(qs_pairs)
    if ordenar == "valor":
        qs_sem_loja.append(("ordenar", "valor"))
    qs_base_sem_loja = (urlencode(qs_sem_loja) + "&") if qs_sem_loja else ""
    if loja_selecionada:
        qs_pairs.append(("loja", loja_selecionada))
    qs_base = (urlencode(qs_pairs) + "&") if qs_pairs else ""
    # Base dos links de página: como `pagina` nunca entra em qs_*, preserva
    # filtros E ordenação sem arrastar a página atual junto.
    qs_pagina = list(qs_pairs)
    if ordenar == "valor":
        qs_pagina.append(("ordenar", "valor"))
    qs_base_pagina = (urlencode(qs_pagina) + "&") if qs_pagina else ""

    return render(request, "scrapers/top_promocoes.html", {
        "produtos": produtos,
        "page_obj": page_obj,
        "cupons_page": cupons_page,
        "qs_base_pagina": qs_base_pagina,
        "macro_categorias": macro_categorias,
        "categorias_por_macro": categorias_por_macro,
        "macros_selecionados": macros_selecionados,
        "categorias_selecionadas": categorias_selecionadas,
        "loja_selecionada": loja_selecionada,
        "lojas": list(MARKETPLACES.keys()) + (["awin"] if IntegracaoAfiliado.objects.filter(
            owner=request.user, provedor="awin", status="conectada", habilitada=True).exists()
            else []),
        "canais": list(SENDERS.keys()),
        "ordenar": ordenar,
        "busca": busca,
        "min_desconto": min_desconto,
        "tipo": tipo,
        "fontes": fontes,
        "afiliacao": afiliacao,
        "afiliacao_ultimo_erro": afiliacao_ultimo_erro,
        "fonte_selecionada": fonte_selecionada,
        "confianca_selecionada": confianca_selecionada,
        "atualizado_desde": atualizado_desde,
        "cupom_categorias": cupom_categorias,
        "categoria_cupom_selecionada": categoria_cupom_selecionada,
        "cupom_anunciantes": cupom_anunciantes,
        "anunciante_selecionado": anunciante_selecionado,
        "como_usar_selecionado": como_usar_selecionado,
        "cupons_catalogo": cupons_catalogo,
        "awin_programas": ProgramaAfiliado.objects.filter(
            integracao__owner=request.user, integracao__provedor="awin",
            integracao__status="conectada", habilitado=True,
            status_vinculo="joined", link_status="online").order_by("nome"),
        "manual_coupons_enabled": True,
        "amazon_diagnostico": amazon_diagnostico,
        "so_afiliados": so_afiliados,
        "pendentes_ocultos": pendentes_ocultos,
        "qs_base_sem_afiliado": qs_base_sem_afiliado,
        "filtros_ativos": len(macros_selecionados) + len(categorias_selecionadas)
            + bool(loja_selecionada) + bool(busca) + bool(min_desconto)
            + bool(fonte_selecionada) + bool(confianca_selecionada) + bool(atualizado_desde)
            + bool(categoria_cupom_selecionada) + bool(anunciante_selecionado)
            + bool(como_usar_selecionado),
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
            except Exception:
                logger.exception("Falha inesperada no scraper principal")
                q.put("[ERRO] Falha inesperada ao processar a solicitação.")
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
            except Exception:
                logger.exception("Falha inesperada na raspagem de ofertas")
                q.put("[ERRO] Falha inesperada ao processar a solicitação.")
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
            except Exception:
                logger.exception("Falha inesperada no job SSE %s", fn.__name__)
                q.put("[ERRO] Falha inesperada ao processar a solicitação.")
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
    """SSE — pipeline completo de cupons: campanhas, códigos e projeção p/ a aba."""
    from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
    from apps.scrapers.scraper_mercadolivre.scraper import (
        mapear_cupons, projetar_catalogo_cupons)

    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError

    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.afiliado import frase_resumo_afiliacao
        from apps.scrapers.eventos import log_event
        from apps.scrapers.marketplaces.registry import get_marketplace
        usuario = get_user_model().objects.filter(id=uid).first()

        # O trabalho é dividido em faixas da barra porque nenhuma etapa sozinha
        # conhece o total: sem isso a barra ou zerava a cada etapa, ou (o que
        # acontecia) nunca aparecia e o botão ficava cinza sem explicação.
        try:
            n_campanha = mapear_cupons(faixa=(0, 45))
        except (LoginError, AuthError, SessaoExpirada) as exc:
            print(f"[ERRO] Sessão do Mercado Livre expirada: {exc}")
            print("__ML_LOGIN__")
            return
        except BrowserError as exc:
            print(f"[ERRO] Não foi possível abrir a página de cupons: {exc}")
            return
        print(f"{n_campanha} cupom(ns) de campanha raspados.")

        try:
            n_codigo = mapear_cupons_codigo(faixa=(45, 75))
            print(f"{n_codigo} produto(s) de cupom de checkout raspados.")
        except Exception as exc:
            print(f"Aviso: raspagem de códigos de checkout falhou ({exc}).")

        n_proj = projetar_catalogo_cupons(faixa=(75, 82))
        print(
            f"{n_proj} campanha(s) personalizada(s) catalogada(s) como dados "
            "internos; elas não são códigos públicos."
        )
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items
        fonte_oficial = run_source("ml-cupons-afiliados")
        persistidos = persist_items(fonte_oficial.get("coupons", []))
        n_oficiais = persistidos["coupons"]
        print(f"{n_oficiais} cupom(ns) público(s) oficial(is) encontrado(s).")
        from apps.scrapers.coupon_products import preparar_lote
        preparo = preparar_lote(limite=max(12, n_oficiais))
        print(
            f"{preparo['prontos']} cupom(ns) novo(s) preparado(s) com produtos; "
            f"{preparo['processados']} verificado(s) neste ciclo."
        )
        if not n_oficiais:
            print("Aviso: a fonte oficial não trouxe códigos públicos ativos; "
                  "campanhas de ativação pessoais não serão divulgadas.")
            log_event("scraper", "cupons_vazios",
                      "A fonte oficial não trouxe códigos públicos ativos.",
                      level="warning", usuario=usuario,
                      contexto={"marketplace": "mercadolivre",
                                "campanhas": n_campanha, "etapa": "fonte_oficial"})

        # Produto de cupom sem link de afiliado não aparece na tela de envio (ver
        # top_promocoes): raspar sem afiliar deixava a raspagem "sem efeito visível".
        emitir_fase("Gerando links de afiliado", 0.0, (85, 100))
        pendentes = _produtos_sem_link(usuario, origens=("cupom", "cupom_codigo"))
        if not pendentes:
            print("Todos os produtos de cupom já têm link de afiliado.")
        else:
            print(f"\nGerando links de afiliado para {len(pendentes)} produto(s) de cupom...")
            por_loja = {}
            for p in pendentes:
                por_loja.setdefault(p.marketplace or "mercadolivre", []).append(p)
            for slug, grupo in por_loja.items():
                try:
                    get_marketplace(slug).prefetch_links(grupo, usuario=usuario,
                                                         faixa=(85, 100))
                except (LoginError, AuthError, SessaoExpirada) as exc:
                    print(f"[ERRO] Sessão do Mercado Livre expirada: {exc}")
                    print("__ML_LOGIN__")
                    break
                except Exception as exc:
                    print(f"Aviso: geração de links em {slug} falhou ({exc}).")
        print(frase_resumo_afiliacao(usuario))

    return _sse_runner(_job)


def _produtos_sem_link(usuario, origens=None, limite=80, macros=None):
    """Produtos visíveis ao usuário que ainda não têm link de afiliado dele.

    Mesmo predicado de `gerar_links_stream` (pendente é por USUÁRIO: o link mora em
    LinkAfiliadoUsuario, não no Produto), fatorado porque o fluxo de cupons passou a
    precisar dele também.
    """
    ja_tem = LinkAfiliadoUsuario.objects.filter(
        usuario=usuario, produto=OuterRef("pk")).exclude(link_afiliado="")
    qs = (
        Produto.objects
        .filter(Q(owner__isnull=True) | Q(owner=usuario))
        .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
        .exclude(Exists(ja_tem))
    )
    if origens:
        qs = qs.filter(origem__in=origens)
    # Mesmo filtro de categoria da tela de Promoções (macro_categoria): gera link só
    # do nicho escolhido no seletor ao lado do botão.
    if macros:
        qs = qs.filter(macro_categoria__in=macros)
    return list(qs.order_by("-ultima_observacao")[:limite])


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


@require_GET
@throttle_sse(10)
def gerar_links_stream(request):
    """SSE endpoint — gera links de afiliado em lote para produtos sem link.

    Não é mais só para staff: a fila é por usuário (LinkAfiliadoUsuario) e a tela de
    Promoções só lista item afiliado, então quem não é staff precisava esperar o
    worker para ter QUALQUER produto enviável.
    """
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.auxiliar import SessaoExpirada
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError

    try:
        limite = int(request.GET.get("limite", 50))
    except (TypeError, ValueError):
        limite = 50
    # Filtro opcional de categoria (mesmo campo `macro` da tela de Promoções): gera
    # link só do nicho escolhido no seletor ao lado do botão. Vazio = todos.
    macros = [m for m in request.GET.getlist("macro") if m.strip()]
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.afiliado import frase_resumo_afiliacao
        usuario = get_user_model().objects.filter(id=uid).first()

        pendentes = _produtos_sem_link(usuario, limite=limite, macros=macros or None)
        if not pendentes:
            if macros:
                print(f"Nenhum produto sem link na categoria selecionada ({', '.join(macros)}).")
            else:
                print("Nenhum produto na fila — todos já têm link de afiliado.")
            print(frase_resumo_afiliacao(usuario))
            return
        alvo = f" ({', '.join(macros)})" if macros else ""
        print(f"Gerando link de afiliado para {len(pendentes)} produto(s){alvo}...")
        # Agrupa por loja: cada marketplace gera seus links (ML=Playwright,
        # Amazon=puro Python). Evita rodar o Link Builder do ML num ASIN.
        por_loja = {}
        for p in pendentes:
            por_loja.setdefault(p.marketplace or "mercadolivre", []).append(p)
        for slug, grupo in por_loja.items():
            try:
                get_marketplace(slug).prefetch_links(grupo, usuario=usuario)
            except (LoginError, AuthError, SessaoExpirada) as exc:
                print(f"[ERRO] Sessão do Mercado Livre expirada: {exc}")
                print("__ML_LOGIN__")
                break
            except Exception as exc:
                print(f"Aviso: geração de links em {slug} falhou ({exc}).")
        print(frase_resumo_afiliacao(usuario))

    return _sse_runner(_job)
