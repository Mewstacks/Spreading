"""Transporte de saída. Contrato herdado do `Sender` do v1: nunca levanta por falha de envio."""
import base64
import re
from dataclasses import dataclass

import requests
from django.conf import settings

SESSION_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")  # nome de sessão do WAHA
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

    def reachable(self) -> bool:
        """O WhatsApp responde por esta sessão? Pergunta ao servidor pelo próprio número (não
        manda mensagem): pega o número que diz 'conectado' mas já não fala com o WhatsApp."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        timeout = settings.HTTP_TIMEOUT_SECONDS
        try:
            me = self.http.get(f"{self.base_url}/api/sessions/{self.session_name}",
                               headers=headers, timeout=timeout)
            number = str((((me.json() or {}).get("me")) or {}).get("id") or "").split("@")[0]
            if me.status_code != 200 or not number.isdigit():
                return False
            answer = self.http.get(
                f"{self.base_url}/api/contacts/check-exists",
                params={"phone": number, "session": self.session_name}, headers=headers,
                timeout=timeout)
            return answer.status_code == 200 and (answer.json() or {}).get("numberExists") is True
        except (requests.exceptions.RequestException, ValueError, AttributeError):
            return False

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

    def list_sessions(self) -> list[dict]:
        """Todas as sessões do WAHA: [{name, status}]; [] se inalcançável."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.get(
                f"{self.base_url}/api/sessions", params={"all": "true"}, headers=headers,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            data = response.json() if response.status_code == 200 else []
        except (requests.exceptions.RequestException, ValueError):
            return []
        return [{"name": str(row["name"]), "status": str(row.get("status") or "")}
                for row in data if isinstance(row, dict) and row.get("name")]

    def recent_messages(self, chat_id: str, limit: int = 50) -> list[dict] | None:
        """Últimas mensagens do chat que o WAHA guardou: `{id, from_me, ts, body, ack}`; None se
        não deu para ler (diferente de lista vazia, que é um chat sem mensagens)."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            response = self.http.get(
                f"{self.base_url}/api/{self.session_name}/chats/{chat_id}/messages",
                params={"limit": limit, "downloadMedia": "false"}, headers=headers,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
            data = response.json() if response.status_code == 200 else None
        except (requests.exceptions.RequestException, ValueError):
            return None
        if not isinstance(data, list):
            return None
        rows = []
        for row in data:
            if isinstance(row, dict) and row.get("id"):
                rows.append({
                    "id": str(row["id"]), "from_me": bool(row.get("fromMe")),
                    "ts": int(row.get("timestamp") or 0), "body": str(row.get("body") or ""),
                    "ack": row.get("ack"),
                })
        return rows

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


class SessionRouter:
    """Um WahaTransport por sessão do WAHA (um número de WhatsApp cada)."""

    def __init__(self, factory=None):
        self.factory = factory or WahaTransport
        self._cache = {}

    def for_session(self, name: str):
        name = name or settings.WAHA_SESSION
        if name not in self._cache:
            self._cache[name] = self.factory(session_name=name)
        return self._cache[name]

    def used_sessions(self) -> set[str]:
        """Sessões que têm regra ativa ou item na fila: só essas precisam estar conectadas."""
        from apps.delivery.models import OutboxItem
        from apps.offers.models import Rule

        names = set(Rule.unscoped.filter(active=True)
                    .values_list("destination__session", flat=True))
        names |= set(OutboxItem.unscoped.filter(state=OutboxItem.QUEUED)
                     .values_list("destination__session", flat=True))
        return {name for name in names if name}

    def working_sessions(self) -> set[str]:
        return {name for name in self.used_sessions() if self.for_session(name).session_working()}
