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
    # UA fixo, não sorteado. Estes cookies nasceram numa sessão de Chromium; sondá-los
    # ora como Firefox, ora como Safari, é o tipo de incoerência que faz o ML devolver
    # 403 para uma sessão que está viva. O 403 chega aqui como "inconclusivo", então o
    # sintoma para o usuário era o login nunca confirmar e a tela ficar em "Aguardando
    # login" até o deadline de 10 minutos.
    from apps.scrapers.contexto_login import ACCEPT_LANGUAGE, UA_SONDA
    session.headers.update({
        "User-Agent": UA_SONDA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Upgrade-Insecure-Requests": "1",
    })
    return session


# Paredes que o ML devolve NO LUGAR do conteúdo, com HTTP 200, quando a sessão não
# vale para aquela página. Duas famílias distintas e ambas fatais para a raspagem:
#   /jms/…/lgz/login, /login, /registration → sessão morta (logout de fato);
#   /gz/account-verification                → o gateway exige conta logada/validada.
# Desde 2026-08 TODA página de `lista.mercadolivre.com.br` (inclusive uma busca
# comum e sem cookie nenhum) cai na segunda — ou seja, ler container de cupom
# passou a exigir sessão viva. Sem reconhecer a parede, o GET "deu 200" e o parser
# só via zero produto: o pipeline registrava "nenhum produto aplicável" — a
# resposta certa para a pergunta errada.
_PAREDES_ML = (
    "/gz/account-verification", "/lgz/login", "/login", "/registration", "loginhub",
)


def parede_de_login(resposta) -> bool:
    """A resposta é uma parede de login/verificação em vez do conteúdo pedido?

    Olha a URL FINAL (depois dos redirects), que é onde a parede aparece: o ML
    responde 200 no destino, então status_code não distingue nada aqui.
    """
    url = str(getattr(resposta, "url", "") or "").casefold()
    return any(marca in url for marca in _PAREDES_ML)


def storage_state_para(usuario):
    """storage_state do Playwright da organização deste usuário, ou None."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts.ml_sessions import load_storage_state
    from apps.accounts.tenant import executar_no_tenant

    if usuario is None or not getattr(usuario, "id", None):
        return None
    try:
        return executar_no_tenant(load_storage_state, usuario)
    except ValueError:
        # Compatibilidade para comandos locais executados fora de um job tenant.
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
