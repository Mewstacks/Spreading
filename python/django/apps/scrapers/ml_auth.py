"""Resolvedor ÚNICO da credencial ML usada pela raspagem.

Antes cada scraper chamava `session_paths.ml_auth_path()` sem usuário. Desde a
migração multi-tenant essa função devolve string vazia de propósito (escolher
`auth.json` ou "o arquivo mais recente" cruzava fronteira de tenant), então o
`open("")` falhava, o cookie jar saía vazio e a raspagem rodava ANÔNIMA — sem
ninguém reclamar. O portão da tela (conexoes.estado_ml) lia a sessão cifrada do
banco e via "conectado", enquanto o trabalho logo abaixo não lia credencial
nenhuma: era a divergência "conectado na tela, desconectado ao raspar".

Aqui a credencial vem sempre do repositório cifrado (accounts.ml_sessions), em
dois modos:

- `storage_state_para(usuario)` — clique na tela, dono da sessão conhecido.
- `storage_state_do_sistema()` — loop de automação (@system_job), que alimenta o
  catálogo compartilhado e não tem usuário. Usa a organização designada em
  settings.ML_SYSTEM_ORGANIZATION_ID; nunca escolhe uma sozinha.

Devolver None é um resultado legítimo (não há sessão) — quem chama decide se
segue anônimo, mas deve DIZER isso no log em vez de raspar em silêncio.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def http_session(state) -> requests.Session:
    """`requests.Session` com os cookies do storage_state do Playwright.

    Ponto ÚNICO de conversão storage_state → cookie jar. Existia uma segunda
    conversão em `conexoes._cookies_do_storage_state` que achatava tudo num dict
    `{name: value}` e descartava `domain`/`path`. O storage_state do ML carrega
    cookies homônimos de vários domínios (`ssid`, `_d2id`, `orgnickp` aparecem em
    `.mercadolivre.com.br`, `.mercadolibre.com`, `.mercadopago.com.br`): no dict o
    último do arquivo vencia e era enviado ao host errado, então a sonda tomava
    302→login sobre uma sessão perfeitamente viva — e o veredito apagava a sessão.
    """
    session = requests.Session()
    state = state or {}
    for cookie in state.get("cookies", []):
        try:
            # Preserva o domínio como salvo (inclusive o ponto inicial): é ele que
            # diz ao jar para mandar o cookie também nos subdomínios (www., lista.).
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain") or "",
                path=cookie.get("path") or "/",
            )
        except Exception:
            continue
    from apps.scrapers.auxiliar import ua_aleatorio
    session.headers.update({
        "User-Agent": ua_aleatorio(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def storage_state_para(usuario):
    """storage_state do Playwright da organização deste usuário, ou None."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts.ml_sessions import load_storage_state

    if usuario is None or not getattr(usuario, "id", None):
        return None
    try:
        return load_storage_state(usuario)
    except MLSessionCryptoError:
        logger.warning(
            "Sessão ML da organização do usuário %s não pôde ser decifrada; "
            "a raspagem seguirá sem credencial.", usuario.id,
        )
        return None


def storage_state_do_sistema():
    """storage_state da organização designada p/ o catálogo compartilhado, ou None."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts.ml_sessions import load_storage_state_for_organization
    from apps.accounts.models import Organization

    org_id = settings.ML_SYSTEM_ORGANIZATION_ID
    if not org_id:
        return None
    organization = Organization.objects.filter(pk=org_id, status="active").first()
    if organization is None:
        logger.warning(
            "ML_SYSTEM_ORGANIZATION_ID=%s não corresponde a uma organização ativa.",
            org_id,
        )
        return None
    try:
        return load_storage_state_for_organization(organization)
    except MLSessionCryptoError:
        logger.warning(
            "Sessão ML da organização de sistema não pôde ser decifrada.",
        )
        return None


def storage_state(usuario=None):
    """Credencial do fluxo atual: do usuário quando há um, senão a do sistema.

    É este o ponto único que os scrapers chamam — a decisão "usuário ou sistema"
    não deve ficar espalhada por cada módulo de raspagem.
    """
    if usuario is not None and getattr(usuario, "id", None):
        return storage_state_para(usuario)
    return storage_state_do_sistema()


def avisar_sem_sessao(contexto: str, usuario=None) -> None:
    """Registra que uma raspagem vai rodar anônima.

    Sem isto, "0 cupons" por falta de credencial é indistinguível de "0 cupons
    porque não há campanha ativa" — que foi exatamente o que escondeu este bug.
    """
    if usuario is not None and getattr(usuario, "id", None):
        logger.warning(
            "%s vai rodar SEM sessão do Mercado Livre: a organização do usuário %s "
            "não tem sessão salva.", contexto, usuario.id,
        )
        return
    if not settings.ML_SYSTEM_ORGANIZATION_ID:
        logger.warning(
            "%s vai rodar SEM sessão do Mercado Livre: ML_SYSTEM_ORGANIZATION_ID "
            "não está configurada. Páginas que exigem login virão vazias.", contexto,
        )
        return
    logger.warning(
        "%s vai rodar SEM sessão do Mercado Livre: a organização de sistema "
        "(ML_SYSTEM_ORGANIZATION_ID) não tem sessão salva ou ela expirou.", contexto,
    )
