"""
Cliente HTTP fino para o serviço Node de WhatsApp (node.js/index.js).

O Node expõe:
  POST /api/enviar   (x-api-key) -> texto: {grupoid|numero, mensagem}
                                    midia: {grupoid|numero, base64, mimetype, nomeArquivo, legenda}
  GET  /api/grupos   (x-api-key) -> {grupos: [{id, nome}]}
  GET  /api/status               -> {conectado: bool}

Toda função retorna dicts simples; nunca levanta por falha de envio
(o orquestrador decide o que fazer com sucesso=False).
"""
import requests
from django.conf import settings


class WhatsAppError(Exception):
    """Falha de configuração/transporte ao falar com o serviço Node."""
    pass


def _base_url() -> str:
    return settings.WHATSAPP_API_URL.rstrip("/")


def _headers() -> dict:
    if not settings.WHATSAPP_API_KEY:
        raise WhatsAppError("WHATSAPP_API_KEY não configurada no .env.")
    return {"x-api-key": settings.WHATSAPP_API_KEY, "Content-Type": "application/json"}


def status() -> dict:
    """Retorna {conectado: bool}. Rota aberta (sem api-key)."""
    try:
        r = requests.get(f"{_base_url()}/api/status", timeout=5)
        return r.json()
    except Exception as e:
        return {"conectado": False, "erro": str(e)}


def qrcode() -> dict:
    """Retorna {conectado, qr?} do serviço Node. Rota aberta (sem api-key)."""
    try:
        r = requests.get(f"{_base_url()}/api/qrcode", timeout=8)
        return r.json()
    except Exception as e:
        return {"conectado": False, "qr": None, "erro": str(e)}


def listar_grupos() -> dict:
    """Lista grupos do WhatsApp conectado. Usado pelo dashboard para escolher destino."""
    try:
        r = requests.get(f"{_base_url()}/api/grupos", headers=_headers(), timeout=15)
        return r.json()
    except Exception as e:
        return {"erro": str(e)}


def refresh_grupos() -> dict:
    """Força o Node a re-sincronizar a lista de grupos. POST /api/grupos/refresh."""
    try:
        r = requests.post(f"{_base_url()}/api/grupos/refresh", headers=_headers(), timeout=30)
        return r.json()
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}


def enviar_oferta(grupoid: str, mensagem: str, imagem_base64: str = None,
                  mimetype: str = "image/jpeg", legenda: str = None) -> dict:
    """
    Envia uma oferta para um grupo (ou número) via serviço Node.

    Args:
        grupoid: id do grupo (ex '12345@g.us'). Se None, cai no WHATSAPP_GRUPO_ID padrão.
        mensagem: texto da oferta (vira legenda quando há imagem).
        imagem_base64: opcional; se informado envia mídia em vez de texto puro.

    Returns:
        {sucesso: bool, via?: 'local'|'evolution', erro?: str}
    """
    destino = grupoid or settings.WHATSAPP_GRUPO_ID
    if not destino:
        return {"sucesso": False, "erro": "Nenhum grupoid informado e WHATSAPP_GRUPO_ID vazio."}

    payload = {"grupoid": destino}
    if imagem_base64:
        payload.update({
            "base64": imagem_base64,
            "mimetype": mimetype,
            "legenda": legenda or mensagem,
            "nomeArquivo": "oferta.jpg",
        })
    else:
        payload["mensagem"] = mensagem

    try:
        r = requests.post(f"{_base_url()}/api/enviar", json=payload,
                          headers=_headers(), timeout=30)
        try:
            corpo = r.json()
        except ValueError:
            corpo = {"erro": r.text[:200]}
        # Node devolve 200 com sucesso:true, ou 4xx/503 com erro.
        if r.status_code == 200 and corpo.get("sucesso"):
            return corpo
        return {"sucesso": False, "status": r.status_code, **corpo}
    except WhatsAppError as e:
        return {"sucesso": False, "erro": str(e)}
    except Exception as e:
        return {"sucesso": False, "erro": f"Falha de transporte: {e}"}
