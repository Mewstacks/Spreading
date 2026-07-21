"""
Contrato de canal de envio + formatação de texto rica por canal.

`montar_mensagem` (ofertas.py) constrói o corpo usando o `Markup` do canal, então o
mesmo texto sai com a marcação certa em cada destino (WhatsApp usa *neg*/_ital_/~ris~,
Telegram usa HTML <b>/<i>/<s>).
"""
from abc import ABC, abstractmethod


def padronizar_resultado(resultado, canal: str) -> dict:
    """Completa o envelope do transporte sem apagar dados específicos do canal."""
    dados = dict(resultado or {})
    sucesso = bool(dados.get("sucesso"))
    classe = str(dados.get("classe") or ("" if sucesso else "desconhecido"))
    desfecho = str(dados.get("resultado") or ("confirmado" if sucesso else "falha"))
    repetir = dados.get("repetir")
    if repetir is None:
        repetir = bool(not sucesso and classe == "transitorio" and desfecho != "incerto")
    dados.update({
        "sucesso": sucesso,
        "mensagem_id": str(dados.get("mensagem_id") or ""),
        "canal": canal,
        "via": str(dados.get("via") or canal),
        "classe": classe,
        "resultado": desfecho,
        "repetir": bool(repetir),
        "etapa": str(dados.get("etapa") or "transporte"),
        "duracao_ms": max(0, int(dados.get("duracao_ms") or 0)),
    })
    return dados


class Markup:
    """Formatação neutra (sem marcação). Subclasses aplicam a sintaxe do canal."""
    def bold(self, s):   return s
    def italic(self, s): return s
    def strike(self, s): return s
    def code(self, s):   return s
    def escape(self, s): return "" if s is None else str(s)  # escapa caracteres reservados


class WhatsAppMarkup(Markup):
    def bold(self, s):   return f"*{s}*"
    def italic(self, s): return f"_{s}_"
    def strike(self, s): return f"~{s}~"
    def code(self, s):   return f"`{s}`"


class TelegramHTMLMarkup(Markup):
    def escape(self, s):
        return super().escape(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def bold(self, s):   return f"<b>{s}</b>"
    def italic(self, s): return f"<i>{s}</i>"
    def strike(self, s): return f"<s>{s}</s>"
    def code(self, s):   return f"<code>{s}</code>"


class Sender(ABC):
    """
    Canal de broadcast. Contrato de retorno idêntico ao whatsapp_client legado:
    dict {sucesso, via, mensagem_id, erro, classe, resultado, repetir, etapa,
    duracao_ms} — nunca levanta por falha de envio.
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
