"""Contexto do Chromium das telas de login interativo dos marketplaces.

O gateway anti-bot do Mercado Livre recusava a sessão antes de o usuário conseguir
digitar a senha, devolvendo "Ops! Ocorreu um erro. Tente novamente mais tarde." dentro
do live view. A causa era o fingerprint se contradizer: os três workers pegavam o user
agent de ``auxiliar.ua_aleatorio()``, um pool compartilhado com os scrapers que inclui
Safari no macOS e Firefox — aplicados a um **Chromium**. Em metade das tentativas o ML
recebia um UA dizendo Safari enquanto o runtime respondia Blink em tudo o que ele
consulta depois (``window.chrome``, ``navigator.userAgentData.brands``, WebGL,
plugins). As duas UAs de Chrome do pool eram da versão 120/121, defasadas em relação
ao binário da imagem — outra contradição.

Rotacionar UA faz sentido numa raspagem anônima e repetida. Num login interativo, que
acontece uma vez e é o próprio usuário do outro lado, não protege nada e só cria
contradição. Aqui o UA vem do binário de verdade: lemos o que o Chromium reporta e só
trocamos o marcador ``HeadlessChrome``. A versão acompanha a imagem sozinha, sem lista
para manter atualizada.
"""
import logging

logger = logging.getLogger(__name__)

LOCALE = "pt-BR"
TIMEZONE_ID = "America/Sao_Paulo"
ACCEPT_LANGUAGE = "pt-BR,pt;q=0.9,en;q=0.8"

# UA das sondas HTTP que reusam os cookies capturados no live view (ml_auth). Não é
# lido do binário porque a sonda não abre browser nenhum, mas precisa ser ESTÁVEL e
# do mesmo mecanismo: sondar como Chrome uma sessão nascida no Chromium é coerente,
# sortear Firefox a cada chamada não é.
UA_SONDA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# Argumentos de launch comuns aos três logins. `--lang` alinha a UI do próprio
# Chromium ao locale do contexto; sem ele o navegador negocia em inglês.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    f"--lang={LOCALE}",
]


def user_agent_do_binario(browser) -> str:
    """User agent real do Chromium em execução, sem o marcador de headless.

    Lido por CDP no nível do browser (``Browser.getVersion``), então não custa uma
    página descartável nem depende de nenhuma tabela de versões.

    Devolve string vazia se a leitura falhar — nesse caso o chamador deve deixar o
    Playwright usar o UA default em vez de inventar um: um UA default com
    ``HeadlessChrome`` é um sinal ruim, mas um UA falso e contraditório é pior.
    """
    try:
        sessao = browser.new_browser_cdp_session()
    except Exception:
        logger.warning("Não foi possível abrir sessão CDP para ler o user agent.",
                       exc_info=True)
        return ""
    try:
        bruto = (sessao.send("Browser.getVersion") or {}).get("userAgent", "")
    except Exception:
        logger.warning("Browser.getVersion falhou ao ler o user agent.", exc_info=True)
        bruto = ""
    finally:
        try:
            sessao.detach()
        except Exception:
            pass
    if not isinstance(bruto, str) or "Chrome" not in bruto:
        return ""
    return bruto.replace("HeadlessChrome", "Chrome")


def habilitar_foco(context, page) -> None:
    """Faz o renderer headless se considerar sempre focado.

    Um Chromium headless pode tratar a página como se estivesse em janela de fundo, e
    aí campos não mostram o cursor e alguns componentes ignoram o que é digitado. O
    fluxo da Amazon já fazia esta chamada; os dois do ML não, e a divergência não tinha
    razão de ser. Falha aqui é tolerada: é uma melhoria de fidelidade, não um
    requisito para o login funcionar.
    """
    try:
        sessao = context.new_cdp_session(page)
    except Exception:
        logger.debug("Sem sessão CDP para emular foco.", exc_info=True)
        return
    try:
        sessao.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    except Exception:
        logger.debug("Emulação de foco indisponível nesta página.", exc_info=True)


def opcoes_de_coerencia(browser) -> dict:
    """Base de ``new_context`` que não se contradiz: UA do binário + pt-BR + GRU.

    É o mínimo que qualquer contexto Chromium que carregue cookies do ML precisa, com
    ou sem usuário na frente da tela. Nasceu no login interativo, mas a geração de
    link (``auxiliar.iniciar_browser``) ficou de fora e continuou sorteando o UA de
    ``ua_aleatorio()`` — um pool que inclui Safari e Firefox — sem locale nem fuso.
    Ou seja: o fluxo que MAIS depende da sessão era o que mais se contradizia diante
    do SSO do portal de afiliados, e era ele que fazia o Link Builder pedir login.

    Sem UA legível do binário, devolve o dicionário sem a chave: o default do
    Playwright é ruim, mas um UA inventado e contraditório é pior.
    """
    opcoes = {
        "locale": LOCALE,
        "timezone_id": TIMEZONE_ID,
        "extra_http_headers": {"Accept-Language": ACCEPT_LANGUAGE},
    }
    user_agent = user_agent_do_binario(browser)
    if user_agent:
        opcoes["user_agent"] = user_agent
    return opcoes


def opcoes_de_contexto(browser, viewport: dict, *, permissions=None) -> dict:
    """kwargs de ``browser.new_context`` para uma sessão de login interativo.

    Sem ``locale``/``timezone_id`` o Chromium anuncia ``Accept-Language: en-US`` e
    relógio em UTC: um login em português, vindo de um IP brasileiro, com o fuso de
    Londres. É outro sinal barato para o anti-bot e é de graça corrigir.

    O UA fica no do binário até no celular, de propósito. Fabricar um UA de Chrome
    Android deixaria o UA dizendo Android enquanto os client hints
    (``Sec-CH-UA-Platform``) seguem reportando o sistema real do container — a mesma
    classe de contradição que causou o problema. ``is_mobile``/``has_touch`` já dão o
    layout e o toque de celular, que é o que o usuário precisa; um navegador de
    desktop com a janela estreita é uma combinação perfeitamente comum.
    """
    opcoes = {
        **opcoes_de_coerencia(browser),
        "viewport": {"width": viewport["width"], "height": viewport["height"]},
        "device_scale_factor": viewport["device_pixel_ratio"],
        "is_mobile": viewport["device_class"] == "mobile",
        "has_touch": viewport["pointer"] == "coarse",
    }
    if permissions:
        opcoes["permissions"] = list(permissions)
    return opcoes
