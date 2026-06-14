"""Registry de canais. Adicionar canal = importar + uma entrada aqui."""
from apps.scrapers.senders.whatsapp import WhatsAppSender
from apps.scrapers.senders.telegram import TelegramSender

SENDERS = {
    WhatsAppSender.slug: WhatsAppSender(),
    TelegramSender.slug: TelegramSender(),
}


def get_sender(canal: str):
    """Retorna o Sender do canal (default WhatsApp se vazio/desconhecido)."""
    return SENDERS.get((canal or "whatsapp").lower(), SENDERS["whatsapp"])
