"""Relay de webhooks externos → GitHub Actions.

Hoje só existe um: o Sentry avisa que uma issue nova apareceu em produção e
este módulo dispara um `repository_dispatch` no GitHub, que por sua vez roda o
workflow `sentry-autofix.yml` (Claude Code abre um PR com a correção).

Por que um relay e não o Sentry chamando o GitHub direto: o webhook do Sentry
não permite headers customizados, e `POST /repos/{repo}/dispatches` exige
`Authorization: Bearer <PAT>`.

O endpoint é público (`login_not_required`) — a autenticação é a assinatura HMAC
do Sentry. Nenhuma query no banco acontece aqui: o hook precisa funcionar mesmo
quando o Postgres é justamente o que está quebrado.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request

from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger("apps.scrapers.hooks")

# Ambientes cujos erros valem um autofix. Staging/dev geram ruído e queimam a
# cota semanal do plano Claude à toa.
AUTOFIX_ENVIRONMENTS = {"production", "prod"}

# Teto de texto enviado ao GitHub (e daí ao Claude). O payload do Sentry pode
# trazer stacktraces gigantes de bibliotecas.
MAX_STACK_CHARS = 6000
MAX_FRAMES_PER_EXCEPTION = 12


def _env(nome: str, padrao: str = "") -> str:
    # Lido a cada request de propósito: `fly secrets set` reinicia o processo,
    # mas em teste isso permite sobrescrever com override_settings/monkeypatch.
    return os.getenv(nome, padrao)


def _assinatura_confere(corpo: bytes, assinatura: str, segredo: str) -> bool:
    if not segredo or not assinatura:
        return False
    esperado = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura, esperado)


def _resumo_stacktrace(evento: dict) -> str:
    """Extrai tipo/mensagem/frames da exceção, sem variáveis locais.

    Deliberadamente não lê `frame["vars"]`: é lá que moram tokens, senhas e
    dados de usuário que o `before_send` de settings.py já tenta remover do
    lado do Sentry. Aqui o filtro é por construção — só se lê arquivo, linha
    e nome da função.
    """
    linhas: list[str] = []
    for entrada in evento.get("entries") or []:
        if entrada.get("type") != "exception":
            continue
        for valor in (entrada.get("data") or {}).get("values") or []:
            linhas.append(f"{valor.get('type')}: {valor.get('value')}")
            frames = (valor.get("stacktrace") or {}).get("frames") or []
            for frame in frames[-MAX_FRAMES_PER_EXCEPTION:]:
                linhas.append(
                    "  {arquivo}:{linha} in {funcao}".format(
                        arquivo=frame.get("filename") or "?",
                        linha=frame.get("lineNo") or "?",
                        funcao=frame.get("function") or "?",
                    )
                )
    return "\n".join(linhas)[:MAX_STACK_CHARS]


def _dispara_github(payload: dict, repo: str, token: str) -> None:
    corpo = json.dumps(
        {"event_type": "sentry-error", "client_payload": payload}
    ).encode()
    requisicao = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=corpo,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "spreading-sentry-relay",
        },
    )
    with urllib.request.urlopen(requisicao, timeout=10) as resposta:
        resposta.read()


@csrf_exempt
@require_POST
@login_not_required
def sentry_hook(request):
    """Recebe alerta do Sentry e dispara o workflow de autofix no GitHub."""
    segredo = _env("SENTRY_HOOK_SECRET")
    token = _env("GITHUB_DISPATCH_TOKEN")
    repo = _env("GITHUB_REPO")

    if not (segredo and token and repo):
        # Sem credenciais o relay está desligado. 503 (e não 500) para o Sentry
        # marcar como falha temporária em vez de desabilitar a integração.
        logger.warning("sentry_hook chamado sem SENTRY_HOOK_SECRET/GITHUB_* configurados")
        return HttpResponse(status=503)

    if not _assinatura_confere(
        request.body, request.headers.get("Sentry-Hook-Signature", ""), segredo
    ):
        logger.warning("sentry_hook recebeu assinatura inválida")
        return HttpResponseForbidden("assinatura inválida")

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse("payload inválido", status=400)

    if payload.get("action") != "triggered":
        return HttpResponse(status=204)

    evento = ((payload.get("data") or {}).get("event")) or {}

    ambiente = (evento.get("environment") or "").lower()
    if ambiente not in AUTOFIX_ENVIRONMENTS:
        return HttpResponse(status=204)

    issue_id = str(evento.get("issue_id") or "")
    if not issue_id:
        # Sem issue_id não há como deduplicar branch/PR — melhor ignorar do que
        # criar um PR novo a cada ocorrência do mesmo erro.
        return HttpResponse(status=204)

    despacho = {
        "issue_id": issue_id,
        "title": (evento.get("title") or "")[:300],
        "culprit": (evento.get("culprit") or "")[:300],
        "permalink": evento.get("web_url") or evento.get("url") or "",
        "stack": _resumo_stacktrace(evento),
    }

    try:
        _dispara_github(despacho, repo=repo, token=token)
    except (urllib.error.URLError, OSError) as exc:
        # GitHub fora do ar não pode derrubar o endpoint: o Sentry reentrega.
        logger.warning("falha ao disparar repository_dispatch: %s", exc)
        return HttpResponse(status=502)

    logger.info("autofix disparado para issue Sentry %s", issue_id)
    return HttpResponse(status=202)
