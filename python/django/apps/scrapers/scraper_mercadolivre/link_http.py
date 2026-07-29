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

import requests

from apps.scrapers.auxiliar import _redirecionou_login, ua_aleatorio
from apps.scrapers.link_validacao import (
    aprovado_por_relatorio, eh_pagina_produto, eh_vitrine_social,
)

logger = logging.getLogger(__name__)

# (connect, read). O ML responde o HTML SSR rápido; quem demora é o resto da página,
# que não nos interessa.
_TIMEOUT = (5, 15)

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
    inicio = corpo.find("ui-pdp-price")
    if inicio < 0:
        inicio = corpo.find("ui-pdp-container")
    if inicio < 0:
        return False
    return "andes-money-amount--previous" in corpo[inicio:inicio + 8000]


def relatorio_por_http(link_afiliado: str, nome_esperado: str = None,
                       confiar_desconto: bool = False) -> dict:
    """Mesmo relatório de `verificar_link_afiliado`, obtido sem browser."""
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
        resp = _sessao().get(link_afiliado, allow_redirects=True, timeout=_TIMEOUT)
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
