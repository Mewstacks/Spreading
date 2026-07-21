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

from django.db.models import Q
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser, pausa_humana
from apps.scrapers.models import CupomNormalizado, Produto, ProdutoCupom
from apps.scrapers.session_paths import ml_auth_path
from .link import _extrair_item_id

logger = logging.getLogger(__name__)


def _cupons_de_container():
    """CupomNormalizado ativos, com container e que NÃO são site-wide."""
    agora = timezone.now()
    qs = CupomNormalizado.objects.filter(
        marketplace="mercadolivre", estado="ativo",
    ).filter(Q(validade__isnull=True) | Q(validade__gte=agora))
    return [c for c in qs
            if (c.regras or {}).get("container_url")
            and not (c.regras or {}).get("is_mar_aberto")]


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


def _coletar_ids_da_pagina(page):
    """Todos os item ids MLB presentes nos links da página atual do container."""
    ids = set()
    try:
        anchors = page.locator("a[href*='MLB']")
        total = anchors.count()
    except Exception:
        return ids
    for i in range(total):
        try:
            href = anchors.nth(i).get_attribute("href", timeout=1000) or ""
        except Exception:
            continue
        iid = _extrair_item_id(href)
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


def _rodar(cupons, idx, agora, coletor, max_paginas):
    total = 0
    for cupom in cupons:
        url = (cupom.regras or {}).get("container_url")
        try:
            ids = coletor(url, max_paginas)
        except Exception as e:
            logger.warning("Coleta do container do cupom %s falhou: %s", cupom.codigo, e)
            continue
        total += _confirmar(cupom, ids, idx, agora)
    logger.info("Casamento cupom-container: %s vinculo(s) confirmado(s)", total)
    return total


def casar_cupons_container(coletor=None, max_paginas=2):
    """Confirma quais produtos rastreados participam de cada cupom de container.

    `coletor(url, max_paginas) -> set[item_id]` pode ser injetado (testes). Sem ele,
    abre UM navegador Playwright reaproveitado para todos os containers.
    """
    cupons = _cupons_de_container()
    if not cupons:
        return 0
    idx = _indexar_produtos()
    if not idx:
        return 0
    agora = timezone.now()

    if coletor is not None:
        return _rodar(cupons, idx, agora, coletor, max_paginas)

    with iniciar_browser(auth_path=ml_auth_path(), headless=True,
                         validar_sessao=False) as (page, _context):
        return _rodar(cupons, idx, agora,
                      lambda url, paginas: _ids_do_container(page, url, paginas),
                      max_paginas)
