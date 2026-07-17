"""Workspace do superadmin (is_superuser).

Vê todos os usuários, uso computado (envios/configs/conexões), saúde das máquinas
Fly (compartilhadas) e permite suspender, definir cotas e impersonar um usuário.

Uso é COMPUTADO das tabelas existentes — não há metering novo. Infra é compartilhada
(uma máquina por serviço), então o painel Fly é global, não por usuário.
"""
import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.scrapers import automacao_state as st
from apps.scrapers.fly_infra import snapshot as fly_snapshot
from apps.scrapers.models import (
    CanalMonitorado, ConfiguracaoEnvio, EventoOperacional, HistoricoEnvio,
    IncidenteSaude, Publicacao,
)
from apps.scrapers.saude import resumo as saude_resumo
from apps.scrapers.views import superadmin_required

User = get_user_model()
logger = logging.getLogger(__name__)

# Chave da sessão que guarda o superadmin original durante a impersonação.
IMPERSONATOR_KEY = "impersonator_id"


def _uso_usuario(user, *, agora=None) -> dict:
    """Uso computado de um usuário a partir das tabelas existentes (sem metering novo)."""
    agora = agora or timezone.now()
    perfil = getattr(user, "perfil", None)
    corte7 = agora - timedelta(days=7)
    hoje = timezone.localtime(agora).date()

    envios_qs = HistoricoEnvio.objects.filter(usuario=user)
    envios_hoje = envios_qs.filter(data_envio__date=hoje).count()
    return {
        "user": user,
        "perfil": perfil,
        "verificado": bool(perfil and perfil.email_verificado),
        "bloqueado": bool(perfil and perfil.bloqueado),
        # Último estado visto pelo watchdog, não leitura ao vivo: esta função roda
        # para TODOS os usuários na listagem, e sondar cada um seria uma ida à rede
        # por linha. É honesto porque o processo `monitor` agora atualiza estas
        # colunas a cada 5min incondicionalmente — antes elas congelavam em None
        # enquanto o worker de envio estivesse desligado, que é o que fazia esta
        # tela discordar do dashboard. Para o estado ao vivo, ver conexoes.py.
        "wa_estado": getattr(perfil, "wa_estado", None),
        "ml_estado": getattr(perfil, "ml_estado", None),
        "amazon_elegivel": getattr(perfil, "amazon_elegivel", None),
        "amazon_erro": getattr(perfil, "amazon_ultimo_erro", ""),
        "configs_ativas": ConfiguracaoEnvio.objects.filter(owner=user, ativo=True).count(),
        "canais": CanalMonitorado.objects.filter(owner=user).count(),
        "envios_total": envios_qs.count(),
        "envios_7d": envios_qs.filter(data_envio__gte=corte7).count(),
        "envios_hoje": envios_hoje,
        "ultima_atividade": envios_qs.aggregate(m=Max("data_envio"))["m"],
        # Cotas (com fallback global) p/ exibição/edição.
        "cota_configs": perfil.cota_max_configs() if perfil else 0,
        "cota_envios_dia": perfil.cota_max_envios_dia() if perfil else 0,
        "cota_wa": perfil.cota_max_wa_sessions() if perfil else 0,
    }


@superadmin_required
def superadmin_usuarios(request):
    """Tabela de todos os usuários + uso computado + saúde das conexões."""
    users = (User.objects.select_related("perfil")
             .order_by("-is_superuser", "-date_joined"))
    linhas = [_uso_usuario(u) for u in users]
    ctx = {
        "linhas": linhas,
        "total_users": len(linhas),
        # Loops são globais (single-tenant em transição): heartbeat mostrado uma vez.
        "worker_scrape": st.worker_alive("scrape"),
        "worker_envio": st.worker_alive("envio"),
        "scrape_ligado": st.is_enabled("scrape"),
        "envio_ligado": st.is_enabled("envio"),
    }
    return render(request, "scrapers/superadmin/usuarios.html", ctx)


@superadmin_required
@require_POST
def superadmin_criar_usuario(request):
    """Cria usuário direto pelo painel (sem depender de e-mail/SMTP).

    Signup público exige e-mail de verificação; em prod o SMTP pode não estar
    configurado, então o superadmin cria a conta já pronta (pré-verificada)."""
    username = (request.POST.get("username") or "").strip()
    email = (request.POST.get("email") or "").strip()
    senha = request.POST.get("senha") or ""
    verificado = bool(request.POST.get("verificado"))

    if not username or not senha:
        messages.error(request, "Usuário e senha são obrigatórios.")
        return redirect("superadmin-usuarios")
    if User.objects.filter(username__iexact=username).exists():
        messages.error(request, f"Usuário '{username}' já existe.")
        return redirect("superadmin-usuarios")
    if len(senha) < 8:
        messages.error(request, "Senha precisa de ao menos 8 caracteres.")
        return redirect("superadmin-usuarios")

    user = User.objects.create_user(username=username, email=email, password=senha)
    # O signal post_save já cria o Perfil; marca verificado se pedido (pula o gate).
    perfil = getattr(user, "perfil", None)
    if perfil and verificado:
        perfil.marcar_verificado()
    messages.success(request, f"Usuário '{username}' criado"
                     + (" (pré-verificado)." if verificado else "."))
    return redirect("superadmin-usuario", user_id=user.id)


@superadmin_required
def superadmin_usuario_detalhe(request, user_id):
    from apps.scrapers.conexoes import estados_do_usuario

    user = get_object_or_404(User.objects.select_related("perfil"), pk=user_id)
    # Uma conta só: aqui cabe sondar ao vivo (na listagem seria uma ida à rede por
    # linha). É esta tela que responde "a conexão dele está de pé AGORA?".
    return render(request, "scrapers/superadmin/usuario_detalhe.html",
                  {"u": _uso_usuario(user), "conexoes": estados_do_usuario(user)})


@superadmin_required
def superadmin_saude(request):
    """Relatório diário de saúde: o que quebrou, para quem, e o que fazer.

    Período curto por padrão (24h) porque a pergunta que esta tela responde é "o que
    aconteceu desde ontem"; 7 dias serve para ver se algo é recorrente ou foi blip.
    """
    horas, usuario, usuario_nome = _filtros_da_saude(request)
    return render(request, "scrapers/superadmin/saude.html",
                  {"r": saude_resumo(horas=horas, usuario=usuario,
                                      usuario_nome=usuario_nome), "horas": horas,
                   "usuario_busca": usuario_nome,
                   "usuario_encontrado": usuario})


def _filtros_da_saude(request):
    """(horas, usuario, usuario_nome) da querystring. Compartilhado pela tela e o JSON."""
    try:
        horas = int(request.GET.get("horas", 24))
    except (TypeError, ValueError):
        horas = 24
    horas = horas if horas in (24, 72, 168) else 24
    usuario_nome = (request.GET.get("usuario") or "").strip()
    usuario = (User.objects.filter(username__iexact=usuario_nome).first()
               if usuario_nome else None)
    return horas, usuario, usuario_nome


@superadmin_required
@require_GET
def superadmin_saude_json(request):
    """Resumo da Saúde em JSON para o auto-refresh. SÓ LEITURA.

    A tela não tinha refresh nenhum: quem consertava um worker ficava olhando um
    vermelho velho até lembrar de dar F5. Só dá para fazer polling porque
    saude.resumo() deixou de escrever — a projeção de incidentes virou trabalho do
    worker `monitor`. Se voltar a escrever aqui, cada tela aberta reprocessa o lote
    a cada 15s.
    """
    horas, usuario, usuario_nome = _filtros_da_saude(request)
    r = saude_resumo(horas=horas, usuario=usuario, usuario_nome=usuario_nome)
    return JsonResponse({
        "estado": r["estado"], "texto": r["texto"],
        "n_erros": r["n_erros"], "n_avisos": r["n_avisos"],
        "atualizado_em": timezone.localtime(r["agora"]).strftime("%H:%M:%S"),
        "conexoes": [{"servico": c["servico"], "conectado": c["conectado"],
                      "motivo": c["motivo"]} for c in r["conexoes"]],
        "workers": [{"job": w["job"], "nome": w["nome"], "ligado": w["ligado"],
                     "vivo": w["vivo"], "alerta": w["alerta"], "fase": w["fase"],
                     "ultima_msg": w["ultima_msg"]} for w in r["workers"]],
        "problemas": [{"causa": p["causa"], "titulo": p["titulo"], "n": p["n"],
                       "critico": p["critico"], "usuarios": p["usuarios"]}
                      for p in r["problemas"]],
        # A contagem move quando um reteste conclui algo: é o gatilho do reload.
        "assinatura": f'{len(r["problemas"])}:{r["n_erros"]}:{r["n_avisos"]}:'
                      f'{len(r["concluidos"])}',
    })


def _retestar_incidente(incidente) -> dict:
    """Retesta UM incidente sem publicar promoção nem repetir mensagem.

    Cada causa tem um teste que já existe no sistema; a única regra é que nenhum
    deles pode ter efeito visível para o usuário final — reteste que publica oferta
    seria pior que o problema.
    """
    contexto = incidente.contexto or {}
    causa = incidente.causa

    if causa.startswith("whatsapp_"):
        from apps.scrapers import whatsapp_client
        destino = ""
        publicacao_id = contexto.get("publicacao_id")
        if publicacao_id:
            destino = (Publicacao.objects.filter(pk=publicacao_id)
                        .values_list("destino_id", flat=True).first() or "")
        # Sem guarda, um usuário sem Perfil levantava Perfil.DoesNotExist, que o
        # except genérico transformava em "Reteste falhou" — escondendo a causa.
        perfil = getattr(incidente.usuario, "perfil", None) if incidente.usuario else None
        if perfil is None:
            return {"sucesso": False, "mensagem": "A conta deste incidente não tem perfil."}
        return whatsapp_client.diagnosticar(perfil.sessao_whatsapp(), destino)

    if causa.startswith("link_"):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        from apps.scrapers.models import Produto
        produto = Produto.objects.filter(pk=contexto.get("produto_id")).first()
        if not (produto and incidente.usuario):
            return {"sucesso": False,
                    "mensagem": "Produto de referência não está mais disponível."}
        r = enviar_oferta_de_produto(produto, "diagnostico@g.us", verificar=True,
                                     dry_run=True, usuario=incidente.usuario,
                                     destino_nome="Diagnóstico sem publicação")
        return {"sucesso": bool(r.get("sucesso")),
                "mensagem": ("Link validado sem publicar oferta." if r.get("sucesso")
                             else r.get("motivo", "Link não validado."))}

    if causa.startswith("sync_") and incidente.usuario:
        from apps.scrapers.relatorios import sync_marketplace
        marketplace = str(contexto.get("marketplace")
                          or incidente.escopo.removeprefix("marketplace:") or "")
        sync = sync_marketplace(incidente.usuario, marketplace)
        return {"sucesso": sync.status == "ok",
                "mensagem": sync.erro or "Relatório sincronizado."}

    if causa == "email_falhou":
        from django.core.mail import get_connection
        connection = get_connection()
        connection.open()
        connection.close()
        return {"sucesso": True, "mensagem": "Conexão SMTP validada sem enviar e-mail."}

    # ── Conexão: agora tem reteste porque existe uma fonte única para perguntar ──
    if causa.startswith("conexao_") or causa == "links_sem_sessao":
        from apps.scrapers.conexoes import estado_ml, estado_whatsapp
        servico = (contexto.get("servico") or "").lower()
        if not incidente.usuario:
            return {"sucesso": False, "mensagem": "Incidente de conexão sem conta associada."}
        if "whats" in servico:
            est = estado_whatsapp(incidente.usuario)
        elif "mercado" in servico or causa == "links_sem_sessao":
            est = estado_ml(incidente.usuario)
        else:
            wa, ml = estado_whatsapp(incidente.usuario), estado_ml(incidente.usuario)
            est = wa if not wa.conectado else ml
        return {"sucesso": est.conectado,
                "mensagem": (f"{est.servico} está conectado agora." if est.conectado
                             else est.motivo)}

    # ── Scraper/fonte: o próprio registro de ingestão responde ──
    if causa.startswith("scrape_") or causa in ("fonte_falhou", "flash_erro",
                                                "cupons_vazios", "cupons_campanha_erro"):
        from apps.scrapers.models import FonteIngestao
        marketplace = contexto.get("marketplace") or contexto.get("fonte") or ""
        fontes = FonteIngestao.objects.all()
        if marketplace:
            fontes = fontes.filter(marketplace=marketplace)
        fonte = fontes.order_by("-ultimo_sucesso").first()
        if fonte is None:
            return {"sucesso": False, "mensagem": "Nenhuma fonte de ingestão registrada."}
        # Só o último ciclo conta: um sucesso de ontem não prova que voltou hoje.
        recente = (fonte.ultimo_sucesso
                   and fonte.ultimo_sucesso >= timezone.now() - timedelta(hours=6))
        if fonte.status == "ok" and recente:
            return {"sucesso": True,
                    "mensagem": (f"{fonte.nome} coletou {fonte.ultimo_total} item(ns) em "
                                 f"{timezone.localtime(fonte.ultimo_sucesso):%d/%m %H:%M}.")}
        return {"sucesso": False,
                "mensagem": (fonte.erro_publico
                             or "A fonte ainda não teve uma coleta bem-sucedida recente.")}

    return {"sucesso": False,
            "mensagem": "Este incidente exige correção manual antes de ser confirmado."}


@superadmin_required
@require_POST
def superadmin_saude_retest(request, incidente_id):
    """Retesta o GRUPO do incidente e conclui os que passarem.

    Por grupo, não por incidente: a tela agrupa por (pipeline, causa, escopo), e só
    oferecia o botão quando o grupo tinha exatamente 1 item — justamente o caso raro.
    Com várias contas afetadas pelo mesmo problema, não havia como marcar nada como
    resolvido, e a tela acumulava erro que ninguém conseguia baixar.
    """
    from apps.scrapers.incidentes_saude import confirmar

    base = get_object_or_404(IncidenteSaude.objects.select_related("usuario"), pk=incidente_id)
    grupo = list(IncidenteSaude.objects.select_related("usuario").filter(
        pipeline=base.pipeline, causa=base.causa, escopo=base.escopo, status="aberto"))
    if not grupo:
        grupo = [base]

    concluidos, falhas, ultima_msg = 0, 0, ""
    for incidente in grupo:
        try:
            resultado = _retestar_incidente(incidente)
        except Exception as exc:
            logger.warning("Reteste do incidente %s falhou: %s", incidente.pk, exc)
            resultado = {"sucesso": False, "mensagem": f"Reteste falhou: {exc}"}
        ultima_msg = resultado.get("mensagem") or ultima_msg
        if resultado.get("sucesso"):
            confirmar(incidente, resultado["mensagem"])
            concluidos += 1
        else:
            falhas += 1

    if concluidos and not falhas:
        messages.success(request, f"Ajuste concluído: {ultima_msg}")
    elif concluidos:
        messages.warning(
            request, f"{concluidos} conta(s) confirmada(s), {falhas} ainda com "
                     f"problema. Última mensagem: {ultima_msg}")
    else:
        messages.error(request, ultima_msg or "O reteste não confirmou o ajuste.")

    # Preserva o filtro: o redirect nu jogava o superadmin de volta em 24h/global,
    # perdendo a conta que ele estava investigando.
    destino = reverse("superadmin-saude")
    filtros = urlencode({k: v for k, v in (
        ("horas", request.POST.get("horas") or ""),
        ("usuario", request.POST.get("usuario") or "")) if v})
    return redirect(f"{destino}?{filtros}" if filtros else destino)


@superadmin_required
def superadmin_infra(request):
    """Painel global das máquinas Fly (compartilhadas). Somente leitura."""
    snap = fly_snapshot(force=request.GET.get("force") == "1")
    # Sessões WhatsApp ativas ~ usuários com wa_estado True (cap da máquina: ~3-4/2GB).
    wa_ativas = User.objects.filter(perfil__wa_estado=True).count()
    eventos = EventoOperacional.objects.select_related("usuario").order_by("-criado_em")[:30]
    return render(request, "scrapers/superadmin/infra.html",
                  {"fly": snap, "wa_ativas": wa_ativas, "eventos": eventos})


@superadmin_required
@require_POST
def superadmin_suspender(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    if user.is_superuser:
        messages.error(request, "Não é possível suspender um superadmin.")
        return redirect("superadmin-usuario", user_id=user_id)
    perfil = user.perfil
    if perfil.bloqueado:
        perfil.desbloquear()
        messages.success(request, f"{user.get_username()} reativado.")
    else:
        motivo = request.POST.get("motivo", "").strip()
        perfil.marcar_bloqueado(motivo)
        messages.success(request, f"{user.get_username()} suspenso.")
    return redirect("superadmin-usuario", user_id=user_id)


@superadmin_required
@require_POST
def superadmin_cotas(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    perfil = user.perfil

    def _int(nome):
        try:
            return max(0, int(request.POST.get(nome, "0") or 0))
        except ValueError:
            return 0

    perfil.max_configs = _int("max_configs")
    perfil.max_envios_dia = _int("max_envios_dia")
    perfil.max_wa_sessions = _int("max_wa_sessions")
    perfil.save(update_fields=["max_configs", "max_envios_dia", "max_wa_sessions"])
    messages.success(request, "Cotas atualizadas (0 = usa o default global).")
    return redirect("superadmin-usuario", user_id=user_id)


# ── Impersonação (session-swap, sem dependência externa) ──────────────
@superadmin_required
@require_POST
def superadmin_impersonar(request, user_id):
    alvo = get_object_or_404(User, pk=user_id)
    if alvo.id == request.user.id:
        return redirect("superadmin-usuario", user_id=user_id)
    real_id = request.user.id
    login(request, alvo)               # cicla a sessão…
    request.session[IMPERSONATOR_KEY] = real_id   # …então grava o superadmin original
    messages.info(request, f"Você está agora como {alvo.get_username()}.")
    return redirect("home")


@require_POST
def superadmin_parar_impersonar(request):
    """Volta ao superadmin original. NÃO exige is_superuser — durante a impersonação
    request.user é o alvo. Autorizado apenas pela presença da chave de sessão."""
    imp_id = request.session.get(IMPERSONATOR_KEY)
    if not imp_id:
        return redirect("home")
    try:
        admin = User.objects.get(pk=imp_id, is_superuser=True)
    except User.DoesNotExist:
        request.session.pop(IMPERSONATOR_KEY, None)
        return redirect("home")
    login(request, admin)              # cicla a sessão e limpa a chave
    messages.success(request, "Impersonação encerrada.")
    return redirect("superadmin-usuarios")
