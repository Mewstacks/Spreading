"""Verificação de destino do link de afiliado por HTTP puro — sem Chromium.

⚠️ DESLIGADO POR PADRÃO (settings.ML_VERIFICACAO_TRANSPORTE="browser").

Resultado medido em 2026-07-29: o ML responde `/gz/account-verification?go=<destino>`
a QUALQUER cliente HTTP sem fingerprint de browser, com status 200 e corpo grande.
Testado do IP residencial e do datacenter da Fly, com User-Agent completo, com o
conjunto inteiro de headers Sec-Fetch e com cookies aquecidos por uma visita à home
— todos caíram no mesmo interstitial. O bloqueio é por TLS/JA3, que `requests` não
tem como imitar.

O módulo fica aqui, testado e pronto, porque a troca é uma variável de ambiente: se
o ML afrouxar, `ML_VERIFICACAO_TRANSPORTE=http` liga sem deploy de código. E o
aprendizado mais importante dele já foi levado para o caminho com browser: o
interstitial precisa virar TRANSITÓRIO, nunca reprovação (ver `_CAMINHOS_INTERSTICIAIS`).

Cuidado relacionado: `ofertas.esta_vivo` também usa `requests.get` contra a PDP e
só procura "Anúncio pausado" no corpo. Contra o interstitial ele não encontra o
termo e conclui "vivo" — um falso positivo silencioso que vale corrigir à parte.


A verificação NÃO interage com a página: ela só segue o redirect (meli.la -> PDP),
lê a URL final e procura marcadores no HTML. Tudo isso vem no HTML server-side do
ML, então subir um Chromium por link custava ~20-30s (goto 45s + networkidle 15s +
dois `is_visible` de 2s e 5s) para fazer o trabalho de um GET.

O mesmo transporte já é usado no projeto contra a mesma PDP: `ofertas.esta_vivo`
faz `requests.get` e procura "Anúncio pausado" no corpo.

REGRA CENTRAL — challenge nunca reprova. No IP de datacenter da Fly o anti-bot do
ML redireciona navegações legítimas para challenge/login (ver
auxiliar.iniciar_browser). Sem browser a exposição é maior, então qualquer sinal de
bloqueio vira "Falha ao abrir link", que `verificar_e_aprovar` trata como
TRANSITÓRIO. Reprovar nesse caso derrubaria centenas de links bons de uma vez e
esvaziaria a tela de Promoções.

A decisão de aprovar continua sendo de `link_validacao.aprovado_por_relatorio` —
fonte única compartilhada com o caminho de browser e com o envio.
"""
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from apps.scrapers.auxiliar import _redirecionou_login, ua_aleatorio
from apps.scrapers.link_validacao import (
    aprovado_por_relatorio, eh_pagina_produto, eh_vitrine_social,
)
from apps.scrapers.sources.base import normalizar_dinheiro

logger = logging.getLogger(__name__)

# (connect, read). O ML responde o HTML SSR rápido; quem demora é o resto da página,
# que não nos interessa.
_TIMEOUT = (5, 15)

_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def _url_ml_permitida(url: str) -> bool:
    """Restringe egress a encurtadores e domínios oficiais do Mercado Livre."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return (
        host == "meli.la"
        or host == "mercadolivre.com.br"
        or host.endswith(".mercadolivre.com.br")
        or host == "mercadolivre.com"
        or host.endswith(".mercadolivre.com")
        or host == "mercadolibre.com"
        or host.endswith(".mercadolibre.com")
    )


def _get_ml(sessao, url: str, *, timeout):
    """GET com redirects manuais; valida cada hop antes de abrir a conexão."""
    atual = url
    historico = []
    for _ in range(_MAX_REDIRECTS + 1):
        if not _url_ml_permitida(atual):
            raise ValueError("destino fora dos domínios permitidos do Mercado Livre")
        resp = sessao.get(atual, allow_redirects=False, timeout=timeout)
        if resp.status_code not in _REDIRECTS:
            try:
                resp.history = historico
            except Exception:
                pass
            return resp
        location = (getattr(resp, "headers", {}) or {}).get("location")
        if not location:
            return resp
        historico.append(resp)
        atual = urljoin(atual, location)
    raise ValueError("redirecionamentos demais ao abrir link do Mercado Livre")

_TERMOS_MORTO = ("anúncio pausado", "página não encontrada", "estoque indisponível")
_RE_CUPOM = re.compile(r"com\s+cupom|cupom\s+de\s+r?\$|%\s*off\s*com\s*cupom|aplicar\s+cupom")
_RE_PRECO = re.compile(
    r'andes-money-amount__fraction[^>]*>\s*([\d][\d.  ]{0,12})\s*<')
# Marcadores de que veio uma página real do ML (e não um interstitial de challenge).
_MARCADORES_ML = ("andes-", "ui-pdp", "nav-header", "mercadolivre")

# Interstitials do ML que NÃO são o destino pedido. `_redirecionou_login` cobre só
# /login, /lgz/, /registration e loginhub — e a verificação real caiu em
# `/gz/account-verification?go=<destino>`, que passava batido: status 200, corpo
# grande, e o `go=` na URL ainda fazia o nome do produto "bater". O relatório saía
# sem erro nenhum e o link ia para REPROVADO, quando na verdade nunca foi visto.
_CAMINHOS_INTERSTICIAIS = (
    "/gz/", "account-verification", "/captcha", "/challenge", "/blocked",
    "/security-check", "/verificacion", "/verify",
)


def _sessao():
    """Sessão anônima com cara de browser. Sem cookies: verificar é público, e é
    assim que o caminho com Chromium já fazia (`validar_sessao=False`, sem
    storage_state)."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ua_aleatorio(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    })
    return sess


def _motivo_bloqueio(resp) -> str:
    """Descreve por que a resposta não serve para julgar o link — ou "" se serve.

    Tudo que sai daqui não-vazio vira erro "Falha ao abrir link", ou seja,
    TRANSITÓRIO. 404/410 NÃO entram aqui: anúncio que sumiu é reprovação legítima.
    """
    if _redirecionou_login(resp.url):
        return "o Mercado Livre redirecionou para login/challenge"
    baixo_url = (resp.url or "").lower()
    if any(p in baixo_url for p in _CAMINHOS_INTERSTICIAIS):
        return "o Mercado Livre exigiu verificação antes de mostrar a página"
    if resp.status_code in (401, 403, 429):
        return f"o Mercado Livre respondeu {resp.status_code} (bloqueio/limite)"
    if resp.status_code >= 500:
        return f"o Mercado Livre respondeu {resp.status_code}"
    if resp.status_code not in (200, 404, 410):
        return f"resposta inesperada ({resp.status_code})"
    corpo = resp.text or ""
    # Interstitial de challenge é curto e não traz nenhum componente do ML. Uma PDP
    # real tem centenas de KB e sempre carrega as classes `andes-`.
    if resp.status_code == 200 and len(corpo) < 20000:
        baixo = corpo.lower()
        if not any(m in baixo for m in _MARCADORES_ML):
            return "a resposta não parece uma página do Mercado Livre (challenge?)"
    return ""


def _preco_riscado_no_buybox(corpo: str) -> bool:
    """Preço "de" riscado dentro do buybox do produto.

    O caminho com browser usa `.ui-pdp-price s..., .ui-pdp-container s...` justamente
    para NÃO pegar os cards da vitrine /social/, que dariam falso positivo de outro
    item. Aqui a mesma intenção: só procura depois do início do bloco do produto.
    """
    fatia = _fatia_do_buybox(corpo)
    return bool(fatia) and "andes-money-amount--previous" in fatia


def _fatia_do_buybox(corpo: str) -> str:
    """Trecho do HTML que começa no bloco de preço do produto.

    Mesma intenção de sempre: NÃO olhar os cards da vitrine /social/ nem os
    carrosséis de recomendação, que dariam o preço de outro item.
    """
    inicio = corpo.find("ui-pdp-price")
    if inicio < 0:
        inicio = corpo.find("ui-pdp-container")
    if inicio < 0:
        return ""
    return corpo[inicio:inicio + 8000]


class _PrecoBuyboxParser(HTMLParser):
    """Preço corrente e preço riscado dentro do bloco de preço da PDP.

    Não conta profundidade de tags: o HTML do ML tem void tags de sobra e um
    contador de `div` erra silenciosamente. Aqui bastam dois estados, que é o
    mesmo caminho que `_MLCardsHTMLParser` (coupon_products) usa nos cards.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.atual = {"fraction": "", "cents": ""}
        self.anterior = {"fraction": "", "cents": ""}
        self._em_anterior = False
        # O parcelamento ("em 10x R$ 179,90") também é `andes-money-amount`, e vem
        # depois do preço no `ui-pdp-price__subtitles`. Uma vez dentro dele nada
        # mais interessa — o que vier a seguir na fatia já é outro componente.
        self._em_subtitulo = False
        self._capturando = None
        self._buffer = []

    @staticmethod
    def _classes(attrs):
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag, attrs):
        classes = self._classes(attrs)
        if "ui-pdp-price__subtitles" in classes:
            self._em_subtitulo = True
        if self._em_subtitulo:
            return
        if tag == "s" and "andes-money-amount--previous" in classes:
            self._em_anterior = True
            return
        alvo = self.anterior if self._em_anterior else self.atual
        if "andes-money-amount__fraction" in classes:
            # O primeiro preço corrente é o do buybox; o resto da fatia pode
            # trazer variação/frete e não pode sobrescrevê-lo.
            if not alvo["fraction"]:
                self._capturando, self._buffer = (alvo, "fraction"), []
        elif "andes-money-amount__cents" in classes:
            if not alvo["cents"]:
                self._capturando, self._buffer = (alvo, "cents"), []

    def handle_data(self, data):
        if self._capturando:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if self._capturando:
            alvo, campo = self._capturando
            alvo[campo] = "".join(self._buffer).strip()
            self._capturando, self._buffer = None, []
        if tag == "s":
            self._em_anterior = False

    @staticmethod
    def _valor(parte) -> float:
        if not parte["fraction"]:
            return 0.0
        # A vírgula manda: normalizar_dinheiro trata o ponto como milhar e evita
        # que "1.799" vire 179900. Fonte única de conversão, sem um quarto parser.
        return normalizar_dinheiro(f"{parte['fraction']},{parte['cents'] or '00'}")


def _brl(valor: float) -> str:
    """"R$ 1.799,90" — como a página mostra, não como o float sai."""
    inteiro, _, centavos = f"{valor:,.2f}".partition(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def preco_da_pdp(corpo: str) -> tuple[float, float]:
    """(preço do buybox, preço "de" riscado) da PDP do ML; 0.0 quando não achou.

    `_RE_PRECO` pega a PRIMEIRA fração do documento inteiro e sem centavos: numa
    PDP com preço riscado — que vem ANTES do corrente — ele devolve o "DE", e sem
    centavos erra qualquer preço terminado em vírgula. Serve para o gate booleano
    de "tem preço visível", não para dizer quanto o produto custa.
    """
    fatia = _fatia_do_buybox(corpo)
    if not fatia:
        return 0.0, 0.0
    parser = _PrecoBuyboxParser()
    try:
        parser.feed(fatia)
    except Exception:
        logger.debug("Parser de preço da PDP falhou", exc_info=True)
        return 0.0, 0.0
    atual = parser._valor(parser.atual)
    anterior = parser._valor(parser.anterior)
    # Riscado menor que o corrente é dado corrompido, não promoção.
    return atual, (anterior if anterior > atual else 0.0)


def relatorio_por_http(link_afiliado: str, nome_esperado: str = None,
                       confiar_desconto: bool = False, sessao=None) -> dict:
    """Mesmo relatório de `verificar_link_afiliado`, obtido sem browser.

    `sessao` permite injetar o transporte AUTENTICADO (`ml_auth.http_session`). O
    GET anônimo cai no interstitial contra a PDP — ver o topo deste módulo e
    `coupon_products._coletar_ml_remoto`, que já lê o mesmo host com cookies.
    """
    from apps.scrapers.scraper_mercadolivre.link import _nome_bate

    relatorio = {
        "ok": False, "url_final": None, "is_pagina_produto": False,
        "is_landing_afiliado": False, "cupom_detectado": False,
        "preco_visivel": None, "nome_confere": None, "erros": [],
    }
    if not link_afiliado:
        relatorio["erros"].append("link_afiliado vazio.")
        return relatorio

    try:
        resp = _get_ml(sessao if sessao is not None else _sessao(),
                       link_afiliado, timeout=_TIMEOUT)
    except Exception as e:
        relatorio["erros"].append(f"Falha ao abrir link: {e}")
        return relatorio

    bloqueio = _motivo_bloqueio(resp)
    if bloqueio:
        relatorio["erros"].append(f"Falha ao abrir link: {bloqueio}")
        return relatorio

    relatorio["url_final"] = resp.url
    relatorio["is_pagina_produto"] = eh_pagina_produto(resp.url)
    relatorio["is_landing_afiliado"] = eh_vitrine_social(resp.url)

    corpo = (resp.text or "").lower()
    if resp.status_code in (404, 410) or any(t in corpo for t in _TERMOS_MORTO):
        relatorio["erros"].append("Página indica anúncio inativo/inexistente.")

    relatorio["cupom_detectado"] = bool(_RE_CUPOM.search(corpo))

    if nome_esperado is not None:
        relatorio["nome_confere"] = _nome_bate(nome_esperado, corpo)

    # Preço e desconto só entram na decisão quando `confiar_desconto` é False
    # (ver aprovado_por_relatorio). Para oferta/busca extraí-los é trabalho jogado
    # fora — no caminho com browser eram 7s por link só nos dois `is_visible`.
    if not confiar_desconto:
        relatorio["preco_riscado"] = _preco_riscado_no_buybox(corpo)
        preco, _preco_de = preco_da_pdp(corpo)
        if preco > 0:
            # `preco_visivel` é lido como gate BOOLEANO em aprovado_por_relatorio;
            # continua string truthy. O número vai à parte, para quem precisa dele.
            relatorio["preco_visivel"] = _brl(preco)
            relatorio["preco_numerico"] = preco
        else:
            achado = _RE_PRECO.search(corpo)
            if achado:
                relatorio["preco_visivel"] = "R$ " + achado.group(1).strip()

    relatorio["ok"] = aprovado_por_relatorio(relatorio, confiar_desconto)
    if (not confiar_desconto and relatorio["is_landing_afiliado"]
            and not relatorio["is_pagina_produto"]):
        relatorio["erros"].append(
            "Caiu na vitrine /social/ (afiliado ok, mas não dá pra confirmar o cupom do item)."
        )
    return relatorio


def relatorio_de_link_com_cupom(link_afiliado: str, *, desconto_comprovado: bool,
                                sessao=None) -> dict:
    """Relatório de um link cujo desconto foi provado ANTES, na coleta.

    Dois fatos medidos em produção (13/08/2026) definem esta função.

    **1. Todo short link do Programa resolve para a vitrine do afiliado**, não para
    o anúncio — inclusive os 10.807 links de oferta que o sistema já aprovava::

        https://meli.la/1EStDY1  (produto de cupom, reprovado)
        https://meli.la/15Grqae  (produto de oferta, APROVADO)
        ambos -> https://www.mercadolivre.com.br/social/<afiliado>?matt_word=…&ref=<opaco>

    O item viaja dentro do `ref` cifrado. Exigir `is_pagina_produto` NO DESTINO é,
    portanto, uma condição que nenhum link do ML satisfaz. Quem passava só passava
    porque `confiar_desconto=True` dispensa a prova; os produtos de cupom, que
    precisam dela, eram reprovados 100% das vezes ("Caiu na vitrine /social/", 262
    de 391 verificações em 3h; 0 aprovados em 4.447) — era isso, e não a coleta,
    que prendia o catálogo inteiro em "aguardando link".

    **2. A PDP não responde a GET do IP da Fly.** Sessão autenticada, sessão
    anônima, com e sem `coupon_campaign_id`: as três voltam 200 em
    `/gz/account-verification`. Ler o desconto ao vivo na página do produto não é
    uma opção neste ambiente — é a mesma parede documentada no topo deste módulo.

    Sobra a evidência que JÁ temos, e que é de primeira mão: `preco_sem_desconto` e
    `preco_com_cupom` vêm da própria listagem oficial do cupom no ML (ver
    `coupon_products._coletar_ml_remoto`), e `calcular_precos` já os validou em
    `ProdutoCupom.preco_final`. É o mesmo padrão de confiança que `oferta`/`busca`
    têm desde sempre: o de/por confirmado na coleta vale, e a verificação de
    destino responde só pelo destino.

    Então aqui ficam as perguntas que este transporte CONSEGUE responder:
      1. o link leva a um destino do Programa e está vivo?  → lido no short link;
      2. o desconto está comprovado?                        → `desconto_comprovado`,
         resolvido pelo chamador a partir de `ProdutoCupom`.
    """
    relatorio = relatorio_por_http(link_afiliado, nome_esperado=None,
                                   confiar_desconto=True, sessao=sessao)
    relatorio["evidencia_origem"] = bool(desconto_comprovado)
    if relatorio.get("erros"):
        # Challenge/timeout no encurtador: TRANSITÓRIO, nunca reprovação.
        relatorio["ok"] = False
        return relatorio
    if not desconto_comprovado:
        relatorio["ok"] = False
        relatorio["erros"] = [
            "O desconto deste cupom não está comprovado para o produto."
        ]
        return relatorio
    # `aprovado_por_relatorio` com confiar_desconto=True: destino de afiliado
    # válido, anúncio vivo. O nome não é conferido porque a vitrine não traz o
    # item — conferi-lo aqui reprovaria todo link do Programa.
    relatorio["ok"] = aprovado_por_relatorio(relatorio, True)
    return relatorio


# (connect, read) do caminho de ENVIO. `_TIMEOUT` é dimensionado para lote noturno
# e penduraria até 20s por item; nove itens de uma colagem seriam minutos.
_TIMEOUT_ENVIO = (3, 6)


def relatorio_de_preco(link: str, *, sessao=None, timeout=None) -> dict:
    """Preço ao vivo da página que o assinante vai abrir. Um GET, sem browser.

    `sessao` existe porque o GET anônimo NÃO passa no ML (ver o topo deste
    módulo: o bloqueio é por TLS/JA3). Quem chama injeta aqui a sessão com os
    cookies do storage_state — `ml_auth.http_session`, o mesmo transporte que
    coupon_products usa como caminho primário contra este host.

    `bloqueio` não-vazio significa INCONCLUSIVO, nunca "o preço está errado":
    challenge do anti-bot não pode virar veredito sobre a oferta.
    """
    resultado = {"preco": 0.0, "preco_de": 0.0, "url_final": "", "bloqueio": ""}
    if not link:
        resultado["bloqueio"] = "link vazio"
        return resultado
    try:
        resp = _get_ml(
            sessao or _sessao(), link, timeout=timeout or _TIMEOUT_ENVIO,
        )
    except Exception as e:
        resultado["bloqueio"] = f"falha ao abrir link: {e}"
        return resultado

    bloqueio = _motivo_bloqueio(resp)
    if bloqueio:
        resultado["bloqueio"] = bloqueio
        return resultado

    resultado["url_final"] = resp.url
    corpo = (resp.text or "").lower()
    if resp.status_code in (404, 410) or any(t in corpo for t in _TERMOS_MORTO):
        resultado["morto"] = True
        return resultado

    resultado["preco"], resultado["preco_de"] = preco_da_pdp(corpo)
    if resultado["preco"] <= 0:
        # Página abriu e não é challenge, mas o preço não saiu: layout novo ou
        # bloco fora da fatia. Também é inconclusivo — jamais um preço zero.
        resultado["bloqueio"] = "preço não encontrado na página"
    return resultado
