"""WhatsApp Sender — fino wrapper sobre o whatsapp_client (serviço Node)."""
from apps.scrapers import whatsapp_client
from apps.scrapers.senders.base import Sender, WhatsAppMarkup


class WhatsAppSender(Sender):
    slug = "whatsapp"
    prefers_image = "b64"          # whatsapp-web.js exige bytes (webp do ML falha)
    markup = WhatsAppMarkup()

    def enviar_oferta(self, destino, mensagem, *, imagem_url=None, imagem_b64=None,
                      mimetype="image/jpeg", legenda=None, session=None):
        if imagem_b64:
            return whatsapp_client.enviar_oferta(
                destino, mensagem, imagem_base64=imagem_b64,
                mimetype=mimetype or "image/jpeg", legenda=legenda or mensagem,
                session=session)
        return whatsapp_client.enviar_oferta(destino, mensagem, session=session)
