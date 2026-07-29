"""Casa cupons de CONTAINER (fonte oficial de afiliados) com os produtos rastreados.

A página de afiliados diz que um cupom vale para um `_Container_...` (uma lista
curada de produtos participantes), mas não quais produtos são esses. Aqui a gente
abre a lista de cada container, coleta os item ids (MLB...) que aparecem e cruza com
os produtos que já rastreamos — gerando um ProdutoCupom 'confirmado'. É esse vínculo
confirmado que libera o cupom a entrar na mensagem (ver ofertas._melhor_cupom_normalizado).

O cruzamento é por item id MLB (robusto a parâmetros de tracking na URL), não pela URL
crua. Cupons site-wide (is_mar_aberto) não passam por aqui: valem para todo item.

A parte de navegador fica isolada num "coletor" injetável, então a lógica de
casamento é testável sem Playwright.
"""
import logging
import re
import time

from django.db.models import Max, Q
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser, pausa_humana
from apps.scrapers.ml_auth import avisar_sem_sessao, storage_state
from apps.scrapers.models import CupomNormalizado, Produto, ProdutoCupom
from .link import _extrair_item_id
from .scraper import _ml_http_session

logger = logging.getLogger(__name__)

# Teto de cupons por passada. Sem ele a varredura pegava TODOS os cupons ativos com
# container — em homologação, 2357 — a 2 páginas de navegação cada, e levava horas.
LIMITE_CUPONS = 60
# Teto de tempo. Este passo roda dentro do ciclo de scrape completo, num
# `try/except` que só loga: sem orçamento, uma coleta longa não falha — ela ocupa o
# ciclo inteiro e nada mais roda.
ORCAMENTO_S = 300

_RE_HREF = re.compile(r'href="([^"]*MLB[^"]*)"', re.I)


def _cupons_de_container(limite=LIMITE_CUPONS):
    """CupomNormalizado ativos, com container e que NÃO são site-wide.

    Ordenados por quem mais precisa: nunca casado primeiro, depois o casamento mais
    antigo. Sem o `limite` isto pegava TODOS os cupons ativos com container — em
    homologação, 2357 — e cada um custa 2 páginas de navegação. A varredura levava
    horas dentro de um `try/except` que só loga, ou seja, ela virava o ciclo inteiro.
    """
    agora = timezone.now()
    qs = CupomNormalizado.objects.filter(
        marketplace="mercadolivre", estado="ativo",
    ).filter(Q(validade__isnull=True) | Q(validade__gte=agora))
    elegiveis = [c for c in qs
                 if (c.regras or {}).get("container_url")
                 and not (c.regras or {}).get("is_mar_aberto")]
    if not elegiveis:
        return []
    # Uma query só para saber quando cada cupom foi casado pela última vez.
    visto_em = dict(
        ProdutoCupom.objects
        .filter(cupom_id__in=[c.id for c in elegiveis], evidencia__regra="container")
        .values_list("cupom_id")
        .annotate(ultimo=Max("verificado_em"))
    )
    elegiveis.sort(key=lambda c: (visto_em.get(c.id) is not None,
                                  visto_em.get(c.id) or agora))
    return elegiveis[:limite]


def _indexar_produtos():
    """item_id MLB -> [Produto] entre os produtos ML ativos que rastreamos."""
    idx = {}
    for p in Produto.objects.filter(
            marketplace="mercadolivre", estado="ativo").only("id", "link_produto"):
        iid = _extrair_item_id(p.link_produto or "")
        if iid:
            idx.setdefault(iid, []).append(p)
    return idx


def _confirmar(cupom, ids_container, idx, agora):
    """Grava ProdutoCupom 'confirmado' para cada produto rastreado no container."""
    n = 0
    for iid in ids_container & set(idx):
        for prod in idx[iid]:
            ProdutoCupom.objects.update_or_create(
                produto=prod, cupom=cupom,
                defaults={
                    "status": "confirmado", "verificado_em": agora,
                    "evidencia": {"regra": "container", "item_id": iid,
                                  "container": (cupom.regras or {}).get("container_name")},
                },
            )
            n += 1
    return n


def _ids_do_html(texto):
    """item ids MLB dos links de um HTML. Compartilhado pelos dois transportes."""
    ids = set()
    for href in _RE_HREF.findall(texto or ""):
        iid = _extrair_item_id(href)
        if iid:
            ids.add(iid)
    return ids


def _coletar_ids_da_pagina(page):
    """Todos os item ids MLB presentes nos links da página atual do container.

    UMA chamada ao browser, não uma por âncora. A versão anterior fazia
    `anchors.nth(i).get_attribute(timeout=1000)` item a item — numa listagem do ML
    são 200-600 âncoras, ou seja, centenas de round-trips de protocolo por página,
    cada um com seu timeout.
    """
    try:
        hrefs = page.eval_on_selector_all(
            "a[href*='MLB']",
            "els => els.map(e => e.getAttribute('href') || '')")
    except Exception:
        return set()
    ids = set()
    for href in hrefs or []:
        iid = _extrair_item_id(href or "")
        if iid:
            ids.add(iid)
    return ids


def _ids_do_container(page, url, max_paginas):
    """Navega a lista do container (paginando com _Desde_) e junta os item ids."""
    ids = set()
    for n in range(max_paginas):
        page_url = url if n == 0 else f"{url}_Desde_{n * 50 + 1}"
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Container %s pagina %s falhou: %s", url, n + 1, e)
            break
        achados = _coletar_ids_da_pagina(page)
        if not achados:
            break
        ids |= achados
        pausa_humana()
    return ids


def _ids_por_http(sessao, url, max_paginas):
    """Item ids do container por GET, sem browser. None quando não deu.

    Mesmo padrão de `coupon_products._coletar_ml_remoto`: a listagem do ML é SSR,
    então um GET traz os mesmos links. Devolver None (e não conjunto vazio) permite
    ao chamador distinguir "não consegui" de "container legitimamente vazio" e só
    então pagar o Chromium.
    """
    if sessao is None:
        return None
    ids = set()
    for n in range(max_paginas):
        page_url = url if n == 0 else f"{url}_Desde_{n * 50 + 1}"
        try:
            resp = sessao.get(page_url, timeout=25)
        except Exception as e:
            logger.debug("Container %s por HTTP falhou: %s", url, e)
            return None if not ids else ids
        if resp.status_code != 200:
            return None if not ids else ids
        achados = _ids_do_html(resp.text)
        if not achados:
            break
        ids |= achados
    return ids or None


def _coletar(cupons, coletor, max_paginas, orcamento_s=ORCAMENTO_S):
    """Só navegação: [(cupom, ids)]. Roda DENTRO do Playwright.

    Separado de `_confirmar_todos` porque o ORM não pode ser chamado de dentro de
    `with sync_playwright()`: a API sync mantém um event loop vivo num greenlet da
    mesma thread e o @async_unsafe do Django levanta SynchronousOnlyOperation.

    `orcamento_s` corta a varredura no tempo. Este passo roda dentro do ciclo de
    scrape completo, num `try/except` que só loga: sem teto, uma coleta longa não
    "falha" — ela simplesmente ocupa o ciclo inteiro.
    """
    pares = []
    fim = time.monotonic() + orcamento_s
    for i, cupom in enumerate(cupons):
        if time.monotonic() > fim:
            logger.info("Orçamento de %ss esgotado no casamento de container; "
                        "%s de %s cupons processados", orcamento_s, i, len(cupons))
            break
        url = (cupom.regras or {}).get("container_url")
        try:
            pares.append((cupom, coletor(url, max_paginas)))
        except Exception as e:
            logger.warning("Coleta do container do cupom %s falhou: %s", cupom.codigo, e)
    return pares


def _confirmar_todos(pares, idx, agora):
    """Só ORM: grava os ProdutoCupom. Roda FORA do Playwright."""
    total = sum(_confirmar(cupom, ids, idx, agora) for cupom, ids in pares)
    logger.info("Casamento cupom-container: %s vinculo(s) confirmado(s)", total)
    return total


def _rodar(cupons, idx, agora, coletor, max_paginas, orcamento_s=ORCAMENTO_S):
    """Coleta + confirmação em sequência (sem browser em voo)."""
    return _confirmar_todos(
        _coletar(cupons, coletor, max_paginas, orcamento_s), idx, agora)


def casar_cupons_container(coletor=None, max_paginas=2, usuario=None,
                           limite_cupons=LIMITE_CUPONS, orcamento_s=ORCAMENTO_S):
    """Confirma quais produtos rastreados participam de cada cupom de container.

    `coletor(url, max_paginas) -> set[item_id]` pode ser injetado (testes). Sem ele,
    tenta HTTP primeiro e só abre o Chromium para os containers que o HTTP não
    resolveu — o mesmo padrão de `coupon_products._coletar_ml_remoto`. Na maioria
    dos casos o navegador nem sobe.
    """
    cupons = _cupons_de_container(limite_cupons)
    if not cupons:
        return 0
    idx = _indexar_produtos()
    if not idx:
        return 0
    agora = timezone.now()

    if coletor is not None:
        return _rodar(cupons, idx, agora, coletor, max_paginas, orcamento_s)

    state = storage_state(usuario)
    if state is None:
        avisar_sem_sessao("Casamento cupom-container", usuario)

    sessao = _ml_http_session(state)
    pendentes = []          # (cupom, url) que o HTTP não resolveu
    pares = []
    fim = time.monotonic() + orcamento_s
    for cupom in cupons:
        if time.monotonic() > fim:
            break
        url = (cupom.regras or {}).get("container_url")
        ids = _ids_por_http(sessao, url, max_paginas)
        if ids is None:
            pendentes.append((cupom, url))
        else:
            pares.append((cupom, ids))

    if pendentes:
        restante = max(fim - time.monotonic(), 0)
        logger.info("Casamento de container: %s por HTTP, %s vão pro navegador",
                    len(pares), len(pendentes))
        # A gravação fica FORA do `with`: dentro dele o ORM levanta
        # SynchronousOnlyOperation (event loop do Playwright vivo nesta thread).
        with iniciar_browser(storage_state=state, headless=True,
                             validar_sessao=False) as (page, _context):
            pares += _coletar(
                [c for c, _ in pendentes],
                lambda url, paginas: _ids_do_container(page, url, paginas),
                max_paginas, restante)
    return _confirmar_todos(pares, idx, agora)
