"""Transporte de saída. Contrato herdado do `Sender` do v1: nunca levanta por falha de envio."""
from dataclasses import dataclass

import requests
from django.conf import settings

CONFIRMED, FAILED, UNCERTAIN = "confirmado", "falhou", "incerto"
TRANSIENT, PERMANENT, UNKNOWN = "transitorio", "permanente", "desconhecido"


@dataclass
class Result:
    success: bool
    outcome: str  # confirmado | falhou | incerto
    message_id: str = ""
    klass: str = ""  # transitorio | permanente | desconhecido
    retry: bool = False
    error: str = ""


class WahaTransport:
    """WAHA (GOWS): POST /api/sendText com X-Api-Key."""

    def __init__(self, base_url=None, api_key=None, session_name=None, http=None):
        self.base_url = (base_url or settings.WAHA_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.WAHA_API_KEY
        self.session_name = session_name or settings.WAHA_SESSION
        self.http = http or requests

    def send_text(self, chat_id: str, text: str) -> Result:
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.post(
                f"{self.base_url}/api/sendText",
                json={"session": self.session_name, "chatId": chat_id, "text": text},
                headers=headers,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
        except requests.exceptions.Timeout:
            # Pode ter saído: não reenviar sozinho.
            return Result(False, UNCERTAIN, klass=UNKNOWN, error="timeout")
        except requests.exceptions.RequestException as exc:
            return Result(False, FAILED, klass=TRANSIENT, retry=True, error=str(exc)[:200])
        if 200 <= response.status_code < 300:
            return Result(True, CONFIRMED, message_id=_message_id(response))
        error = f"http {response.status_code}"
        if response.status_code >= 500:
            return Result(False, FAILED, klass=TRANSIENT, retry=True, error=error)
        return Result(False, FAILED, klass=PERMANENT, error=error)


def _message_id(response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    value = data.get("id") if isinstance(data, dict) else ""
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("id") or ""
    return str(value or "")
