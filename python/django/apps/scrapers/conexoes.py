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
import logging
from dataclasses import dataclass, asdict

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Quanto tempo confiamos na sonda de sessão do ML. A sonda custa um GET à rede;
# o dashboard e a Saúde fazem polling, então sem isto cada aba aberta viraria
# uma ida ao ML. 5min é curto o bastante para a tela não mentir por muito tempo
# e longo o bastante para o polling de 15s da Saúde não pesar.
_TTL_ML_S = 300

# Cache de processo (L1) na frente do veredito do banco (L2). Existe só para o
# poll de 3s da tela de conexão não virar uma query por disparo por aba; a fonte
# compartilhada continua sendo a linha de MercadoLivreSession, então 60s de
# defasagem é o máximo que um processo pode ficar discordando dos outros oito.
_TTL_L1_S = 60

# URL logada usada como sonda. Mesma que auxiliar.iniciar_browser usa para validar
# sessão — se ela redireciona p/ login, a sessão morreu.
_URL_SONDA_ML = "https://myaccount.mercadolivre.com.br/my_purchases/list"

# Marcadores de redirect p/ login. Mesma família de padrões de auxiliar.py:41-44
# e link.py:57-66 — mantidos em sincronia de propósito.
_MARCAS_LOGIN = ("/login", "/lgz/", "/registration", "loginhub")

# Sonda do PORTAL DE AFILIADOS. Mesma URL que link._LB_URL abre no Chromium, sem
# o fragmento (#hub não viaja em HTTP). O portal tem SSO próprio (jms/msl): um
# cookie que ainda vale em mercadolivre.com.br pode ser recusado aqui, e era
# justamente isso que nenhuma tela media — o usuário via tudo verde e a geração
# de links falhava com "sessão expirada".
_URL_SONDA_LINKBUILDER = "https://www.mercadolivre.com.br/afiliados/linkbuilder"


@dataclass
class Estado:
    """Estado de uma conexão. `conectado=False` sempre vem com `motivo` preenchido."""
    conectado: bool
    servico: str                 # "WhatsApp" | "Mercado Livre" | "Amazon Relatórios"
    fonte: str                   # como sabemos: "worker" | "sonda" | "banco" | "cache"
    motivo: str = ""             # texto humano; vazio quando conectado
    detalhe: str = ""            # slug p/ a UI decidir o CTA: "expirado" | "sem_sessao" | ...
    verificado_em: object = None
    # Aviso que NÃO derruba a conexão: "a última sonda não passou, mas isso ainda
    # não é logout". Campo separado de `motivo` de propósito — `motivo` é o texto
    # de quem está desconectado, e misturar os dois fazia o estado se contradizer
    # (conectado=True com motivo preenchido).
    alerta: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── WhatsApp ───────────────────────────

def estado_whatsapp(user=None, session=None) -> Estado:
    """Estado do WhatsApp do usuário. Consulta o worker Node (cache de 5s dele).

    Não inicia sessão: consultar estado não pode ter efeito colateral. Reviver é
    intenção explícita do usuário — o POST whatsapp/iniciar/ disparado pela tela
    de WhatsApp (views.whatsapp_iniciar).
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
    fase = data.get("fase") or ""
    # Fase transitória (worker religando após deploy/restart) não é falta de
    # pareamento: a tela de WhatsApp mostra essas fases como progresso azul, e a
    # Saúde dizia "escaneie o QR" para o MESMO payload — a outra metade da
    # divergência entre as duas telas.
    if fase in {"iniciando", "preparando", "carregando", "autenticado",
                "sincronizando", "reconectando"}:
        return Estado(False, "WhatsApp", "worker",
                      "WhatsApp reativando a conexão — aguarde alguns instantes.",
                      "conectando", agora)
    if fase == "capacidade":
        return Estado(False, "WhatsApp", "worker",
                      "Serviço de WhatsApp no limite de conexões — tente novamente em instantes.",
                      "capacidade", agora)
    if fase == "recuperacao_pausada":
        # Terminal com credencial preservada: reviver resolve sem QR novo.
        return Estado(False, "WhatsApp", "worker",
                      "WhatsApp pausou a reconexão após falhas — abra a tela do WhatsApp para reativar.",
                      "recuperacao_pausada", agora)
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

    Três camadas, da mais barata para a mais cara:

    1. cache do processo (60s) — para o poll de 3s da tela não bater no banco;
    2. veredito persistido em MercadoLivreSession — a fonte COMPARTILHADA pelos
       nove processos (ver accounts.ml_sessions.registrar_veredito);
    3. sonda HTTP de verdade, que é a única que decifra o storage_state.

    A ordem importa: antes o decrypt (AES sobre o storage_state inteiro) e um
    UPDATE em `last_used_at` aconteciam ANTES da consulta ao cache, então cada
    disparo do poll de 3 segundos, por aba aberta, custava uma escrita no banco.
    """
    from apps.accounts.models import organization_for_user
    from apps.accounts import ml_sessions

    agora = timezone.now()
    organization = organization_for_user(user) if user is not None else None
    if organization is None:
        return Estado(False, "Mercado Livre", "banco",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)

    chave = _chave_ml_org(organization)
    if usar_cache:
        cacheado = cache.get(chave)
        if cacheado is not None:
            return Estado(**{**cacheado, "fonte": "cache"})

    registro = ml_sessions.probe_snapshot(organization)
    if registro is None:
        return Estado(False, "Mercado Livre", "banco",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)
    if registro["status"] == "decrypt_error":
        return Estado(False, "Mercado Livre", "banco",
                      "Sessão do Mercado Livre inválida — reconecte sua conta.",
                      "decrypt_error", agora)

    sondado_em = registro.get("last_probe_at")
    fresco = (
        sondado_em is not None
        and (agora - sondado_em).total_seconds() < _TTL_ML_S
    )
    if usar_cache and fresco:
        return _cachear(chave, _estado_do_registro(registro, "banco", agora))

    return _cachear(chave, _sondar_e_registrar(user, organization, agora))


def _sondar_e_registrar(user, organization, agora) -> Estado:
    """Roda a sonda de verdade e persiste o veredito. Nunca apaga a credencial."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts import ml_sessions

    try:
        # touch=False: sondar não é usar a credencial. `last_used_at` responde
        # "quando a sessão trabalhou pela última vez", e uma tela aberta não é
        # trabalho — era isso que gerava um UPDATE a cada poll.
        storage_state = ml_sessions.load_storage_state(user, touch=False)
    except MLSessionCryptoError:
        return Estado(False, "Mercado Livre", "sonda",
                      "Sessão do Mercado Livre inválida — reconecte sua conta.",
                      "decrypt_error", agora)
    if storage_state is None:
        return Estado(False, "Mercado Livre", "sonda",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)

    veredito, motivo = sondar_sessao_ml(storage_state)
    registro = ml_sessions.registrar_veredito(organization, veredito, motivo)
    if registro is None:
        return Estado(False, "Mercado Livre", "sonda",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)
    if veredito == "inconclusivo":
        logger.warning("Sonda de sessão ML inconclusiva (%s); sessão preservada.", motivo)
    return _estado_do_registro(registro, "sonda", agora)


def _estado_do_registro(registro: dict, fonte: str, agora, *,
                        servico: str = "Mercado Livre",
                        motivo_expirado: str = "Sessão do Mercado Livre expirou — "
                                               "reconecte sua conta.",
                        alerta_instavel: str = "Conexão instável com o Mercado Livre — "
                                               "reconecte se o problema persistir.") -> Estado:
    """Traduz o veredito persistido no Estado que a tela renderiza.

    Serve os dois escopos (site principal e Link Builder): a política de acúmulo é
    a mesma, só mudam o nome do serviço e os textos. Para o Link Builder,
    `registro["status"]` vem sempre "" — ver o comentário em
    `ml_sessions.registrar_veredito_linkbuilder`.
    """
    from apps.accounts.ml_sessions import PROBE_FALHAS_PARA_DESCONECTAR

    falhas = registro.get("probe_failures") or 0
    if registro.get("status") == "expired" or falhas >= PROBE_FALHAS_PARA_DESCONECTAR:
        return Estado(False, servico, fonte, motivo_expirado, "expirado", agora)
    if falhas:
        # Suspeita isolada: a conexão SEGUE valendo. O ML responde 302→login/403 a
        # requisições autenticadas quando o anti-bot entra no caminho, e derrubar a
        # conexão aqui desconectava o usuário por ruído de infraestrutura.
        return Estado(True, servico, fonte, "", "", agora, alerta=alerta_instavel)
    return Estado(True, servico, fonte, "", "", agora)


def _cachear(chave: str, estado: Estado) -> Estado:
    cache.set(chave, estado.as_dict(), timeout=_TTL_L1_S)
    return estado


def sondar_sessao_ml(storage_state) -> tuple:
    """A sessão salva ainda vale? → ("conectado"|"suspeito"|"inconclusivo", motivo)

    Usa requests com os cookies do storage_state em vez de subir um Chromium: a
    sonda roda em request de tela e no polling da Saúde, e um Chromium por
    verificação queimaria a CPU da máquina (que já divide com raspagem e painel).

    "suspeito", e não "expirado": nada que esta função observa distingue com
    segurança um logout de um challenge do gateway anti-bot do ML, que responde
    exatamente 302→login a requisições autenticadas vindas de IP de datacenter.
    Quem decide desconectar é a acumulação de suspeitas em
    `accounts.ml_sessions.registrar_veredito` — e nem ela apaga a credencial.
    """
    from apps.scrapers.ml_auth import http_session

    try:
        sessao = http_session(storage_state)
    except Exception as e:
        return "inconclusivo", f"não foi possível ler a sessão: {e}"
    if not len(sessao.cookies):
        return "suspeito", "sessão salva não tem cookies"

    try:
        r = sessao.get(_URL_SONDA_ML, timeout=8, allow_redirects=False)
    except Exception as e:
        return "inconclusivo", str(e)

    destino = (r.headers.get("Location") or "").lower()
    if r.status_code in (301, 302, 303, 307, 308):
        if any(m in destino for m in _MARCAS_LOGIN):
            return "suspeito", "o ML redirecionou para o login"
        return "inconclusivo", f"redirect inesperado para {destino[:80]}"
    if r.status_code == 200:
        return "conectado", ""
    if r.status_code == 401:
        return "suspeito", "o ML respondeu 401"
    if r.status_code == 403:
        # 403 é a assinatura do gateway anti-bot para o IP da Fly, não logout: ele
        # barra a requisição antes de olhar o cookie. Tratar como expirado fazia a
        # sessão de um usuário recém-conectado ser marcada como morta.
        return "inconclusivo", "o ML respondeu 403 (bloqueio de bot, não logout)"
    # 5xx é problema do ML, não da sessão.
    return "inconclusivo", f"o ML respondeu {r.status_code}"


def sondar_portal_afiliados_ml(storage_state) -> tuple:
    """O portal de afiliados aceita esta sessão? → (veredito, motivo)

    Irmã de `sondar_sessao_ml`, com o mesmo vocabulário de veredito e as mesmas
    regras — inclusive 403 → "inconclusivo", que no IP de datacenter da Fly é
    assinatura do gateway anti-bot e não logout. A diferença é só o alvo: aqui o
    SSO do portal (jms/msl), que é o que a geração de link realmente atravessa.

    Sem Chromium, pelo mesmo motivo declarado em `sondar_sessao_ml`: isto roda em
    request de tela e no polling da Saúde.
    """
    from apps.scrapers.ml_auth import http_session
    from apps.scrapers.scraper_mercadolivre.link_http import _CAMINHOS_INTERSTICIAIS

    try:
        sessao = http_session(storage_state)
    except Exception as e:
        return "inconclusivo", f"não foi possível ler a sessão: {e}"
    if not len(sessao.cookies):
        return "suspeito", "sessão salva não tem cookies"

    try:
        r = sessao.get(_URL_SONDA_LINKBUILDER, timeout=8, allow_redirects=False)
    except Exception as e:
        return "inconclusivo", str(e)

    destino = (r.headers.get("Location") or "").lower()
    if r.status_code in (301, 302, 303, 307, 308):
        if any(p in destino for p in _CAMINHOS_INTERSTICIAIS):
            # Verificação interposta: a conta segue conectada, o gateway só pediu
            # confirmação. Mesma leitura de link._pagina_intersticial.
            return "inconclusivo", "o ML pediu verificação de segurança"
        if any(m in destino for m in _MARCAS_LOGIN):
            return "suspeito", "o portal de afiliados redirecionou para o login"
        return "inconclusivo", f"redirect inesperado para {destino[:80]}"
    if r.status_code == 200:
        return "conectado", ""
    if r.status_code == 401:
        return "suspeito", "o portal de afiliados respondeu 401"
    if r.status_code == 403:
        return "inconclusivo", "o ML respondeu 403 (bloqueio de bot, não logout)"
    return "inconclusivo", f"o portal de afiliados respondeu {r.status_code}"


_MOTIVO_LB_EXPIRADO = ("O Link Builder do Mercado Livre está pedindo login de novo. "
                       "Reconecte sua conta para voltar a gerar links de afiliado.")
_ALERTA_LB_INSTAVEL = ("O Link Builder recusou a última verificação — se a geração "
                       "de links falhar, reconecte o Mercado Livre.")


def estado_ml_linkbuilder(user=None, usar_cache: bool = True) -> Estado:
    """A sessão salva serve para GERAR LINKS (portal de afiliados)?

    Mesma arquitetura de três camadas de `estado_ml` (cache de processo → veredito
    no banco → sonda HTTP) sobre as colunas `lb_*`. Existe porque `estado_ml` mede
    `myaccount.mercadolivre.com.br` e o Link Builder mora atrás de outro SSO: sem
    esta função, toda tela dizia "conectado" e só o clique em "Gerar links de
    afiliado" descobria o contrário.

    Nunca reporta desconectado quando não existe sessão nenhuma — nesse caso o
    problema é o anterior (`estado_ml`), e duplicar o aviso confunde mais do que
    ajuda.
    """
    from apps.accounts.models import organization_for_user
    from apps.accounts import ml_sessions

    agora = timezone.now()
    organization = organization_for_user(user) if user is not None else None
    if organization is None:
        return Estado(False, "Link Builder", "banco",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)

    chave = _chave_ml_org(organization, escopo="linkbuilder")
    if usar_cache:
        cacheado = cache.get(chave)
        if cacheado is not None:
            return Estado(**{**cacheado, "fonte": "cache"})

    registro = ml_sessions.linkbuilder_snapshot(organization)
    if registro is None:
        return Estado(False, "Link Builder", "banco",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)

    sondado_em = registro.get("last_probe_at")
    fresco = (
        sondado_em is not None
        and (agora - sondado_em).total_seconds() < _TTL_ML_S
    )
    if usar_cache and fresco:
        return _cachear(chave, _estado_lb_do_registro(registro, "banco", agora))

    return _cachear(chave, _sondar_e_registrar_lb(user, organization, agora))


def _estado_lb_do_registro(registro: dict, fonte: str, agora) -> Estado:
    return _estado_do_registro(
        registro, fonte, agora, servico="Link Builder",
        motivo_expirado=_MOTIVO_LB_EXPIRADO, alerta_instavel=_ALERTA_LB_INSTAVEL,
    )


def _sondar_e_registrar_lb(user, organization, agora) -> Estado:
    """Sonda o portal e persiste o veredito. Nunca apaga a credencial."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts import ml_sessions

    try:
        storage_state = ml_sessions.load_storage_state(user, touch=False)
    except MLSessionCryptoError:
        return Estado(False, "Link Builder", "sonda",
                      "Sessão do Mercado Livre inválida — reconecte sua conta.",
                      "decrypt_error", agora)
    if storage_state is None:
        return Estado(False, "Link Builder", "sonda",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)

    veredito, motivo = sondar_portal_afiliados_ml(storage_state)
    registro = ml_sessions.registrar_veredito_linkbuilder(organization, veredito, motivo)
    if registro is None:
        return Estado(False, "Link Builder", "sonda",
                      "Nenhuma sessão do Mercado Livre — conecte sua conta.",
                      "sem_sessao", agora)
    if veredito == "inconclusivo":
        logger.warning("Sonda do Link Builder inconclusiva (%s); sessão preservada.", motivo)
    return _estado_lb_do_registro(registro, "sonda", agora)


def _chave_ml_org(organization, escopo: str = "site") -> str:
    """Chave por ORGANIZAÇÃO, não por usuário.

    A sessão ML é OneToOne com Organization, então o veredito vale para a
    organização inteira — mas a chave antiga (`user.id`) só invalidava o cache de
    UM membro. Numa org com mais de uma pessoa, os outros continuavam vendo o
    estado velho por até 5 min.

    `escopo` separa a sonda do site da sonda do Link Builder: são dois vereditos
    independentes sobre a mesma credencial (o portal de afiliados tem SSO próprio),
    e compartilhar a chave faria um sobrescrever o outro.
    """
    return f"ml_sessao:org:{organization.pk}:{escopo}"


def invalidar_ml(user=None) -> None:
    """Descarta o estado cacheado — chame ao salvar uma sessão nova.

    Só alcança o cache DESTE processo (LocMem); os outros oito continuam com o
    valor antigo por até `_TTL_L1_S`. É aceitável porque a fonte compartilhada é a
    linha do banco, e 60s é o teto da defasagem — antes o teto era 5 min e o
    veredito divergente tinha poder de apagar a credencial.
    """
    from apps.accounts.models import organization_for_user

    organization = organization_for_user(user) if user is not None else None
    if organization is not None:
        cache.delete(_chave_ml_org(organization))
        cache.delete(_chave_ml_org(organization, escopo="linkbuilder"))


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
    """Todos os estados de uma conta. É o que as telas renderizam."""
    return {"whatsapp": estado_whatsapp(user), "mercadolivre": estado_ml(user),
            "ml_linkbuilder": estado_ml_linkbuilder(user),
            "amazon_relatorios": estado_amazon_relatorios(user),
            "ml_relatorios": estado_ml_relatorios(user)}
