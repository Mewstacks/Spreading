"""
Contrato de canal de envio + formatação de texto rica por canal.

`montar_mensagem` (ofertas.py) constrói o corpo usando o `Markup` do canal, então o
mesmo texto sai com a marcação certa em cada destino (WhatsApp usa *neg*/_ital_/~ris~,
Telegram usa HTML <b>/<i>/<s>).
"""
from abc import ABC, abstractmethod


class Markup:
    """Formatação neutra (sem marcação). Subclasses aplicam a sintaxe do canal."""
    def bold(self, s):   return s
    def italic(self, s): return s
    def strike(self, s): return s
    def code(self, s):   return s
    def escape(self, s): return s  # escapa caracteres reservados do canal


class WhatsAppMarkup(Markup):
    def bold(self, s):   return f"*{s}*"
    def italic(self, s): return f"_{s}_"
    def strike(self, s): return f"~{s}~"
    def code(self, s):   return f"`{s}`"


class TelegramHTMLMarkup(Markup):
    def escape(self, s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def bold(self, s):   return f"<b>{s}</b>"
    def italic(self, s): return f"<i>{s}</i>"
    def strike(self, s): return f"<s>{s}</s>"
    def code(self, s):   return f"<code>{s}</code>"


class Sender(ABC):
    """
    Canal de broadcast. Contrato de retorno idêntico ao whatsapp_client legado:
    dict {sucesso: bool, via?: str, erro?: str} — nunca levanta por falha de envio.
    """
    slug: str = ""
    # "url" -> o canal aceita URL de imagem direto (Telegram); "b64" -> precisa de
    # bytes base64 (WhatsApp via whatsapp-web.js, que falha com webp do ML).
    prefers_image: str = "b64"
    markup: Markup = Markup()

    @abstractmethod
    def enviar_oferta(self, destino: str, mensagem: str, *, imagem_url: str = None,
                      imagem_b64: str = None, mimetype: str = "image/jpeg",
                      legenda: str = None, usuario=None, session: str = None) -> dict:
        # `usuario`: credenciais por-usuário (ex: token do bot do Telegram).
        # `session`: roteia p/ a conexão do dono (WhatsApp multi-tenant). Cada canal
        # usa o que fizer sentido e ignora o resto.
        ...
