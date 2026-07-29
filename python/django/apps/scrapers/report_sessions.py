"""Sessões cifradas dos portais de relatório por usuário e marketplace.

O storage state do Playwright contém cookies de autenticação. Para relatórios ele
nunca fica em JSON legível no volume: o arquivo persistido usa Fernet e o
plaintext é entregue ao Playwright somente como objeto em memória.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from django.conf import settings

from apps.accounts.crypto import decrypt, encrypt


def _directory(*, criar=False) -> Path:
    """Diretório das sessões de relatório.

    `criar=False` por padrão: só a GRAVAÇÃO precisa garantir o diretório. Antes todo
    caminho passava por um `mkdir(exist_ok=True)`, e como `has_report_session` roda
    no polling de status da tela de conexão, isso era uma syscall a cada 3 segundos
    por aba aberta — num processo web que já divide CPU com um Chromium.
    """
    root = Path(getattr(settings, "ML_AUTH_DIR", "") or settings.BASE_DIR / "sessions")
    path = root / "report_sessions"
    if criar:
        path.mkdir(parents=True, exist_ok=True)
    return path


def encrypted_state_path(usuario, marketplace: str, *, criar=False) -> Path:
    return _directory(criar=criar) / f"{marketplace}_{usuario.id}.state"


# Mesma política do Mercado Livre (accounts/ml_sessions.py): uma falha isolada não
# desconecta ninguém, e NADA aqui apaga a credencial — só o usuário desconectando.
PROBE_FALHAS_PARA_DESCONECTAR = 3


def has_report_session(usuario, marketplace: str) -> bool:
    """A sessão de relatórios existe e ainda deve funcionar?

    Antes isto era um `is_file()` puro, e por isso mentia de duas formas. Um arquivo
    ilegível (chave Fernet trocada, gravação truncada) aparecia como "conectado" e
    só falhava lá na frente, no sync; e uma sessão que o portal já tinha rejeitado
    seguia verde para sempre, porque nada registrava essa rejeição.

    Não há sonda HTTP: a Amazon não expõe um endpoint barato que distinga "sessão
    morta" de "bot bloqueado", e inventar um reproduziria o falso-positivo que
    derrubava a conexão do ML. O sinal usado é o real — o resultado do último sync,
    gravado por `registrar_veredito`, que é o único fluxo que de fato usa a sessão.
    """
    caminho = encrypted_state_path(usuario, marketplace)
    if not caminho.is_file():
        return False
    veredito = probe_snapshot(usuario, marketplace)
    return veredito.get("falhas", 0) < PROBE_FALHAS_PARA_DESCONECTAR


def _probe_path(usuario, marketplace: str) -> Path:
    return encrypted_state_path(usuario, marketplace).with_suffix(".probe")


def probe_snapshot(usuario, marketplace: str) -> dict:
    """Último veredito registrado. Arquivo ao lado do `.state`, no mesmo volume.

    Sidecar em vez de cache: o cache do Django é LocMem, por processo, e os nove
    processos do Procfile discordariam entre si — foi exatamente esse desenho que
    fazia a tela do ML mostrar verde enquanto um worker declarava a sessão morta.
    """
    caminho = _probe_path(usuario, marketplace)
    if not caminho.is_file():
        return {"falhas": 0, "resultado": "", "motivo": ""}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        return {"falhas": int(dados.get("falhas", 0)),
                "resultado": str(dados.get("resultado", "")),
                "motivo": str(dados.get("motivo", ""))}
    except Exception:
        return {"falhas": 0, "resultado": "", "motivo": ""}


def registrar_veredito(usuario, marketplace: str, resultado: str, motivo: str = "") -> dict:
    """Registra como foi o último uso REAL da sessão de relatórios.

    `resultado` é "conectado" (o sync leu o relatório) ou "suspeito" (o portal pediu
    login). Nunca apaga o `.state`: repetidas suspeitas fazem a tela pedir reconexão,
    mas os cookies ficam, e um sync bem-sucedido zera o contador sozinho.
    """
    atual = probe_snapshot(usuario, marketplace)
    falhas = 0 if resultado == "conectado" else atual["falhas"] + 1
    dados = {"falhas": falhas, "resultado": resultado, "motivo": (motivo or "")[:200]}
    caminho = _probe_path(usuario, marketplace)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(dados), encoding="utf-8")
    except OSError:
        # Não poder anotar o veredito não pode derrubar o sync: o pior caso é a tela
        # continuar mostrando o estado anterior.
        pass
    return dados


def limpar_veredito(usuario, marketplace: str) -> None:
    """Chame ao gravar uma sessão nova: o histórico era sobre os cookies antigos."""
    try:
        _probe_path(usuario, marketplace).unlink(missing_ok=True)
    except OSError:
        pass


def save_report_state(usuario, marketplace: str, state: dict) -> None:
    raw = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    cipher = encrypt(base64.b64encode(raw.encode()).decode())
    target = encrypted_state_path(usuario, marketplace, criar=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(cipher, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    limpar_veredito(usuario, marketplace)


def load_report_state(usuario, marketplace: str) -> dict | None:
    """Descriptografa em memória; nunca materializa cookies em arquivo temporário."""
    source = encrypted_state_path(usuario, marketplace)
    if not source.is_file():
        return None
    try:
        encoded = decrypt(source.read_text(encoding="utf-8"))
        raw = base64.b64decode(encoded.encode()).decode()
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError("estado de sessão inválido")
    except Exception as exc:
        raise ValueError("sessão de relatórios ilegível; conecte novamente") from exc

    return state
