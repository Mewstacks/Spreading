import sys
import os
import re
import time
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

caminho_atual = os.path.dirname(os.path.abspath(__file__))
caminho_django = os.path.dirname(os.path.dirname(os.path.dirname(caminho_atual)))
sys.path.append(caminho_django)
from apps.scrapers.afiliado import (
    ids_com_link, registrar_aprovacao, registrar_falha, registrar_reprovacao,
    salvar_cache,
)
from apps.scrapers.link_validacao import (
    aprovado_por_relatorio, eh_pagina_produto, eh_vitrine_social, motivo_reprovacao,
)
from apps.scrapers.auxiliar import iniciar_browser, BrowserError
from apps.scrapers.carga import coordinated_ml_browser
from apps.scrapers.progresso import emitir_fase
from apps.accounts.ml_sessions import has_storage_state
from apps.accounts.tenant import executar_no_tenant

logger = logging.getLogger(__name__)


def _salvar_link_global(prod, url_isca, link):
    """Grava o link no catálogo compartilhado (worker de sistema, sem usuário)."""
    prod.url_isca = url_isca
    prod.link_afiliado = link
    prod.afiliado_ok = True
    prod.save(update_fields=["url_isca", "link_afiliado", "afiliado_ok"])


class LoginError(Exception):
    """Exceção personalizada para erros de login."""
    pass

class AuthError(Exception):
    """Exceção personalizada para erros de autenticação (ML bloqueou a sessão)."""
    pass


class AntiBotError(Exception):
    """O ML pediu verificação antes de mostrar a página — ela NUNCA foi vista.

    Semanticamente diferente de LoginError: aqui a conta está boa e não há nada
    para o usuário corrigir. É o gateway anti-bot reagindo ao IP de datacenter da
    Fly (ver auxiliar.iniciar_browser e ml_conexao._ir_para_login, que já
    documentam o mesmo comportamento).

    Existe para quebrar o vínculo falso entre "não abriu" e "reconecte sua conta":
    tratar challenge como sessão morta manda o usuário refazer um login que estava
    perfeito, e — pior — fazia o lote inteiro ser descartado.
    """
    pass





class UrlNaoPermitidaError(Exception):
    """O Programa de Afiliados rejeitou a URL (ex: páginas /up/MLBU... de catálogo)."""
    pass


# O portal de afiliados tem SSO PRÓPRIO (jms/msl), separado do site principal — o
# mesmo desenho já reconhecido em ml_relatorio_conexao.py. Cookie bom em
# mercadolivre.com.br não implica Link Builder logado.
#
# A mensagem NÃO diz mais "ele tem sessão própria": era verdade sobre o SSO e
# mentira sobre o que o usuário precisa fazer. A geração de link usa a sessão do
# SITE (iniciar_browser(session_user=...), abaixo), não a de report_sessions —
# então um único login em Conexão Mercado Livre resolve, e sugerir uma segunda
# conexão só produzia a dúvida "onde é a outra?". A assimetria do SSO agora é
# medida por conexoes.sondar_portal_afiliados_ml e aparece na tela ANTES do clique;
# _registrar_veredito_lb, abaixo, alimenta essa medição com o sinal real daqui.
MSG_SESSAO_EXPIRADA = ("O Mercado Livre pediu login de novo ao abrir o Link Builder. "
                       "É a mesma conexão da sua conta: reconecte em Conexão Mercado "
                       "Livre e a geração de links volta a funcionar.")

MSG_SEM_SESSAO = ("Nenhuma conta do Mercado Livre conectada. Conecte em Conexão "
                  "Mercado Livre para gerar links de afiliado.")


def _organization_id_de(usuario):
    """Id da organização do usuário, resolvido com o escopo de tenant instalado.

    Anota o resultado em `usuario._spreading_organization_id` porque o resto do
    trabalho acontece com o Playwright vivo, numa thread onde `organization_for_user`
    não teria como se resolver sozinha.
    """
    if usuario is None:
        return None
    from apps.accounts.models import organization_for_user
    from apps.accounts.tenant import executar_orm_ou_direto

    organization = executar_orm_ou_direto(organization_for_user, usuario)
    organization_id = getattr(organization, "pk", None)
    usuario._spreading_organization_id = organization_id
    return organization_id


def _executor_no_tenant(organization_id):
    """Devolve um `_no_tenant(fn, ...)` preso a esta organização.

    Mesmo contrato de `tenant.executar_orm_ou_direto`, com a organização explícita:
    no SSE (tenant só anotado) reinstala o escopo numa transação curta; em comandos
    e testes sem escopo nenhum, chama direto.
    """
    def _no_tenant(fn, *args, **kwargs):
        try:
            return executar_no_tenant(
                fn, *args, organization_id=organization_id, **kwargs,
            )
        except ValueError as exc:
            if "exige organização" not in str(exc):
                raise
            return fn(*args, **kwargs)
    return _no_tenant


def _registrar_veredito_lb(usuario, veredito: str, motivo: str = "") -> None:
    """Publica no estado compartilhado o que ACABOU de acontecer com o portal.

    Vale mais que a sonda HTTP de conexoes.py: aqui o Chromium realmente abriu (ou
    não) o Link Builder com a sessão do usuário. Sem isto, a tela só descobriria a
    falha no próximo ciclo de sonda — e o usuário já teria visto o erro no stream.

    Falha de escrita nunca derruba a geração de link: isto é telemetria de estado.
    """
    if usuario is None:
        return
    try:
        from apps.accounts.ml_sessions import (
            registrar_prontidao_linkbuilder,
            registrar_veredito_linkbuilder,
        )
        from apps.accounts.models import organization_for_user
        from apps.scrapers.conexoes import invalidar_ml_organization

        estado = {
            "conectado": "ready",
            "suspeito": "login_required",
            "inconclusivo": "temporarily_unavailable",
        }.get(veredito, "temporarily_unavailable")

        def _gravar():
            # Mantém a telemetria histórica, mas a prontidão abaixo é a única
            # fonte que pode acender o verde.
            organization = organization_for_user(usuario)
            registrar_veredito_linkbuilder(organization, veredito, motivo)
            registrar_prontidao_linkbuilder(organization, estado, motivo)
            return getattr(organization, "pk", None)

        # Esta função é chamada com o Playwright sync ativo. Ele mantém um event
        # loop no greenlet da thread atual, então qualquer acesso direto ao ORM
        # dispara SynchronousOnlyOperation. A mesma ponte já usada pelas demais
        # gravações do lote reinstala o tenant numa thread sem esse loop.
        organization_id = executar_no_tenant(
            _gravar,
            organization_id=getattr(usuario, "_spreading_organization_id", None),
        )
        invalidar_ml_organization(organization_id)
    except Exception:
        logger.warning("Não foi possível registrar o veredito do Link Builder.",
                       exc_info=True)


def e_catalogo_universal(url: str) -> bool:
    """True para a vitrine /up/MLBU sem um anúncio MLB individual.

    O Link Builder não aceita esse destino e publicar a vitrine como se fosse um
    produto faz o usuário clicar sem garantia de atribuição. Esta regra é usada já
    na ingestão, antes de o item chegar ao ranking ou à fila de links.
    """
    texto = (url or "").lower()
    return "/up/mlbu" in texto


def _pagina_de_login(page) -> bool:
    """True se o ML redirecionou para a tela de login (sessão caída/insuficiente)."""
    url = (page.url or "").lower()
    if any(t in url for t in ("/login", "/lgz/", "/registration", "loginhub")):
        return True
    try:
        return page.get_by_test_id("user_id").is_visible()
    except Exception:
        return False


def _pagina_intersticial(page) -> bool:
    """True se o ML interpôs uma verificação (/gz/account-verification, captcha).

    Reusa a mesma lista da verificação de destino (link_http), onde ela nasceu de
    uma medição: o interstitial responde 200 com corpo grande, então sem
    reconhecê-lo o código conclui "não é página válida" e culpa a sessão.
    """
    from apps.scrapers.scraper_mercadolivre.link_http import _CAMINHOS_INTERSTICIAIS
    url = (getattr(page, "url", "") or "").lower()
    return any(p in url for p in _CAMINHOS_INTERSTICIAIS)


def _campo_url_linkbuilder(page):
    """Localiza o campo de URLs nas versões atual e legada do Link Builder.

    Em 31/07/2026 o ML removeu o nome acessível ``Insira 1 ou mais URLs`` do
    textarea e deixou apenas um placeholder iniciado por ``Ex: https://...``.
    Selecionar primeiro o textarea evita confundi-lo com a busca global do
    cabeçalho, que também é um textbox visível.
    """
    candidatos = (
        page.locator("textarea[placeholder*='http']").first,
        page.locator("textarea").first,
        page.get_by_role("textbox", name="Insira 1 ou mais URLs"),
    )
    for campo in candidatos:
        try:
            if campo.is_visible(timeout=1000):
                return campo
        except Exception:
            continue
    return None


def _botao_gerar_linkbuilder(page):
    """Localiza somente o botão Gerar do formulário, sem casar botões globais."""
    candidatos = (
        page.get_by_role("button", name="Gerar", exact=True),
        page.locator("button").filter(has_text="Gerar").first,
    )
    for botao in candidatos:
        try:
            if botao.is_visible(timeout=1000):
                return botao
        except Exception:
            continue
    return None


def _linkbuilder_pronto(page) -> bool:
    """Confirma os dois controles necessários no DOM real do portal."""
    try:
        campo = _campo_url_linkbuilder(page)
        botao = _botao_gerar_linkbuilder(page)
        if campo is None or botao is None:
            return False
        if botao.is_enabled(timeout=1000):
            return True

        # No DOM atual o botão nasce desabilitado enquanto o textarea está vazio.
        # Exercitamos o formulário sem submeter: isto prova que os controles estão
        # funcionais, não apenas presentes, e não cria link nem atribuição.
        try:
            valor_anterior = campo.input_value(timeout=1000)
        except Exception:
            valor_anterior = ""
        try:
            campo.fill("https://www.mercadolivre.com.br/")
            try:
                page.wait_for_timeout(150)
            except Exception:
                pass
            return bool(botao.is_enabled(timeout=3000))
        finally:
            try:
                campo.fill(valor_anterior)
            except Exception:
                pass
    except Exception:
        return False


_LB_URL = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
# Mesma forma de ml_conexao._ir_para_login, e pelo mesmo motivo declarado lá: "em
# prod o IP de datacenter da Fly bate no gateway anti-bot da ML". Uma tentativa só
# transformava um soluço do gateway em "sessão expirada".
_LB_TENTATIVAS = 2
_LB_TIMEOUT_MS = 60000
# Reaberturas seguidas do Link Builder antes de desistir do lote. Uma reabertura
# isolada é o gateway anti-bot piscando; três seguidas é o ML recusando o IP, e
# aí insistir só queima tempo.
_MAX_FALHAS_CONSECUTIVAS = 3


def _abrir_link_builder(page, usuario=None):
    """Abre o Link Builder, distinguindo anti-bot de sessão realmente caída.

    O portal de afiliados usa SSO próprio (jms/msl): mesmo com cookies válidos no
    site principal, ele pode redirecionar pro login. O redirect leva alguns
    segundos, então espera a navegação assentar ANTES de checar a URL — checar
    só o campo de login imediatamente após o goto dá falso negativo e o erro
    real só apareceria como timeout genérico no fill() seguinte.

    Levanta AntiBotError (verificação interposta), LoginError (sessão morta de
    verdade) ou AuthError (o ML não respondeu). São três causas com três ações
    diferentes: esperar, reconectar, tentar mais tarde. `usuario` só serve para
    publicar o veredito no estado que as telas leem — ver _registrar_veredito_lb.
    """
    ultimo_erro = None
    navegou = False
    for tentativa in range(1, _LB_TENTATIVAS + 1):
        try:
            page.goto(_LB_URL, wait_until="domcontentloaded", timeout=_LB_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            navegou, ultimo_erro = True, None
        except Exception as exc:
            ultimo_erro = exc
            logger.warning("Link Builder: navegação falhou (tentativa %s/%s): %s",
                           tentativa, _LB_TENTATIVAS, exc)
            continue

        if not _pagina_intersticial(page):
            break
        # O gateway costuma liberar na segunda passada; só insiste se ainda sobra
        # tentativa.
        logger.info("Link Builder: verificação anti-bot interposta (tentativa %s/%s)",
                    tentativa, _LB_TENTATIVAS)
        if tentativa < _LB_TENTATIVAS:
            time.sleep(3)

    # Três causas, três ações do usuário: tentar mais tarde, esperar, reconectar.
    # O veredito acompanha essa mesma divisão — só o login é "suspeito"; anti-bot e
    # ML fora do ar são "inconclusivo", porque a conexão segue valendo e contá-los
    # como falha desconectaria o usuário por ruído de infraestrutura.
    if not navegou:
        _registrar_veredito_lb(usuario, "inconclusivo", "o ML não respondeu")
        raise AuthError(
            "O Mercado Livre não respondeu ao abrir o Link Builder. "
            "Tente de novo em alguns minutos."
        ) from ultimo_erro
    if _pagina_intersticial(page):
        _registrar_veredito_lb(usuario, "inconclusivo", "verificação de segurança interposta")
        raise AntiBotError(
            "O Mercado Livre pediu verificação de segurança antes de abrir o Link "
            "Builder. A conta segue conectada."
        )
    if _pagina_de_login(page):
        _registrar_veredito_lb(usuario, "suspeito", "o portal de afiliados pediu login")
        raise LoginError(MSG_SESSAO_EXPIRADA)

    # A URL pode carregar sem login/challenge e ainda assim trazer um fallback,
    # experimento ou layout novo. Validar o contrato do formulário aqui impede que
    # o loop marque 40 produtos como falhos por um único seletor ausente.
    if not _linkbuilder_pronto(page):
        _registrar_veredito_lb(
            usuario, "inconclusivo",
            "os controles do Link Builder não ficaram disponíveis",
        )
        raise AuthError(
            "O Link Builder não ficou disponível agora. "
            "Tente novamente em alguns minutos."
        )
    _registrar_veredito_lb(usuario, "conectado", "campo de URL e botão Gerar confirmados")


# Campo de resultado do Link Builder — é a fonte do botão "Copiar" (ele copia
# este textarea), então ler daqui dá exatamente o que o clipboard daria, sem
# depender de permissão de clipboard. O sufixo do id é numérico e varia: casamos
# o prefixo.
_SEL_RESULTADO = "textarea[id^='textfield-copyLink']"

# Geração é ida ao servidor do ML; o campo só aparece depois da primeira.
_TIMEOUT_RESULTADO_MS = 30000


def _limpar_resultado(page):
    """Zera o campo de resultado antes de gerar o próximo link.

    É o que torna a espera determinística: o link do produto ANTERIOR fica na
    tela, então sem zerar não há como distinguir "o novo chegou" de "o velho
    ainda está aí". Era exatamente essa a corrida que fazia cada produto do lote
    herdar o link do anterior. No-op na primeira geração (campo ainda não existe).
    """
    page.evaluate(
        """(sel) => { const el = document.querySelector(sel); if (el) el.value = ''; }""",
        _SEL_RESULTADO,
    )


def _esperar_resultado(page):
    """Espera o Link Builder preencher o resultado e devolve o texto cru.

    Chame sempre depois de _limpar_resultado + clique em Gerar. Devolve o texto
    sem validar: mensagens de erro do ML caem aqui também e quem valida é
    _validar_resultado_link.
    """
    page.wait_for_function(
        """(sel) => {
            const el = document.querySelector(sel);
            return !!(el && el.value && el.value.trim());
        }""",
        arg=_SEL_RESULTADO,
        timeout=_TIMEOUT_RESULTADO_MS,
    )
    return page.input_value(_SEL_RESULTADO)


def _validar_resultado_link(bruto):
    """
    O Link Builder escreve no campo de resultado tanto o link quanto mensagens de
    erro (ex '⚠️ Este URL não é permitido pelo Programa.'). Só aceita http(s) real.
    Levanta UrlNaoPermitidaError quando o ML rejeita a URL.
    """
    s = (bruto or "").strip()
    if not s:
        raise ValueError("Link Builder não retornou nada (resultado vazio).")
    import unicodedata
    baixo = unicodedata.normalize("NFKD", s.lower()).encode(
        "ascii", "ignore"
    ).decode()
    rejeicoes = (
        "nao e permitido", "nao permitido", "nao e do mercado livre brasil",
        "url nao e do mercado livre", "url invalido", "url invalida",
    )
    if any(texto in baixo for texto in rejeicoes):
        raise UrlNaoPermitidaError(s)
    if not s.startswith("http"):
        raise ValueError(f"Resultado não é uma URL: {s[:120]}")
    return s


def link_tem_tag_afiliado(link_curto: str, usuario=None) -> bool:
    """
    A3 — Verifica que o link de afiliado realmente carrega a tag de afiliado do
    usuário (ou a global). Segue UM hop do encurtador (meli.la -> destino) e procura
    a tag OU os parâmetros de tracking do Programa (matt_word/matt_tool/tracking_id)
    na URL final. Sem isso, a venda não gera comissão.

    Falha de rede -> None tratado como False pelo chamador (não confia cegamente).
    Se a tag não estiver configurada, cai para a presença dos params padrão.
    """
    import requests as _requests
    from apps.scrapers.afiliado import tag_ml
    from apps.scrapers.scraper_mercadolivre.link_http import _get_ml

    if not link_curto:
        return False
    tag = tag_ml(usuario)
    # Mercado Livre nao usa uma tag manual separada neste fluxo: a identidade de
    # afiliado vem da conta logada no Link Builder. Sem tag configurada, o sucesso
    # do Link Builder ja e a prova operacional que precisamos para nao bloquear.
    if not tag:
        return True
    try:
        sessao = _requests.Session()
        sessao.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        r = _get_ml(sessao, link_curto, timeout=8)
        cadeia = " ".join([h.headers.get("location", "") for h in r.history] + [r.url])
    except Exception:
        return False

    baixo = cadeia.lower()
    if tag and tag.lower() in baixo:
        return True
    # Fallback: parâmetros de tracking que o Link Builder injeta no destino afiliado.
    return any(p in baixo for p in ("matt_word=", "matt_tool=", "tracking_id=", "forceinapp"))


def afiliate_link_builder(link_base, auth_path=None, usuario=None):
    from apps.accounts.feature_flags import enabled_for_user
    if not enabled_for_user("ML_LINK_BUILDER_ENABLED", usuario):
        raise BrowserError(
            "Link Builder por navegador está desativado para esta organização."
        )
    if usuario is not None:
        organization_id = _organization_id_de(usuario)
        if not _executor_no_tenant(organization_id)(has_storage_state, usuario):
            # A guarda acontece antes de iniciar_browser: nunca cria contexto
            # Chromium anônimo para um usuário sem credencial.
            raise LoginError(MSG_SEM_SESSAO)
    with coordinated_ml_browser(
        usuario=usuario, authenticated=usuario is not None,
        owner_kind="ml_link_single",
    ), iniciar_browser(
        auth_path=auth_path,
        session_user=usuario,
        headless=True
    ) as (page, context):
        _abrir_link_builder(page, usuario=usuario)

        try:
            link_final = _afiliar_url_na_pagina(page, link_base)

            logger.debug("Link afiliado ML gerado com sucesso")
            return link_final

        except UrlNaoPermitidaError:
            logger.info("URL rejeitada pelo Programa de Afiliados ML")
            return None
        except Exception as e:
            # O ML pode derrubar a sessão NO MEIO da interação: o fill/click estoura
            # timeout enquanto a página redireciona pro login. Não engolir isso.
            if _pagina_de_login(page):
                _registrar_veredito_lb(usuario, "suspeito",
                                       "sessão caiu durante a geração do link")
                raise LoginError(MSG_SESSAO_EXPIRADA)
            if _pagina_intersticial(page):
                _registrar_veredito_lb(
                    usuario, "inconclusivo",
                    "verificação de segurança durante a geração",
                )
                raise AntiBotError(
                    "O Mercado Livre pediu verificação de segurança durante a geração."
                ) from e
            logger.warning("Erro ao gerar link de afiliado ML: %s", e)
            _registrar_veredito_lb(
                usuario, "inconclusivo",
                "o Link Builder falhou durante a geração",
            )
            raise AuthError(
                "O Link Builder ficou temporariamente indisponível. "
                "Tente novamente em alguns minutos."
            ) from e


def _extrair_item_id(url: str):
    """
    Extrai o item id MLB<digitos> de qualquer forma de URL do ML:
    click1/mclics (pdp_filters=item_id:MLB...), ?item_id=, ?wid=, ou /MLB-123 no path.
    Ignora catálogo MLBU (não é item afiliável). Retorna 'MLB123...' ou None.
    """
    from urllib.parse import unquote
    dec = unquote(url)
    for padrao in (r'item_id[:=]\s*(MLB\d+)', r'[?&]wid=(MLB\d+)', r'/MLB-?(\d{6,})'):
        m = re.search(padrao, dec)
        if m:
            g = m.group(1)
            return g if g.startswith("MLB") else f"MLB{g}"
    return None


def _montar_url_isca(url_produto: str, camp_id: str):
    """
    Normaliza a URL do produto para o formato canônico aceito pelo Programa de
    Afiliados e injeta coupon_campaign_id. URLs de tracking (click1/mclics),
    catálogo (/up/MLBU) ou perfis são reduzidas ao item MLB real quando possível.
    Retorna None se não for página de produto afiliável.
    """
    if not url_produto:
        return None

    from urllib.parse import urlparse

    try:
        parsed = urlparse(url_produto)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")):
        return None
    if parsed.username or parsed.password or port not in (None, 80, 443):
        return None

    _URLS_PROIBIDAS = ("/social/", "/perfil/", "/usuario/", "/noindex/")
    eh_tracking = "click1.mercadolivre" in url_produto or "/mclics/" in url_produto
    eh_catalogo_up = "/up/" in url_produto  # /up/MLBU... = catálogo, Programa rejeita
    url_limpa = url_produto.split('#')[0]

    if any(p in url_limpa for p in _URLS_PROIBIDAS):
        return None

    if e_catalogo_universal(url_produto):
        return None
    if eh_tracking or eh_catalogo_up:
        # Tracking/ads ou catálogo /up/: extrai o item MLB real (wid/item_id/path) e
        # reconstrói uma URL de produto afiliável. (wid costuma estar no fragment.)
        item_id = _extrair_item_id(url_produto)
        if not item_id or item_id.startswith("MLBU"):
            return None  # só catálogo universal, sem item real
        base = f"https://produto.mercadolivre.com.br/MLB-{item_id[3:]}"
    else:
        # Já é página de produto válida (produto./MLB- ou /p/MLB catálogo): mantém
        base = url_limpa.split('?')[0]

    # O destino afiliado sempre deve sair por HTTPS, mesmo se uma fonte antiga
    # ainda tiver persistido uma URL http.
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]

    # Ofertas (sem camp_id) não injetam coupon_campaign_id
    if not camp_id:
        return base
    separador = "&" if "?" in base else "?"
    return f"{base}{separador}coupon_campaign_id={camp_id}"


def _motivo_url_recusada(url_produto: str) -> str:
    """Por que _montar_url_isca devolveu None — em português, para a tela mostrar.

    A função de montagem devolve só None; sem traduzir, "pendente" é tudo o que o
    usuário vê e não há o que fazer a respeito. Aqui refazemos a mesma classificação
    para dizer QUAL das regras barrou.
    """
    url = url_produto or ""
    if not url:
        return "O produto não tem link de origem."
    if any(p in url.split("#")[0] for p in ("/social/", "/perfil/", "/usuario/", "/noindex/")):
        return "Não é uma página de produto (perfil ou vitrine); o Programa de Afiliados não aceita."
    if "/up/" in url or "click1.mercadolivre" in url or "/mclics/" in url:
        return ("Anúncio de catálogo sem item real — o Programa de Afiliados só aceita "
                "a página do produto.")
    return "O Programa de Afiliados não aceita esta URL."


def _afiliar_url_na_pagina(page, link_base: str):
    """
    Gera UM link de afiliado numa página do Link Builder já aberta/logada.
    Reutilizável para chamadas em lote sem reabrir o browser.

    Zera o resultado e espera o novo: sem isso, o link do produto anterior ainda
    está na tela e cada item do lote sai com o link do item anterior — link certo
    apontando pro produto errado, sem erro nenhum no log.
    """
    _limpar_resultado(page)
    campo = _campo_url_linkbuilder(page)
    botao = _botao_gerar_linkbuilder(page)
    if campo is None or botao is None:
        raise AuthError(
            "Os controles do Link Builder não ficaram disponíveis. "
            "Tente novamente em alguns minutos."
        )
    campo.fill(link_base)
    botao.click()
    return _validar_resultado_link(_esperar_resultado(page))


def gerar_links_em_lote(produtos, usuario=None, faixa=None):
    """
    Pré-gera e persiste o link de afiliado de uma lista de Produtos numa ÚNICA
    sessão Playwright. Pula produtos que já têm link em cache.

    Detecta expiração de sessão UMA vez no início (login visível -> LoginError),
    em vez de quebrar no meio do lote.

    `usuario` escolhe a sessão do ML (auth_{id}.json — o que a tela de conexão
    grava) E onde o link é gravado: cada usuário afilia com a conta dele, então o
    link vai pro cache por usuário (LinkAfiliadoUsuario), igual ao caminho de item
    único. Sem `usuario`, grava no Produto.link_afiliado global — o modo antigo
    single-tenant, mantido pelos chamadores legados.

    `faixa` (ini, fim) encaixa o progresso numa fatia da barra, para quem chama isto
    como ETAPA FINAL de um pipeline maior (raspagem de cupons). Sem ela, o % é
    absoluto — o lote é a operação inteira.

    Retorna: (qtd_gerados, qtd_falhas).
    """
    organization_id = None
    if usuario is not None:
        # Resolve o tenant antes de abrir o Playwright. No SSE/worker ele já está
        # apenas anotado; em chamadas diretas (comandos/testes) descobrimos a
        # organização numa consulta curta e passamos o id explicitamente.
        organization_id = _organization_id_de(usuario)
        _no_escopo = _executor_no_tenant(organization_id)
        if not _no_escopo(has_storage_state, usuario):
            raise LoginError(MSG_SEM_SESSAO)
        prontos = _no_escopo(ids_com_link, usuario, produtos)
        pendentes = [p for p in produtos if p.id not in prontos]
    else:
        pendentes = [p for p in produtos if not p.link_afiliado]
    if not pendentes:
        return (0, 0)

    gerados = 0
    falhas = 0
    ultimo_erro = None
    consecutivas = 0      # reaberturas seguidas do Link Builder
    interrompido = None   # erro que encerrou o lote antes do fim
    # Sem guarda global de ORM: cada gravação vai por executar_no_tenant, que a
    # desvia para uma thread sem event loop. O `with` que existia aqui setava
    # DJANGO_ALLOW_ASYNC_UNSAFE no os.environ do PROCESSO — global às 8 threads do
    # gunicorn — e o `finally` dele removia a permissão debaixo de outro fluxo que
    # ainda estava usando. Era a origem do "às vezes funciona".
    with coordinated_ml_browser(
        usuario=usuario, authenticated=usuario is not None,
        owner_kind="ml_links_batch",
    ), iniciar_browser(
        session_user=usuario,
        headless=True
    ) as (page, context):
        _abrir_link_builder(page, usuario=usuario)

        total_lote = len(pendentes)
        logger.info("Gerando links afiliados ML em lote para %s produtos", total_lote)
        for i, prod in enumerate(pendentes, 1):
            emitir_fase(f"Link {i}/{total_lote}", i / total_lote,
                        faixa or (0, 100))
            # Ofertas do feed têm campanha_id vazio: _montar_url_isca trata isso e só
            # injeta coupon_campaign_id quando há campanha. Só pulamos quando a URL
            # não é afiliável (catálogo/perfil) -> None.
            url_isca = _montar_url_isca(prod.link_produto, prod.campanha_id)
            if not url_isca:
                # Este era o ponto cego central: contava a falha e seguia, sem
                # log nem registro. O produto ficava "pendente" para sempre e
                # não havia como saber por quê.
                motivo = _motivo_url_recusada(prod.link_produto)
                logger.info("Produto %s não é afiliável: %s", getattr(prod, "id", None), motivo)
                executar_no_tenant(registrar_falha, usuario, prod, motivo, terminal=True)
                falhas += 1
                continue
            try:
                link = _afiliar_url_na_pagina(page, url_isca)
                # Persistir item a item (e não em lote no fim) é deliberado: se o ML
                # derrubar a sessão no produto 30 de 80, os 29 anteriores ficam salvos.
                if usuario is not None:
                    executar_no_tenant(salvar_cache, usuario, prod, link, url_isca, True)
                else:
                    executar_no_tenant(_salvar_link_global, prod, url_isca, link)
                gerados += 1
            except UrlNaoPermitidaError as e:
                # O Programa recusou explicitamente: retentar não muda nada.
                logger.info("Link Builder recusou o produto %s: %s",
                            getattr(prod, "id", None), e)
                executar_no_tenant(registrar_falha, usuario, prod, str(e), terminal=True)
                falhas += 1
            except Exception as e:
                ultimo_erro = e
                # A página caiu fora do Link Builder (login ou verificação
                # anti-bot). Antes isso levantava LoginError na hora e MATAVA o
                # lote inteiro no primeiro soluço. Agora tenta reabrir UMA vez e
                # segue de onde parou — os itens já gerados ficam salvos.
                if _pagina_de_login(page) or _pagina_intersticial(page):
                    try:
                        _abrir_link_builder(page, usuario=usuario)
                    except AntiBotError:
                        interrompido = e
                        break
                    except (LoginError, AuthError):
                        # Sessão morta de verdade: não vale queimar 50 × 60s.
                        raise
                    # Reabriu: este item não foi culpa do produto, não registra
                    # falha nele e não conta para o teto — ele volta ao topo da fila.
                    consecutivas += 1
                    if consecutivas >= _MAX_FALHAS_CONSECUTIVAS:
                        interrompido = e
                        break
                    continue
                # Fora de uma recusa explícita da URL não sabemos atribuir a falha
                # ao produto. Timeout, DOM alterado e resposta vazia são falhas do
                # portal/automação: interrompem o lote e preservam as tentativas.
                logger.warning(
                    "Infraestrutura do Link Builder falhou no produto %s: %s",
                    getattr(prod, "id", None), e,
                )
                interrompido = e
                break

    if interrompido is not None:
        logger.warning("Lote de links ML interrompido após %s gerado(s): %s",
                       gerados, interrompido)
    logger.info("Links afiliados ML em lote: %s gerados, %s falhas", gerados, falhas)
    if (falhas or interrompido) and usuario is not None:
        from apps.scrapers.eventos import log_event
        executar_no_tenant(
            log_event,
            "scraper", "links_erro",
            f"Lote de links ML: {gerados} gerado(s), {falhas} falha(s)."
            + (" Interrompido por instabilidade do Mercado Livre."
               if interrompido else ""),
            level="warning", usuario=usuario,
            contexto={"gerados": gerados, "falhas": falhas, "total": len(pendentes),
                      "interrompido": bool(interrompido)},
            exc=interrompido or ultimo_erro,
            organization_id=organization_id,
        )
    return (gerados, falhas)


def _nome_bate(nome_esperado: str, corpo: str) -> bool:
    """True se tokens significativos (>3 chars) do nome aparecem no HTML da página."""
    if not nome_esperado:
        return True
    tokens = [t for t in re.findall(r"\w+", nome_esperado.lower()) if len(t) > 3]
    if not tokens:
        return True
    achados = sum(1 for t in tokens if t in corpo)
    # exige maioria dos tokens (≥60%) para confirmar que é o produto certo
    return achados >= max(1, int(len(tokens) * 0.6))


def verificar_link_afiliado(link_afiliado: str, screenshot_path: str = None,
                            nome_esperado: str = None, confiar_desconto: bool = False,
                            usuario=None) -> dict:
    """
    Segue o redirect do link de afiliado (meli.la -> destino) e checa se a oferta
    certa aparece com cupom/afiliado ativos.

    Aceita DOIS destinos válidos:
      - página de produto (produto.mercadolivre / /p/MLB / /MLB-id)
      - landing de afiliado /social/ que destaca o produto (com botão 'Ir para produto')
    Em ambos confirma: produto não inativo, preço visível e (se nome_esperado dado)
    que o nome do produto bate com a página.

    Transporte: HTTP por padrão (link_http), ~1s por link. O caminho com Chromium
    custava ~20-30s e não interage com a página — só lê URL final e HTML, que vêm
    no SSR. `ML_VERIFICACAO_TRANSPORTE=browser` volta ao antigo sem deploy de
    código; `screenshot_path` força o browser, porque só ele tira print.

    Retorna dict-relatório com ok/url_final/is_pagina_produto/is_landing_afiliado/
    cupom_detectado/preco_visivel/nome_confere/erros.
    """
    if not screenshot_path and getattr(
            settings, "ML_VERIFICACAO_TRANSPORTE", "http") != "browser":
        from apps.scrapers.scraper_mercadolivre.link_http import relatorio_por_http
        return relatorio_por_http(link_afiliado, nome_esperado, confiar_desconto)
    return _verificar_com_browser(
        link_afiliado, screenshot_path, nome_esperado, confiar_desconto)


def _verificar_com_browser(link_afiliado: str, screenshot_path: str = None,
                           nome_esperado: str = None,
                           confiar_desconto: bool = False) -> dict:
    """Verifica UM link abrindo um browser próprio.

    Caminho de item único (envio, backfill manual). O lote usa
    `_relatorio_na_pagina` com uma página só — abrir e fechar um Chromium por link
    custava ~2s de processo antes de qualquer trabalho útil.
    """
    if not link_afiliado:
        return {
            "ok": False, "url_final": None, "is_pagina_produto": False,
            "is_landing_afiliado": False, "cupom_detectado": False,
            "preco_visivel": None, "nome_confere": None,
            "erros": ["link_afiliado vazio."],
        }
    with coordinated_ml_browser(
        authenticated=False, owner_kind="ml_link_verify_single",
    ), iniciar_browser(headless=True) as (page, context):
        return _relatorio_na_pagina(page, link_afiliado, screenshot_path,
                                    nome_esperado, confiar_desconto)


def _relatorio_na_pagina(page, link_afiliado: str, screenshot_path: str = None,
                         nome_esperado: str = None,
                         confiar_desconto: bool = False) -> dict:
    """Faz a verificação numa página JÁ aberta — o que permite reusar o browser."""
    relatorio = {
        "ok": False, "url_final": None, "is_pagina_produto": False,
        "is_landing_afiliado": False, "cupom_detectado": False,
        "preco_visivel": None, "nome_confere": None, "erros": [],
    }
    if not link_afiliado:
        relatorio["erros"].append("link_afiliado vazio.")
        return relatorio

    try:
        page.goto(link_afiliado, wait_until="domcontentloaded", timeout=45000)
        # 3s, não 15s: a PDP do ML nunca fica ociosa (ads/tracking), então o
        # networkidle estourava o timeout inteiro em todo link — 15s de espera por
        # nada. Tudo que lemos aqui (URL final, HTML, preço) já está no DOM logo
        # após o domcontentloaded.
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        url_final = page.url
        relatorio["url_final"] = url_final

        # Interstitial do anti-bot (/gz/account-verification, captcha, challenge):
        # a página pedida NUNCA foi vista. Sem isto o relatório saía sem destino
        # válido e sem erro, e `verificar_e_aprovar` REPROVAVA um link que está
        # perfeito — bastava uma janela de bloqueio para esvaziar a tela.
        from apps.scrapers.scraper_mercadolivre.link_http import _CAMINHOS_INTERSTICIAIS
        if any(p in url_final.lower() for p in _CAMINHOS_INTERSTICIAIS):
            relatorio["erros"].append(
                "Falha ao abrir link: o Mercado Livre exigiu verificação "
                "antes de mostrar a página")
            return relatorio

        # Classificação do destino: fonte ÚNICA (link_validacao), a mesma usada
        # pela aprovação na geração e pela conferência do envio.
        relatorio["is_pagina_produto"] = eh_pagina_produto(url_final)
        # Landing de afiliado que destaca o produto (storefront /social/)
        relatorio["is_landing_afiliado"] = eh_vitrine_social(url_final)

        corpo = page.content().lower()

        termos_morto = ["anúncio pausado", "página não encontrada", "estoque indisponível"]
        if any(t in corpo for t in termos_morto):
            relatorio["erros"].append("Página indica anúncio inativo/inexistente.")

        # Cupom REAL: badge de cupom na página ("X% OFF com cupom", "cupom de R$").
        # NÃO usar só a palavra "cupom" — ela aparece no menu global "Cupons" (falso positivo).
        relatorio["cupom_detectado"] = bool(
            re.search(r"com\s+cupom|cupom\s+de\s+r?\$|%\s*off\s*com\s*cupom|aplicar\s+cupom", corpo)
        )
        if nome_esperado is not None:
            relatorio["nome_confere"] = _nome_bate(nome_esperado, corpo)

        # Preço e desconto só pesam na decisão quando `confiar_desconto` é False
        # (ver aprovado_por_relatorio: oferta/busca já teve o de/por confirmado na
        # raspagem). Para elas, estes dois `is_visible` eram 7s de espera por um
        # dado que ninguém lê — o item mais caro do orçamento por link depois do
        # próprio carregamento.
        if not confiar_desconto:
            # Preço antigo riscado SOMENTE no buybox do produto (não nos cards de
            # vitrine da /social/, que dariam falso positivo de outro item).
            try:
                relatorio["preco_riscado"] = page.locator(
                    ".ui-pdp-price s.andes-money-amount--previous, .ui-pdp-container s.andes-money-amount--previous"
                ).first.is_visible(timeout=2000)
            except Exception:
                relatorio["preco_riscado"] = False

            try:
                preco_el = page.locator(".andes-money-amount__fraction").first
                if preco_el.is_visible(timeout=5000):
                    relatorio["preco_visivel"] = "R$ " + preco_el.inner_text().strip()
            except Exception:
                pass

        if screenshot_path:
            try:
                page.screenshot(path=screenshot_path, full_page=False)
                relatorio["screenshot"] = screenshot_path
            except Exception as e:
                relatorio["erros"].append(f"screenshot falhou: {e}")

        # Decisão ÚNICA de aprovação (link_validacao.aprovado_por_relatorio):
        # a MESMA regra aplicada na aprovação (geração) e no envio.
        relatorio["ok"] = aprovado_por_relatorio(relatorio, confiar_desconto)
        if (not confiar_desconto and relatorio["is_landing_afiliado"]
                and not relatorio["is_pagina_produto"]):
            relatorio["erros"].append(
                "Caiu na vitrine /social/ (afiliado ok, mas não dá pra confirmar o cupom do item)."
            )
    except Exception as e:
        relatorio["erros"].append(f"Falha ao abrir link: {e}")

    return relatorio


def _confiar_desconto(produto) -> bool:
    """Ofertas/buscas têm de/por confirmado na raspagem; cupons precisam da PDP."""
    return getattr(produto, "origem", "cupom") in ("oferta", "busca")


def verificar_e_aprovar(usuario, produto, link_afiliado, url_isca="") -> str:
    """Roda a verificação de DESTINO e PERSISTE o veredito. Fonte única de aprovação.

    Retorna 'aprovado' | 'reprovado' | 'transitorio'. Só 'aprovado' torna o item
    enviável (verificado_ok=True + url_canonica). Uma falha de rede é transitória:
    NÃO reprova o link (não some da tela por causa de um erro passageiro) — fica
    verificado_ok=None para a próxima passada tentar de novo.
    """
    if not link_afiliado:
        return "transitorio"
    confiar = _confiar_desconto(produto)
    relatorio = verificar_link_afiliado(
        link_afiliado, nome_esperado=getattr(produto, "nome", None),
        confiar_desconto=confiar, usuario=usuario)
    if relatorio.get("ok"):
        registrar_aprovacao(usuario, produto, link_afiliado, url_canonica=link_afiliado)
        return "aprovado"
    # Não abriu o link (rede/timeout) => transitório: mantém o veredito pendente.
    if any("Falha ao abrir link" in str(e) for e in relatorio.get("erros", [])):
        return "transitorio"
    registrar_reprovacao(usuario, produto, motivo_reprovacao(relatorio, confiar))
    return "reprovado"


def verificar_links_pendentes(usuario, limite=20, produto_ids=None) -> dict:
    """Verifica o destino dos links gerados mas ainda sem veredito (verificado_ok
    IS NULL) e persiste o resultado. É o passo que torna um link enviável ANTES de
    a promoção aparecer na tela de envio — antes disto, o veredito só era calculado
    no clique de enviar (a inconsistência que este módulo corrige).
    """
    if usuario is None:
        return {"aprovados": 0, "reprovados": 0, "transitorios": 0}
    from apps.scrapers.models import LinkAfiliadoUsuario

    def _carregar():
        from apps.accounts.models import organization_for_user

        agora = timezone.now()
        queryset = (
            LinkAfiliadoUsuario.objects
            .filter(usuario=usuario, verificado_ok__isnull=True)
            .filter(Q(proxima_tentativa__isnull=True) | Q(proxima_tentativa__lte=agora))
            .exclude(link_afiliado="")
            .select_related("produto")
        )
        if produto_ids is not None:
            queryset = queryset.filter(produto_id__in=list(produto_ids))
        linhas_locais = list(
            queryset.order_by(
                F("proxima_tentativa").asc(nulls_first=True), "id",
            )[:max(1, min(int(limite), 80))]
        )
        organization = organization_for_user(usuario)
        return linhas_locais, getattr(organization, "pk", None)

    try:
        linhas, org_id = executar_no_tenant(_carregar)
    except ValueError:
        # Chamadas diretas de comandos/testes não carregam o tenant suspenso;
        # ainda assim toda navegação acontece só depois desta consulta curta.
        linhas, org_id = _carregar()
    if not linhas:
        return {"aprovados": 0, "reprovados": 0, "transitorios": 0}
    aprovados = reprovados = transitorios = 0

    def _adiar(linha, motivo="Verificação temporariamente indisponível."):
        LinkAfiliadoUsuario.objects.filter(pk=linha.pk).update(
            proxima_tentativa=timezone.now() + timedelta(minutes=15),
            verificacao_motivo=motivo,
        )

    def _no_tenant(fn, *args, **kwargs):
        return executar_no_tenant(fn, *args, organization_id=org_id, **kwargs)

    # UM browser para o lote inteiro. Abrir e fechar um Chromium por link custava
    # ~2s de processo antes de qualquer trabalho útil — num lote de 40, mais de um
    # minuto só subindo e derrubando navegador.
    #
    # Com o Playwright vivo nesta thread, o ORM levanta SynchronousOnlyOperation:
    # por isso as gravações vão por executar_no_tenant, que as desvia para a thread
    # dedicada (a mesma solução que gerar_links_em_lote já usa).
    from apps.scrapers.carga import BrowserResourceUnavailable, browser_resource

    with browser_resource(owner_kind="links_verify") as acquired:
        if not acquired:
            raise BrowserResourceUnavailable(
                "Capacidade de browser ocupada; verificação será retomada."
            )
        with iniciar_browser(headless=True) as (page, _ctx):
            for linha in linhas:
                confiar = _confiar_desconto(linha.produto)
                try:
                    relatorio = _relatorio_na_pagina(
                        page, linha.link_afiliado,
                        nome_esperado=getattr(linha.produto, "nome", None),
                        confiar_desconto=confiar)
                except Exception as e:
                    logger.warning("Verificação de destino falhou p/ produto %s: %s",
                                   getattr(linha.produto, "id", None), e)
                    _no_tenant(_adiar, linha)
                    transitorios += 1
                    continue
                if relatorio.get("ok"):
                    _no_tenant(registrar_aprovacao, usuario, linha.produto,
                                       linha.link_afiliado,
                                       url_canonica=linha.link_afiliado)
                    aprovados += 1
                elif any("Falha ao abrir link" in str(e)
                         for e in relatorio.get("erros", [])):
                    # Rede/timeout/challenge: o destino nunca foi visto.
                    _no_tenant(_adiar, linha)
                    transitorios += 1
                else:
                    _no_tenant(registrar_reprovacao, usuario, linha.produto,
                                       motivo_reprovacao(relatorio, confiar))
                    reprovados += 1
    logger.info("Verificação de destino ML p/ %s: %s aprovado(s), %s reprovado(s), "
                "%s transitório(s)", usuario, aprovados, reprovados, transitorios)
    return {"aprovados": aprovados, "reprovados": reprovados,
            "transitorios": transitorios}


def produto_vencedor_do_cupom(campanha_id: str):
    """
    Retorna o Produto com o maior desconto absoluto (R$) para um dado campanha_id.
    Exige que Django esteja configurado (DJANGO_SETTINGS_MODULE) antes de chamar.
    """
    from apps.scrapers.models import Produto
    return (
        Produto.objects
        .filter(campanha_id=campanha_id)
        .extra(select={"economia": "preco_sem_desconto - preco_com_cupom"})
        .order_by("-economia")
        .first()
    )


def gerar_link_afiliado_para_produto(produto, usuario=None):
    """
    Deep-link direto para o produto isca com coupon_campaign_id injetado na URL.
    Isso garante que o usuário caia exatamente no produto anunciado (não num
    container dinâmico que pode ter trocado a ordem) e o ML exibe a barra de
    cupom aplicável automaticamente.

    usuario != None (multi-tenant): gera com a SESSÃO do usuário (auth_{id}.json).
    Sem sessão própria, falha e pede reconexão; nunca cai no auth.json global.
    usuario == None: comportamento single-tenant antigo.

    Retorna:
        {
          "link_afiliado": "https://meli.la/...",
          "produto_nome":  "...",
          "preco_vitrine": 1299.90,
          "preco_com_cupom": 1169.91,
          "cupom_titulo":  "10% OFF em Smartphones",
          "url_isca": "https://produto.mercadolivre.com.br/...?coupon_campaign_id=...",
        }
        ou None em caso de falha.
    """
    from apps.scrapers.models import Cupom

    camp_id = produto.campanha_id if hasattr(produto, "campanha_id") else produto["campanha_id"]
    url_produto = produto.link_produto if hasattr(produto, "link_produto") else produto["link_produto"]

    # Todo ORM daqui para baixo passa por _no_tenant. LinkAfiliadoUsuario e
    # MercadoLivreSession estão sob RLS, e esta função é o caminho do envio manual,
    # que roda em SSE com o tenant apenas ANOTADO: a query nua volta zero linhas, o
    # cache do link some e has_storage_state diz "sem sessão" — era o
    # "Sessão do Mercado Livre expirada" com a tela de conexão verde.
    organization_id = _organization_id_de(usuario)
    _no_tenant = _executor_no_tenant(organization_id)

    # Ofertas (origem='oferta') não têm Cupom; cupom fica None
    cupom = None
    if camp_id:
        cupom = Cupom.objects.filter(campanha_id=camp_id).first()
        if cupom is None:
            # A raspagem de cupons de campanha precisa ter rodado antes: sem a linha
            # de Cupom não dá para montar a URL com coupon_campaign_id. Retentável —
            # a próxima raspagem pode trazer o cupom.
            logger.info("Cupom %s nao encontrado no banco", camp_id)
            _no_tenant(registrar_falha, usuario, produto,
                       f"A campanha {camp_id} deste produto ainda não foi raspada.")
            return None

    if not url_produto:
        logger.info("Produto sem link_produto salvo (campanha %s)", camp_id)
        _no_tenant(registrar_falha, usuario, produto,
                   "O produto não tem link de origem.", terminal=True)
        return None

    # ── Multi-tenant: link por usuário, sempre com a sessão ML dele ──
    if usuario is not None:
        from apps.scrapers.afiliado import link_cacheado, salvar_cache

        cacheado = _no_tenant(link_cacheado, usuario, produto)
        if cacheado and cacheado.link_afiliado:
            # O envio ainda passa pelo gate A3 e, quando solicitado, pela
            # revalidação da PDP em ofertas.py. Reaproveitar o cache aqui só
            # evita abrir o Link Builder novamente — e faz um link já pronto
            # continuar utilizável mesmo que a sessão ML tenha expirado.
            return {
                "link_afiliado": cacheado.link_afiliado,
                "afiliado_ok": bool(cacheado.afiliado_ok),
                # Veredito persistido (fonte única): o envio confia nele e usa a
                # url_canonica em vez de reconferir com uma regra divergente.
                "verificado_ok": cacheado.verificado_ok,
                "url_canonica": cacheado.url_canonica or cacheado.link_afiliado,
                "verificacao_motivo": cacheado.verificacao_motivo,
                "produto_nome": getattr(produto, "nome", ""),
                "preco_vitrine": getattr(produto, "preco_sem_desconto", 0),
                "preco_com_cupom": getattr(produto, "preco_com_cupom", 0),
                "cupom_titulo": cupom.titulo if cupom else "",
                "url_isca": cacheado.url_isca or _montar_url_isca(
                    url_produto, camp_id),
            }

        if not _no_tenant(has_storage_state, usuario):
            # Não há sessão NENHUMA — nada foi aberto, então não é a mensagem do
            # Link Builder. Antes as duas causas compartilhavam o mesmo texto e
            # "pediu login de novo" aparecia para quem nunca havia conectado.
            raise LoginError(MSG_SEM_SESSAO)
        url_isca = _montar_url_isca(url_produto, camp_id)
        if not url_isca:
            motivo = _motivo_url_recusada(url_produto)
            logger.info("URL de produto invalida para afiliacao ML: %s", motivo)
            _no_tenant(registrar_falha, usuario, produto, motivo, terminal=True)
            return None
        link_afiliado = afiliate_link_builder(url_isca, usuario=usuario)
        if not link_afiliado:
            # afiliate_link_builder já logou a causa; aqui garantimos que ela também
            # fique gravada no item, senão a tela só sabe dizer "pendente".
            _no_tenant(
                registrar_falha, usuario, produto,
                "O Programa de Afiliados não aceitou a URL deste produto.",
                terminal=True,
            )
            return None
        afiliado_ok = True
        _no_tenant(salvar_cache, usuario, produto, link_afiliado, url_isca, afiliado_ok)
        return {
            "link_afiliado": link_afiliado, "afiliado_ok": afiliado_ok,
            "produto_nome": getattr(produto, "nome", ""),
            "preco_vitrine": getattr(produto, "preco_sem_desconto", 0),
            "preco_com_cupom": getattr(produto, "preco_com_cupom", 0),
            "cupom_titulo": cupom.titulo if cupom else "",
            "url_isca": url_isca,
        }

    # Usa cache se já foi pré-gerado em lote
    cache_link = getattr(produto, "link_afiliado", "") if hasattr(produto, "link_afiliado") else ""
    cache_isca = getattr(produto, "url_isca", "") if hasattr(produto, "url_isca") else ""

    if cache_link:
        url_isca = cache_isca or _montar_url_isca(url_produto, camp_id)
        link_afiliado = cache_link
    else:
        url_isca = _montar_url_isca(url_produto, camp_id)
        if not url_isca:
            logger.info("URL de produto invalida para afiliacao ML")
            return None
        logger.debug("Gerando afiliado ML para produto")
        link_afiliado = afiliate_link_builder(url_isca)
        if not link_afiliado:
            return None
        # Persiste no cache se for instância de modelo
        if hasattr(produto, "save"):
            produto.url_isca = url_isca
            produto.link_afiliado = link_afiliado
            produto.afiliado_ok = True
            produto.save(update_fields=["url_isca", "link_afiliado", "afiliado_ok"])

    return {
        "link_afiliado": link_afiliado,
        "afiliado_ok": True,
        "produto_nome": produto.nome if hasattr(produto, "nome") else produto["nome"],
        "preco_vitrine": produto.preco_sem_desconto if hasattr(produto, "preco_sem_desconto") else produto["preco_sem_desconto"],
        "preco_com_cupom": produto.preco_com_cupom if hasattr(produto, "preco_com_cupom") else produto["preco_com_cupom"],
        "cupom_titulo": cupom.titulo if cupom else "",
        "url_isca": url_isca,
    }
