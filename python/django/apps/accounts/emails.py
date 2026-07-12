"""Envio de e-mails transacionais (verificação, boas-vindas, alerta de conexão).

Usa o EMAIL_BACKEND configurado em settings (console em dev, Titan/SMTP em prod ou
com EMAIL_FORCE_SMTP=1). Cada função monta texto + HTML e nunca levanta: falha de
e-mail não pode derrubar signup nem o watchdog.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from .tokens import gerar_token

logger = logging.getLogger(__name__)


def _enviar(assunto: str, destino: str, template_base: str, ctx: dict) -> bool:
    """Renderia {template_base}.txt (+ .html opcional) e envia. Retorna sucesso."""
    if not destino:
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
        logger.warning("Falha ao enviar e-mail '%s' para %s: %s", assunto, destino, e)
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
