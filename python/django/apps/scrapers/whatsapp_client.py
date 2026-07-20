"""
Cliente HTTP fino para o serviço Node de WhatsApp (node.js/index.js).

O Node expõe (todas exigem x-api-key):
  POST /api/enviar         -> texto: {grupoid|numero, mensagem}
                              midia: {grupoid|numero, base64, mimetype, nomeArquivo, legenda}
  POST /api/sessoes        -> cria/revive a sessão do usuário
  POST /api/sessoes/reset  -> descarta a sessão e inicia um QR novo atomicamente
  POST /api/sessoes/logout -> desfaz o pareamento (revoga + limpa credencial)
  GET  /api/status         -> {conectado, fase, progresso, mensagem, grupos, qr, ...}
  GET  /api/grupos         -> {conectado, fase, sincronizando, grupos_indisponivel, grupos}
  POST /api/grupos/refresh -> {sucesso, ...mesmo payload de /api/grupos}

Toda função retorna dicts simples; nunca levanta por falha de envio
(o orquestrador decide o que fazer com sucesso=False).

CONTRATO: a chave "erro" num payload de status/grupos significa exclusivamente
que o Node está inalcançável — ela só é injetada por _request_json, e o Node não
emite "erro" para estados normais (sincronizando, desconectado, capacidade). O
front depende disso para distinguir "serviço fora do ar" de "WhatsApp
desconectado". Não adicione raise_for_status nem checagem de status_code aqui:
o significado tem de vir do corpo.
"""
import time

import requests
from django.conf import settings
from django.core.cache import cache

# Classificação de falha de envio. Espelha node.js/error_taxonomy.js — o Node
# manda a `classe` no corpo de /api/enviar e ela tem precedência aqui.
#
# Existe porque o orquestrador (ofertas.processar_configs_de_envio) conta
# falhas_consecutivas e desliga a ConfiguracaoEnvio ao bater o teto. Sem separar
# "o worker piscou" de "o grupo foi apagado", algumas horas de indisponibilidade
# desligavam a automação sozinhas e nada a religava.
TRANSITORIO = "transitorio"
PERMANENTE = "permanente"
DESCONHECIDO = "desconhecido"

_CLASSES = frozenset({TRANSITORIO, PERMANENTE, DESCONHECIDO})
_SEND_HTTP_TIMEOUT_S = 65


class WhatsAppError(Exception):
    """Falha de configuração/transporte ao falar com o serviço Node."""
    pass


def _classe_do_corpo(corpo) -> str | None:
    """A classe que o Node mandou, se ele for uma versão que a conhece.

    Node antigo no ar não manda o campo → None → o chamador cai em DESCONHECIDO,
    que é o comportamento anterior a esta taxonomia. Deploy fora de ordem
    (Django novo, Node velho) degrada em vez de quebrar.
    """
    if not isinstance(corpo, dict):
        return None
    classe = corpo.get("classe")
    return classe if classe in _CLASSES else None


def _classe_do_status(status_code: int) -> str:
    """Classifica pelo HTTP quando o Node não disse nada.

    429 (limiter) e 503 (sessão fora do ar / capacidade) somem sozinhos; 5xx é
    worker doente, não config errada. Os demais 4xx são pedido malformado: só
    ação humana resolve, então pausar a config é a atitude certa.
    """
    if status_code == 429 or status_code >= 500:
        return TRANSITORIO
    if 400 <= status_code < 500:
        return PERMANENTE
    return DESCONHECIDO


def _base_url() -> str:
    return settings.WHATSAPP_API_URL.rstrip("/")


def _headers() -> dict:
    if not settings.WHATSAPP_API_KEY:
        raise WhatsAppError("WHATSAPP_API_KEY não configurada no .env.")
    return {"x-api-key": settings.WHATSAPP_API_KEY, "Content-Type": "application/json"}


def _headers_opt() -> dict:
    """api-key quando configurada. status/qrcode agora exigem chave (rota fechada)."""
    key = settings.WHATSAPP_API_KEY
    return {"x-api-key": key} if key else {}


def _params(session=None) -> dict:
    """Query da sessão (multi-tenant). Node multi-cliente usa ?session=<clientId>.
    None = sessão única/global (compat). Node antigo ignora o param."""
    return {"session": session} if session else None


def _request_json(method: str, path: str, *, headers=None, params=None, json=None,
                  timeout=5, attempts=2) -> dict:
    url = f"{_base_url()}{path}"
    last_error = None
    for attempt in range(attempts):
        try:
            r = requests.request(method, url, headers=headers, params=params,
                                 json=json, timeout=timeout)
            return r.json()
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                time.sleep(0.35)
    return {"erro": str(last_error)}


_STATUS_TTL_S = 5


def _chave_status(session=None) -> str:
    return f"wa_status:{session or '_global'}"


def invalidar_status(session=None) -> None:
    """Descarta o status cacheado — chame depois de mexer no pareamento."""
    cache.delete(_chave_status(session))


def status(session=None) -> dict:
    """Retorna {conectado: bool}. Exige api-key (rota fechada).

    Cacheado por _STATUS_TTL_S porque o painel faz polling e cada chamada custa um
    request ao Node — que, com o Node fora do ar, leva até 10s (timeout 5 × 2
    tentativas) segurando uma thread do gunicorn. Com poucas threads e várias abas
    abertas isso derrubava o app inteiro. Cachear a resposta de erro é intencional:
    é justamente quando não se deve martelar o Node.
    """
    chave = _chave_status(session)
    cacheado = cache.get(chave)
    if cacheado is not None:
        return cacheado
    data = _request_json("GET", "/api/status", headers=_headers_opt(),
                         params=_params(session), timeout=5)
    data.setdefault("conectado", False)
    cache.set(chave, data, timeout=_STATUS_TTL_S)
    return data


def iniciar_sessao(session) -> dict:
    """Inicia explicitamente o único runtime WhatsApp deste usuário."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    invalidar_status(session)
    return _request_json(
        "POST", "/api/sessoes", headers=_headers(),
        json={"session": session}, timeout=10, attempts=1,
    )


def desconectar(session) -> dict:
    """Desfaz o pareamento: revoga no celular e apaga a credencial do volume."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    invalidar_status(session)
    # timeout 25 > os 15s do client.logout no Node + margem do destroy.
    # attempts=1 de propósito: o retry cego de _request_json dobraria a espera
    # para 50s, e o Node já trata o logout como idempotente.
    return _request_json(
        "POST", "/api/sessoes/logout", headers=_headers(),
        json={"session": session}, timeout=25, attempts=1,
    )


def reiniciar_com_qr(session) -> dict:
    """Descarta a sessão atual e inicia uma sessão limpa para emitir novo QR."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    invalidar_status(session)
    try:
        # attempts=1: repetir um reset cujo resultado se perdeu pode derrubar o
        # Chromium novo que já está gerando o QR. O Node coalesce concorrência.
        return _request_json(
            "POST", "/api/sessoes/reset", headers=_headers(),
            json={"session": session}, timeout=25, attempts=1,
        )
    finally:
        # Um GET concorrente pode repopular o cache entre a invalidação acima e
        # o fim do reset. Limpar de novo impede a UI de reviver estado antigo.
        invalidar_status(session)


def qrcode(session=None) -> dict:
    """Retorna {conectado, qr?} do serviço Node. Exige api-key (rota fechada)."""
    data = _request_json("GET", "/api/qrcode", headers=_headers_opt(),
                         params=_params(session), timeout=8)
    data.setdefault("conectado", False)
    data.setdefault("qr", None)
    return data


def listar_grupos(session=None) -> dict:
    """Lista grupos do WhatsApp conectado. Usado pelo dashboard para escolher destino."""
    return _request_json("GET", "/api/grupos", headers=_headers(),
                         params=_params(session), timeout=15)


def refresh_grupos(session=None) -> dict:
    """Força o Node a re-sincronizar a lista de grupos. POST /api/grupos/refresh."""
    # attempts=1 de propósito: este POST não é idempotente. Se o Node aceita o
    # pedido mas a resposta estoura os 30s, o retry cego de _request_json dispara
    # um segundo refresh — e um segundo getChats no mesmo Chromium — além de
    # dobrar a espera do usuário para 60s. O Node já coalesce pedidos repetidos
    # num único repique (group_sync.js), então insistir aqui só custa.
    data = _request_json("POST", "/api/grupos/refresh", headers=_headers(),
                         params=_params(session), timeout=30, attempts=1)
    if "erro" in data:
        data.setdefault("sucesso", False)
    return data


def diagnosticar(session=None, grupoid: str = "") -> dict:
    """Confere sessão e grupo sem criar ou enviar uma mensagem."""
    if not session:
        return {"sucesso": False, "causa": "whatsapp_desconectado",
                "mensagem": "Sessão WhatsApp do usuário ausente."}
    data = _request_json(
        "POST", "/api/diagnostico", headers=_headers(), params=_params(session),
        json={"session": session, "grupoid": grupoid}, timeout=30, attempts=1,
    )
    if "erro" in data:
        return {"sucesso": False, "causa": "whatsapp_transporte",
                "mensagem": "Não foi possível falar com o serviço WhatsApp."}
    return data


def enviar_oferta(grupoid: str, mensagem: str, imagem_base64: str = None,
                  mimetype: str = "image/jpeg", legenda: str = None,
                  session=None) -> dict:
    """
    Envia uma oferta para um grupo (ou número) via serviço Node.

    Args:
        grupoid: id do grupo (ex '12345@g.us'), obrigatório.
        mensagem: texto da oferta (vira legenda quando há imagem).
        imagem_base64: opcional; se informado envia mídia em vez de texto puro.

    Returns:
        {sucesso: bool, via?: 'local'|'evolution', erro?: str, classe?: str}

    Toda falha carrega `classe` (TRANSITORIO/PERMANENTE/DESCONHECIDO); o
    orquestrador usa isso para decidir se conta a falha contra a config.
    """
    # Sem destino padrão: num app multi-tenant o grupo vem sempre de
    # ConfiguracaoEnvio.grupo_id do usuário. Um "grupo global" mandaria a oferta
    # de um usuário para o grupo de outro.
    if not grupoid:
        return {"sucesso": False, "erro": "Nenhum grupoid informado.",
                "classe": PERMANENTE}

    payload = {"grupoid": grupoid}
    if session:
        payload["session"] = session   # Node multi-cliente roteia pela sessão do usuário
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
        inicio = time.monotonic()
        r = requests.post(f"{_base_url()}/api/enviar", json=payload,
                          headers=_headers(), timeout=_SEND_HTTP_TIMEOUT_S)
        try:
            corpo = r.json()
        except ValueError:
            corpo = {"erro": r.text[:200]}
        # Node devolve 200 com sucesso:true, ou 4xx/503 com erro.
        if r.status_code == 200 and corpo.get("sucesso") and corpo.get("mensagem_id"):
            return corpo
        if r.status_code == 200 and corpo.get("sucesso"):
            # Regressão do Node (sucesso sem id). Transitório: a config do
            # usuário está correta e nada que ele faça resolve — quem conserta é
            # um deploy. Pausá-lo puniria o usuário pelo nosso bug.
            return {
                **corpo,
                "sucesso": False,
                "status": r.status_code,
                "erro": "WhatsApp não devolveu o ID de confirmação da mensagem.",
                "classe": TRANSITORIO,
            }
        classe = _classe_do_corpo(corpo) or _classe_do_status(r.status_code)
        return {"sucesso": False, "status": r.status_code, **corpo, "classe": classe,
                "duracao_ms": corpo.get("duracao_ms", round((time.monotonic() - inicio) * 1000))}
    except WhatsAppError as e:
        # WHATSAPP_API_KEY ausente: nenhum envio de ninguém vai funcionar até
        # alguém mexer no .env. Não é defeito da config do usuário.
        return {"sucesso": False, "erro": str(e), "classe": TRANSITORIO}
    except (requests.Timeout, requests.ConnectionError) as e:
        # Os dois piores casos nunca chegam classificados: o Node não responde
        # (worker reiniciando/deploy) ou demora mais que o timeout. Ambos somem
        # sozinhos — e eram justamente estes que desligavam a automação.
        return {"sucesso": False, "erro": f"Falha de transporte: {e}",
                "classe": TRANSITORIO, "etapa": "http",
                "duracao_ms": _SEND_HTTP_TIMEOUT_S * 1000,
                "falha_infra": True}
    except Exception as e:
        return {"sucesso": False, "erro": f"Falha de transporte: {e}",
                "classe": DESCONHECIDO}
