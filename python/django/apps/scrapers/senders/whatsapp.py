"""WhatsApp Sender — fino wrapper sobre o whatsapp_client (serviço Node)."""
import re

from apps.scrapers import whatsapp_client
from apps.scrapers.senders.base import Sender, WhatsAppMarkup, padronizar_resultado


class WhatsAppSender(Sender):
    slug = "whatsapp"
    prefers_image = "b64"          # whatsapp-web.js exige bytes (webp do ML falha)
    markup = WhatsAppMarkup()

    def _resultado(self, dados):
        return padronizar_resultado(dados, self.slug)

    def enviar_oferta(self, destino, mensagem, *, imagem_url=None, imagem_b64=None,
                      mimetype="image/jpeg", legenda=None, usuario=None, session=None):
        if not session and usuario is not None:
            perfil = getattr(usuario, "perfil", None)
            session = perfil.sessao_whatsapp() if perfil else str(usuario.id)
        if not session:
            return self._resultado({"sucesso": False,
                    "erro": "Sessão WhatsApp do usuário ausente. Reconecte o WhatsApp.",
                    "classe": "transitorio", "via": "whatsapp"})
        destino = str(destino or "").strip()
        mensagem = str(mensagem or "")
        if not destino or not mensagem:
            return self._resultado({"sucesso": False, "erro": "Destino ou mensagem vazia.",
                    "classe": "permanente", "via": "whatsapp"})
        if not re.fullmatch(r"(?:\d+(?:-\d+)?@g\.us|\d+@c\.us)", destino):
            return self._resultado({"sucesso": False,
                    "erro": "Destino do WhatsApp inválido. Use o código terminado em @g.us.",
                    "classe": "permanente", "via": "whatsapp"})
        if imagem_b64:
            return self._resultado(whatsapp_client.enviar_oferta(
                destino, mensagem, imagem_base64=imagem_b64,
                mimetype=mimetype or "image/jpeg", legenda=legenda or mensagem,
                session=session))
        return self._resultado(whatsapp_client.enviar_oferta(
            destino, mensagem, session=session))
