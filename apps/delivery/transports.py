"""Transporte de saída. Contrato herdado do `Sender` do v1: nunca levanta por falha de envio."""
import base64
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
    keep_claim: bool = False  # falha que não deve devolver o item ao pool (evita laço)


class WahaTransport:
    """WAHA (GOWS): POST /api/sendText com X-Api-Key."""

    def __init__(self, base_url=None, api_key=None, session_name=None, http=None):
        self.base_url = (base_url or settings.WAHA_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.WAHA_API_KEY
        self.session_name = session_name or settings.WAHA_SESSION
        self.http = http or requests

    def send_text(self, chat_id: str, text: str) -> Result:
        return self._post(
            "/api/sendText", {"session": self.session_name, "chatId": chat_id, "text": text}
        )

    def send_image(self, chat_id: str, jpeg: bytes, caption: str) -> Result:
        return self._post("/api/sendImage", {
            "session": self.session_name, "chatId": chat_id, "caption": caption,
            "file": {"mimetype": "image/jpeg", "filename": "oferta.jpg",
                     "data": base64.b64encode(jpeg).decode()},
        })

    def _post(self, path: str, payload: dict) -> Result:
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.post(
                f"{self.base_url}{path}", json=payload, headers=headers,
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


    def session_status(self, timeout=None) -> str:
        """Status da sessão no WAHA (WORKING, SCAN_QR_CODE, FAILED…) ou '' se inalcançável."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.get(
                f"{self.base_url}/api/sessions/{self.session_name}",
                headers=headers, timeout=timeout or settings.HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return ""
            return str((response.json() or {}).get("status") or "")
        except (requests.exceptions.RequestException, ValueError):
            return ""

    def ensure_started(self) -> str:
        """Garante sessão criada e iniciada (QR disponível). Devolve o status final."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        status = self.session_status()
        try:
            if status == "":
                self.http.post(
                    f"{self.base_url}/api/sessions", headers=headers,
                    json={"name": self.session_name, "start": True},
                    timeout=settings.HTTP_TIMEOUT_SECONDS,
                )
            elif status in ("STOPPED", "FAILED"):
                self.http.post(
                    f"{self.base_url}/api/sessions/{self.session_name}/restart",
                    headers=headers, timeout=settings.HTTP_TIMEOUT_SECONDS,
                )
        except requests.exceptions.RequestException:
            pass
        return self.session_status()

    def qr_png(self) -> bytes | None:
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        headers["Accept"] = "image/png"
        try:
            response = self.http.get(
                f"{self.base_url}/api/{self.session_name}/auth/qr",
                headers=headers, timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException:
            return None
        return response.content if response.status_code == 200 else None

    def list_groups(self) -> list[dict]:
        """Grupos da conta: [{id, name, size}] (aceita o formato do GOWS e do NOWEB)."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.get(
                f"{self.base_url}/api/{self.session_name}/groups",
                params={"limit": 500}, headers=headers, timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            data = response.json() if response.status_code == 200 else []
        except (requests.exceptions.RequestException, ValueError):
            return []
        groups = []
        for row in data if isinstance(data, list) else []:
            ident = row.get("JID") or row.get("id")
            if isinstance(ident, dict):
                ident = ident.get("_serialized") or ""
            name = row.get("Name") or row.get("name") or row.get("subject") or ""
            members = row.get("Participants") or row.get("participants") or []
            if ident and str(ident).endswith("@g.us"):
                groups.append({"id": str(ident), "name": str(name), "size": len(members)})
        return sorted(groups, key=lambda g: g["name"].lower())

    def session_working(self) -> bool:
        return self.session_status() == "WORKING"

    def message_ack(self, chat_id: str, message_id: str) -> int | None:
        """ACK da mensagem (-1 erro, 0 pendente, 1 servidor, 2 aparelho, 3 lida) ou None."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.get(
                f"{self.base_url}/api/{self.session_name}/chats/{chat_id}/messages/{message_id}",
                params={"downloadMedia": "false"}, headers=headers,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return None
            ack = (response.json() or {}).get("ack")
            return int(ack) if ack is not None else None
        except (requests.exceptions.RequestException, ValueError, TypeError):
            return None


def _message_id(response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    value = data.get("id") if isinstance(data, dict) else ""
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("id") or ""
    return str(value or "")
