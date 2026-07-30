"""Registry de canais. Adicionar canal = importar + uma entrada aqui."""
from apps.scrapers.senders.whatsapp import WhatsAppSender
from apps.scrapers.senders.telegram import TelegramSender

SENDERS = {
    WhatsAppSender.slug: WhatsAppSender(),
    TelegramSender.slug: TelegramSender(),
}


def get_sender(canal: str):
    """Retorna o Sender; canal desconhecido nunca pode virar WhatsApp em silêncio."""
    slug = str(canal or "whatsapp").strip().lower()
    if slug not in SENDERS:
        raise ValueError("Canal de envio inválido.")
    return SENDERS[slug]
