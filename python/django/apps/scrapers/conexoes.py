"""Fonte ÚNICA de verdade do estado das conexões (WhatsApp e Mercado Livre).

Toda tela lê daqui — dashboard, comecar, conta, painel do ML, painel-admin e a
Saúde. Antes existiam quatro leituras concorrentes (HTTP ao worker, mtime de
arquivo, coluna do Perfil, incidente aberto) e elas discordavam entre si: o
dashboard mostrava verde enquanto a Saúde mostrava vermelho, e as duas estavam
"certas" — respondiam perguntas diferentes. Divergência de tela é sintoma de
fonte de verdade duplicada; a correção é não ter a segunda.

Cada função devolve um Estado, não um bool: a tela precisa dizer POR QUE está
desconectado ("sessão expirada" != "worker fora do ar") e desde quando.

Perfil.wa_estado/ml_estado deixaram de ser fonte de leitura: são o registro do
último estado que o watchdog viu, usado só para detectar transição.
"""
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Quanto tempo confiamos na sonda de sessão do ML. A sonda custa um GET à rede;
# o dashboard e a Saúde fazem polling, então sem cache cada aba aberta viraria
# uma ida ao ML. 5min é curto o bastante para a tela não mentir por muito tempo
# e longo o bastante para o polling de 15s da Saúde não pesar.
_TTL_ML_S = 300

# URL logada usada como sonda. Mesma que auxiliar.iniciar_browser usa para validar
# sessão — se ela redireciona p/ login, a sessão morreu.
_URL_SONDA_ML = "https://myaccount.mercadolivre.com.br/my_purchases/list"

# Marcadores de redirect p/ login. Mesma família de padrões de auxiliar.py:41-44
# e link.py:57-66 — mantidos em sincronia de propósito.
_MARCAS_LOGIN = ("/login", "/lgz/", "/registration", "loginhub")


@dataclass
class Estado:
    """Estado de uma conexão. `conectado=False` sempre vem com `motivo` preenchido."""
    conectado: bool
    servico: str                 # "WhatsApp" | "Mercado Livre" | "Amazon Relatórios"
    fonte: str                   # como sabemos: "worker" | "sonda" | "arquivo" | "cache"
    motivo: str = ""             # texto humano; vazio quando conectado
    detalhe: str = ""            # slug p/ a UI decidir o CTA: "expirado" | "sem_sessao" | ...
    verificado_em: object = None

    def as_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── WhatsApp ───────────────────────────

def estado_whatsapp(user=None, session=None) -> Estado:
    """Estado do WhatsApp do usuário. Consulta o worker Node (cache de 5s dele).

    Não inicia sessão: consultar estado não pode ter efeito colateral. A tela de
    WhatsApp chamava iniciar_sessao() ANTES de ler o status (views.py:373), o que
    a tornava otimista por construção e era metade da divergência com a Saúde.
    """
    from apps.scrapers import whatsapp_client

    agora = timezone.now()
    if session is None and user is not None:
        session = _sessao_wa(user)
        if not session:
            return Estado(False, "WhatsApp", "worker",
                          "Nenhuma sessão de WhatsApp para esta conta.", "sem_sessao", agora)
    # session=None com user=None é a consulta legada à sessão global do worker —
    # o whatsapp_client já sabe tratar (params sem `session`). Não é erro.
    try:
        data = whatsapp_client.status(session)
    except Exception as e:
        logger.warning("Sonda WhatsApp falhou para a sessão %s: %s", session, e)
        return Estado(False, "WhatsApp", "worker",
                      "Não foi possível falar com o serviço de WhatsApp.", "servico_fora", agora)

    if data.get("conectado"):
        return Estado(True, "WhatsApp", "worker", "", "", agora)
    if data.get("erro"):
        return Estado(False, "WhatsApp", "worker",
                      "Serviço de WhatsApp indisponível.", "servico_fora", agora)
    return Estado(False, "WhatsApp", "worker",
                  "WhatsApp não está pareado — escaneie o QR Code.", "sem_pareamento", agora)


def _sessao_wa(user) -> str:
    if user is None or not getattr(user, "id", None):
        return ""
    perfil = getattr(user, "perfil", None)
    return perfil.sessao_whatsapp() if perfil else str(user.id)


# ─────────────────────────── Mercado Livre ───────────────────────────

def estado_ml(user=None, usar_cache: bool = True) -> Estado:
    """Estado do ML: existe sessão salva E o ML ainda a aceita?

    A regra anterior era só `mtime(auth_{id}.json) <= 7 dias`, o que é uma
    mentira barata: cookie revogado pelo ML continuava "conectado" por até uma
    semana, enquanto o sync de relatório falhava e a Saúde abria incidente. Aqui
    perguntamos ao próprio ML.
    """
    from apps.scrapers.session_paths import ml_auth_path

    agora = timezone.now()
    path = ml_auth_path(user)
    if not os.path.exists(path):
        return Estado(False, "Mercado Livre", "arquivo",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.", "sem_sessao", agora)

    chave = _chave_ml(user)
    if usar_cache:
        cacheado = cache.get(chave)
        if cacheado is not None:
            return Estado(**{**cacheado, "fonte": "cache"})

    veredito, motivo = sondar_sessao_ml(path)

    if veredito == "expirado":
        # Sessão morta CONFIRMADA: apaga o arquivo para a tela oferecer "Reconectar"
        # em vez de mentir. Mesma decisão de auxiliar.py:96-100.
        try:
            os.remove(path)
        except OSError:
            pass
        cache.delete(chave)
        return Estado(False, "Mercado Livre", "sonda",
                      "Sessão do Mercado Livre expirou — reconecte sua conta.", "expirado", agora)

    if veredito == "inconclusivo":
        # Timeout/erro de rede NÃO é logout. Preserva o último estado conhecido: uma
        # oscilação de rede não pode desconectar o usuário nem apagar a sessão dele
        # (a lição de auxiliar.py:85-89). Sem estado anterior, cai no mtime — a regra
        # antiga, que aqui serve bem como piso conservador.
        ultimo = cache.get(chave)
        if ultimo is not None:
            return Estado(**{**ultimo, "fonte": "cache",
                            "motivo": ultimo.get("motivo") or ""})
        logger.warning("Sonda de sessão ML inconclusiva (%s); caindo na idade do arquivo.", motivo)
        return _estado_ml_por_mtime(path, agora)

    estado = Estado(True, "Mercado Livre", "sonda", "", "", agora)
    cache.set(chave, estado.as_dict(), timeout=_TTL_ML_S)
    return estado


def sondar_sessao_ml(path: str) -> tuple:
    """Pergunta ao ML se a sessão salva ainda vale. → ("conectado"|"expirado"|"inconclusivo", motivo)

    Usa requests com os cookies do storage_state em vez de subir um Chromium: a
    sonda roda em request de tela e no polling da Saúde, e um Chromium por
    verificação queimaria a CPU da máquina (que já divide com raspagem e painel).
    """
    try:
        cookies = _cookies_do_storage_state(path)
    except Exception as e:
        return "inconclusivo", f"não foi possível ler a sessão: {e}"
    if not cookies:
        return "expirado", "sessão salva não tem cookies"

    try:
        from apps.scrapers.auxiliar import ua_aleatorio
        r = requests.get(
            _URL_SONDA_ML, cookies=cookies, timeout=8, allow_redirects=False,
            headers={"User-Agent": ua_aleatorio(),
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Accept-Language": "pt-BR,pt;q=0.9"},
        )
    except Exception as e:
        return "inconclusivo", str(e)

    destino = (r.headers.get("Location") or "").lower()
    if r.status_code in (301, 302, 303, 307, 308):
        if any(m in destino for m in _MARCAS_LOGIN):
            return "expirado", "o ML redirecionou para o login"
        return "inconclusivo", f"redirect inesperado para {destino[:80]}"
    if r.status_code == 200:
        return "conectado", ""
    if r.status_code in (401, 403):
        return "expirado", f"o ML respondeu {r.status_code}"
    # 5xx é problema do ML, não da sessão.
    return "inconclusivo", f"o ML respondeu {r.status_code}"


def _cookies_do_storage_state(path: str) -> dict:
    """Cookies do storage_state do Playwright → dict simples p/ o requests."""
    with open(path, "r", encoding="utf-8") as f:
        estado = json.load(f)
    return {c["name"]: c["value"] for c in estado.get("cookies", []) if c.get("name")}


def _estado_ml_por_mtime(path: str, agora) -> Estado:
    """Piso conservador quando a sonda não conclui: a regra antiga (idade do arquivo)."""
    dias = (agora.timestamp() - os.path.getmtime(path)) / 86400.0
    limite = getattr(settings, "ML_AUTH_STALE_DIAS", 7)
    if dias <= limite:
        return Estado(True, "Mercado Livre", "arquivo", "", "", agora)
    return Estado(False, "Mercado Livre", "arquivo",
                  f"Sessão do Mercado Livre parada há mais de {limite} dias — reconecte.",
                  "expirado", agora)


def _chave_ml(user) -> str:
    uid = getattr(user, "id", None) or "_global"
    return f"ml_sessao:{uid}"


def invalidar_ml(user=None) -> None:
    """Descarta a sonda cacheada — chame ao salvar uma sessão nova."""
    cache.delete(_chave_ml(user))


def estado_amazon_relatorios(user=None) -> Estado:
    """Estado da sessão de relatórios Amazon, separada de tag/Creators API."""
    agora = timezone.now()
    if user is None:
        return Estado(False, "Amazon Relatórios", "arquivo", "Conta ausente.", "sem_sessao", agora)
    from apps.scrapers.report_sessions import has_report_session
    if has_report_session(user, "amazon"):
        return Estado(True, "Amazon Relatórios", "arquivo", "", "", agora)
    return Estado(False, "Amazon Relatórios", "arquivo",
                  "Conecte o portal Amazon Associados para sincronizar relatórios.",
                  "sem_sessao", agora)


def estado_ml_relatorios(user=None) -> Estado:
    """Estado da sessão de RELATÓRIOS do ML (portal de afiliados), separada da
    sessão do site principal (estado_ml). Reconectar o site principal não conserta
    o relatório — o portal de afiliados tem sessão própria."""
    agora = timezone.now()
    if user is None:
        return Estado(False, "Relatórios Mercado Livre", "arquivo", "Conta ausente.", "sem_sessao", agora)
    from apps.scrapers.report_sessions import has_report_session
    if has_report_session(user, "mercadolivre"):
        return Estado(True, "Relatórios Mercado Livre", "arquivo", "", "", agora)
    return Estado(False, "Relatórios Mercado Livre", "arquivo",
                  "Conecte o portal de afiliados do Mercado Livre para sincronizar relatórios.",
                  "sem_sessao", agora)


# ─────────────────────────── Conveniências ───────────────────────────

def estados_do_usuario(user) -> dict:
    """Os dois estados de uma conta. É o que as telas renderizam."""
    return {"whatsapp": estado_whatsapp(user), "mercadolivre": estado_ml(user),
            "amazon_relatorios": estado_amazon_relatorios(user),
            "ml_relatorios": estado_ml_relatorios(user)}
