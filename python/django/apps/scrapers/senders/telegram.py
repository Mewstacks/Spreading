"""
Telegram Sender — Bot API (HTTP puro, sem browser).

Vantagens sobre o WhatsApp atual: API oficial, aceita URL de imagem direto (dispensa
o download/conversão webp->jpeg), Markdown/HTML nativo, sem risco de ban por automação.

Setup: crie um bot no @BotFather, ponha TELEGRAM_BOT_TOKEN no .env, e adicione o bot
como ADMIN do canal/grupo. `destino` = '@nomedocanal' ou id numérico (ex '-1001234567890').
"""
import requests
from django.conf import settings

from apps.scrapers.senders.base import Sender, TelegramHTMLMarkup


class TelegramSender(Sender):
    slug = "telegram"
    prefers_image = "url"          # sendPhoto aceita a URL da imagem do ML direto
    markup = TelegramHTMLMarkup()

    def _api(self, metodo):
        token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/{metodo}"

    def enviar_oferta(self, destino, mensagem, *, imagem_url=None, imagem_b64=None,
                      mimetype="image/jpeg", legenda=None, session=None):
        # session ignorado: o Bot API do Telegram não tem multi-sessão por usuário.
        if not destino:
            return {"sucesso": False, "erro": "destino (chat_id) vazio."}
        if not getattr(settings, "TELEGRAM_BOT_TOKEN", ""):
            return {"sucesso": False, "erro": "TELEGRAM_BOT_TOKEN não configurado no .env."}

        # Telegram: legenda de foto tem limite de 1024 chars; texto puro até 4096.
        if imagem_url:
            url = self._api("sendPhoto")
            payload = {"chat_id": destino, "photo": imagem_url,
                       "caption": (legenda or mensagem)[:1024], "parse_mode": "HTML"}
        else:
            url = self._api("sendMessage")
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
