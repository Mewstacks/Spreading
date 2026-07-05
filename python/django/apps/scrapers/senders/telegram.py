"""
Telegram Sender — Bot API (HTTP puro, sem browser).

Vantagens sobre o WhatsApp atual: API oficial, aceita URL de imagem direto (dispensa
o download/conversão webp->jpeg), Markdown/HTML nativo, sem risco de ban por automação.

Setup: o usuário cria um bot no @BotFather e cola o token na tela Conexão Telegram
(salvo por-usuário no Perfil). Depois adiciona o bot como ADMIN do canal/grupo.
`destino` = '@nomedocanal' ou id numérico (ex '-1001234567890').
"""
import requests
from django.conf import settings

from apps.scrapers.senders.base import Sender, TelegramHTMLMarkup


def resolver_token(usuario=None) -> str:
    """Token do bot do usuário (Perfil); se vazio, cai no global de settings."""
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.telegram_bot_token:
            return perfil.telegram_bot_token.strip()
    return (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()


class TelegramSender(Sender):
    slug = "telegram"
    prefers_image = "url"          # sendPhoto aceita a URL da imagem do ML direto
    markup = TelegramHTMLMarkup()

    def _api(self, metodo, token):
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/{metodo}"

    def enviar_oferta(self, destino, mensagem, *, imagem_url=None, imagem_b64=None,
                      mimetype="image/jpeg", legenda=None, usuario=None, session=None):
        # session ignorado: o Bot API do Telegram usa `usuario` (token do bot por-usuário).
        if not destino:
            return {"sucesso": False, "erro": "destino (chat_id) vazio."}
        token = resolver_token(usuario)
        if not token:
            return {"sucesso": False,
                    "erro": "Bot do Telegram não conectado. Conecte em Conexão Telegram."}

        # Telegram: legenda de foto tem limite de 1024 chars; texto puro até 4096.
        if imagem_url:
            url = self._api("sendPhoto", token)
            payload = {"chat_id": destino, "photo": imagem_url,
                       "caption": (legenda or mensagem)[:1024], "parse_mode": "HTML"}
        else:
            url = self._api("sendMessage", token)
            payload = {"chat_id": destino, "text": mensagem[:4096],
                       "parse_mode": "HTML", "disable_web_page_preview": False}

        try:
            r = requests.post(url, json=payload, timeout=30)
            corpo = r.json()
            if corpo.get("ok"):
                return {"sucesso": True, "via": "telegram"}
            return {"sucesso": False, "erro": corpo.get("description") or f"HTTP {r.status_code}"}
        except Exception as e:
            return {"sucesso": False, "erro": f"Falha de transporte: {e}"}
