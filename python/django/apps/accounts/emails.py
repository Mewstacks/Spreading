"""Envio de e-mails transacionais (verificação, boas-vindas, alerta de conexão).

Usa o EMAIL_BACKEND configurado em settings (console em dev, Titan/SMTP em prod ou
com EMAIL_FORCE_SMTP=1). Cada função monta texto + HTML e nunca levanta: falha de
e-mail não pode derrubar signup nem o watchdog.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from .tokens import gerar_token

logger = logging.getLogger(__name__)


def _enviar(assunto: str, destino: str, template_base: str, ctx: dict) -> bool:
    """Renderia {template_base}.txt (+ .html opcional) e envia. Retorna sucesso."""
    if not destino:
        return False
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend.endswith("smtp.EmailBackend") and not (
        getattr(settings, "EMAIL_HOST_USER", "")
        and getattr(settings, "EMAIL_HOST_PASSWORD", "")
    ):
        # Sem credencial, insistir no SMTP só produz um 553 por alerta/signup.
        # Registra a degradação no máximo uma vez por hora e preserva o fluxo.
        #
        # `warning`, não `error`, e a diferença é entre duas coisas que não se
        # parecem quando viram evento: ISTO é "você ainda não configurou o SMTP",
        # um estado conhecido e pendente com o dono da conta; o ramo lá embaixo,
        # onde `msg.send` levanta, é "está configurado e quebrou". Só o segundo
        # é notícia.
        #
        # O nível muda o alcance, não a visibilidade: `warning` continua abrindo
        # incidente (`incidentes_saude.processar_evento` aceita warning e error),
        # então a tela de Saúde segue mostrando, com a ação já escrita — checar
        # EMAIL_HOST_USER/EMAIL_HOST_PASSWORD. O que para é o evento de hora em
        # hora no Sentry.
        #
        # O motivo de isso importar foi medido em 07-08/09/2026: 542 ocorrências
        # de `email_falhou` afogaram o `conexao_caiu` de nível error que ninguém
        # viu, e a produção passou a manhã fora do ar. Ruído conhecido esconde
        # sinal desconhecido — é a mesma razão de `alertas._smtp_configurado`.
        if cache.add("email:configuracao-ausente", "1", timeout=3600):
            from apps.scrapers.eventos import log_event

            logger.warning("SMTP não configurado; e-mail não enviado.")
            log_event(
                "sistema", "email_falhou",
                "SMTP não configurado; e-mails estão temporariamente indisponíveis.",
                level="warning", contexto={"backend": backend},
            )
        return False
    try:
        corpo_txt = render_to_string(f"{template_base}.txt", ctx)
    except Exception:
        corpo_txt = ctx.get("fallback", "")
    msg = EmailMultiAlternatives(
        subject=assunto,
        body=corpo_txt,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[destino],
    )
    try:
        corpo_html = render_to_string(f"{template_base}.html", ctx)
        msg.attach_alternative(corpo_html, "text/html")
    except Exception:
        pass
    try:
        msg.send(fail_silently=False)
        return True
    except Exception as e:
        # Registra no banco, não só no logger: e-mail que não sai é invisível por
        # natureza — ninguém reclama de um e-mail que nunca chegou. Os chamadores
        # tratam False como "segue o fluxo", então sem isto um SMTP mal configurado
        # derruba verificação de conta e alerta de conexão sem deixar rastro.
        from apps.scrapers.eventos import log_event
        if cache.add("email:falha-envio", "1", timeout=3600):
            logger.warning("Falha ao enviar e-mail '%s': %s", assunto, e)
            log_event(
                "sistema", "email_falhou",
                f"E-mail '{assunto}' não pôde ser enviado.",
                level="error",
                contexto={"assunto": assunto, "backend": backend},
                exc=e,
            )
        return False


def enviar_verificacao(user, request=None) -> bool:
    token = gerar_token(user)
    caminho = reverse("verificar-email", args=[token])
    url = request.build_absolute_uri(caminho) if request else caminho
    return _enviar(
        "Confirme seu e-mail — Spreading",
        user.email,
        "registration/email_verificacao",
        {"user": user, "url": url,
         "fallback": f"Confirme seu e-mail: {url}"},
    )


def enviar_boas_vindas(user) -> bool:
    return _enviar(
        "Bem-vindo ao Spreading 🛒",
        user.email,
        "registration/email_boas_vindas",
        {"user": user, "fallback": "Bem-vindo ao Spreading!"},
    )


def enviar_alerta_conexao(user, servico: str, caiu: bool) -> bool:
    """servico: 'WhatsApp' | 'Mercado Livre'. caiu=True -> caiu; False -> reconectou."""
    estado = "caiu" if caiu else "reconectou"
    emoji = "🔴" if caiu else "🟢"
    return _enviar(
        f"{emoji} {servico} {estado} — Spreading",
        user.email,
        "registration/email_alerta_conexao",
        {"user": user, "servico": servico, "caiu": caiu,
         "fallback": f"Seu {servico} {estado}."},
    )
