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
from collections import deque
from urllib.parse import urlparse

from django.db.models import Max, Q
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser, pausa_humana
from apps.scrapers.carga import (
    BrowserResourceUnavailable, ml_site_browser_resource,
)
from apps.scrapers.ml_auth import avisar_sem_sessao, storage_state
from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, Produto, ProdutoCupom,
)
from apps.scrapers.resource_control import interesse_interativo_pendente
from .link import _extrair_item_id
from .scraper import _ml_http_session

logger = logging.getLogger(__name__)


class SessaoMLObrigatoriaError(RuntimeError):
    """A parte autenticada do casamento não pode produzir um falso sucesso."""

# Teto de cupons por passada. Sem ele a varredura pegava TODOS os cupons ativos com
# container — em homologação, 2357 — a 2 páginas de navegação cada, e levava horas.
LIMITE_CUPONS = 60
# Teto de tempo. Este passo roda dentro do ciclo de scrape completo, num
# `try/except` que só loga: sem orçamento, uma coleta longa não falha — ela ocupa o
# ciclo inteiro e nada mais roda.
ORCAMENTO_S = 300

_RE_HREF = re.compile(r'href="([^"]*MLB[^"]*)"', re.I)


def _container_url_valida(url):
    """Aceita somente listagens absolutas do Mercado Livre.

    Registros legados chegaram a guardar ``-`` e até item ids ``MLB...`` como
    ``container_url``. O cliente HTTP os rejeita, mas antes eles ainda caíam no
    fallback de Chromium e consumiam o único slot global tentando navegar para
    uma URL inválida.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br"))
    )


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
                 if _container_url_valida((c.regras or {}).get("container_url"))
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
    external_id = str(cupom.external_id or "")
    campanha = external_id.split(":", 1)[1] if external_id.startswith("campanha:") else ""
    for iid in ids_container & set(idx):
        for prod in idx[iid]:
            # A campanha mora no vínculo. O Produto preserva a proveniência da sua
            # lane e pode participar de várias campanhas ao mesmo tempo.
            ProdutoCupom.objects.update_or_create(
                produto=prod, cupom=cupom,
                defaults={
                    "status": "confirmado", "verificado_em": agora,
                    "activation_key": campanha[:160],
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

    Levanta `SessaoMLObrigatoriaError` quando o ML devolve parede de login: aí o
    Chromium bate na MESMA porta, e insistir custa um slot de browser por cupom.
    """
    from apps.scrapers.ml_auth import parede_de_login

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
        if parede_de_login(resp):
            raise SessaoMLObrigatoriaError(
                "O Mercado Livre exigiu sessão para abrir a listagem do container."
            )
        if resp.status_code != 200:
            return None if not ids else ids
        achados = _ids_do_html(resp.text)
        if not achados:
            break
        ids |= achados
    return ids or None


def _coletar(cupons, coletor, max_paginas, orcamento_s=ORCAMENTO_S):
    """Só navegação: [(cupom, ids, motivo)]. Roda DENTRO do Playwright.

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
        if time.monotonic() >= fim:
            logger.info("Orçamento de %ss esgotado no casamento de container; "
                        "%s de %s cupons processados", orcamento_s, i, len(cupons))
            pares.extend(
                (restante, None, "container_budget_exhausted")
                for restante in cupons[i:]
            )
            break
        # Mesmo motivo de `mapear_ofertas`/`gerar_links_em_lote`: o orçamento de
        # tempo (até 5 min) não bastava — em produção, um job manual de cupons
        # segurou o Chromium da máquina por minutos seguidos e todo login
        # interativo falhou nesse intervalo. Cede DEPOIS do item corrente; os
        # restantes voltam com o mesmo motivo do orçamento esgotado.
        if i > 0 and interesse_interativo_pendente("django_chromium"):
            logger.info(
                "Casamento de container cedeu o navegador a um login "
                "interativo após %s de %s cupom(ns).", i, len(cupons),
            )
            pares.extend(
                (restante, None, "container_budget_exhausted")
                for restante in cupons[i:]
            )
            break
        url = (cupom.regras or {}).get("container_url")
        try:
            ids = coletor(url, max_paginas)
            pares.append((
                cupom, ids,
                "" if ids else (
                    "container_fetch_failed" if ids is None
                    else "container_empty_unproven"
                ),
            ))
        except Exception as e:
            logger.warning("Coleta do container do cupom %s falhou: %s", cupom.codigo, e)
            pares.append((cupom, None, "container_fetch_failed"))
    return pares


def _confirmar_todos(pares, idx, agora):
    """Só ORM: grava os ProdutoCupom. Roda FORA do Playwright."""
    total = sum(
        _confirmar(cupom, ids, idx, agora)
        for cupom, ids, _motivo in pares if ids
    )
    logger.info("Casamento cupom-container: %s vinculo(s) confirmado(s)", total)
    return total


def _registrar_saude_containers(resultados, agora):
    """Persiste a saúde da terceira fonte ML sem guardar URL ou HTML.

    ``resultados`` contém ``(cupom, ids, publico, motivo)``. Um conjunto vazio não
    comprova um container legitimamente vazio; por isso degrada a fonte. ``None``
    representa falha operacional ou recurso ausente e também preserva os vínculos
    anteriormente confirmados.
    """
    from apps.scrapers.sources.persistence import record_coupon_observation

    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="ml-public-containers",
        defaults={
            "marketplace": "mercadolivre",
            "nome": "Mercado Livre — containers públicos",
        },
    )
    aceitos = 0
    falhas = 0
    sem_sessao = any(
        motivo in ("ml_session_missing", "ml_session_expired")
        for _cupom, _ids, _publico, motivo in resultados
    )
    for cupom, ids, publico, motivo in resultados:
        comprovado = bool(ids)
        if comprovado:
            aceitos += 1
        else:
            falhas += 1
        record_coupon_observation(
            cupom,
            source=fonte,
            health_status="healthy" if comprovado else "degraded",
            outcome=(
                "accepted" if comprovado
                else "waiting" if motivo == "ml_session_missing"
                else "operational_failure" if ids is None
                else "invalid"
            ),
            reason_code=(
                "container_items_proven" if comprovado
                else motivo or "container_empty_unproven"
            ),
            evidence={
                "association": (
                    "public_container" if publico else "authenticated_container"
                ),
                "product_ids": len(ids or ()),
                "public": publico,
            },
        )
    fonte.ultima_tentativa = agora
    fonte.ultimo_total = aceitos
    if resultados and not falhas:
        fonte.status = "ok"
        fonte.ultimo_sucesso = agora
        fonte.falhas_consecutivas = 0
        fonte.erro_publico = ""
    else:
        fonte.status = "degraded"
        fonte.falhas_consecutivas += 1
        fonte.erro_publico = (
            # A causa muda a ação: sessão caída é "reconecte"; o resto é "aguarde".
            "A conexão do Mercado Livre expirou; os containers não puderam ser lidos."
            if sem_sessao else
            "Alguns containers não puderam ser comprovados; vínculos anteriores preservados."
        )
    fonte.save(update_fields=[
        "ultima_tentativa", "ultimo_total", "status", "ultimo_sucesso",
        "falhas_consecutivas", "erro_publico",
    ])


def _rodar(cupons, idx, agora, coletor, max_paginas, orcamento_s=ORCAMENTO_S):
    """Coleta + confirmação em sequência (sem browser em voo)."""
    pares = _coletar(cupons, coletor, max_paginas, orcamento_s)
    _registrar_saude_containers([
        (cupom, ids, True, motivo)
        for cupom, ids, motivo in pares
    ], agora)
    return _confirmar_todos(pares, idx, agora)


def _credenciais_de_reserva(usuario):
    """Contas cujas sessões podem substituir a do contexto quando ela é barrada.

    Só no contexto COMPARTILHADO (`usuario=None`): num casamento de dono
    conhecido, usar a sessão de outra organização não seria recuperação, seria
    cruzar fronteira de tenant. Devolve usuários, não credenciais — decifrar
    sessão de todo mundo antes de precisar é caro e quase sempre desnecessário.
    """
    if usuario is not None:
        return []
    from django.contrib.auth import get_user_model

    return list(get_user_model().objects.filter(is_active=True))


def _ler_com_reserva(sessao, state, url, max_paginas, reserva):
    """(ids, sessao, state), trocando de credencial enquanto o ML devolver parede."""
    while True:
        try:
            return _ids_por_http(sessao, url, max_paginas), sessao, state
        except SessaoMLObrigatoriaError:
            proximo = None
            while reserva and proximo is None:
                proximo = storage_state(reserva.popleft())
            if proximo is None:
                raise
            logger.info(
                "Casamento de container: a sessão em uso foi barrada; "
                "alternando para outra conta conectada.",
            )
            state, sessao = proximo, _ml_http_session(proximo)


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
    # Reserva de credenciais para quando a do contexto for barrada. A listagem é a
    # mesma página pública para qualquer conta; a sessão só decide se o gateway do
    # ML entrega o HTML. Sem isto, a sessão de sistema expirada parava o casamento
    # de TODAS as organizações — inclusive as que estavam com o ML conectado.
    reserva = deque(_credenciais_de_reserva(usuario))

    sessao = _ml_http_session(state)
    pendentes = []          # (cupom, url) que o HTTP não resolveu
    pares = []
    fim = time.monotonic() + orcamento_s
    for indice, cupom in enumerate(cupons):
        if time.monotonic() >= fim:
            break
        url = (cupom.regras or {}).get("container_url")
        try:
            ids, sessao, state = _ler_com_reserva(
                sessao, state, url, max_paginas, reserva,
            )
        except SessaoMLObrigatoriaError:
            # Nenhuma credencial passou. A parede vale para a sessão inteira, não
            # para este container: fechar a passada aqui preserva os vínculos já
            # confirmados e evita abrir um Chromium por cupom restante para colher
            # a mesma tela de login.
            logger.warning(
                "Casamento de container interrompido: o Mercado Livre exigiu "
                "sessão (%s de %s cupons lidos).", indice, len(cupons),
            )
            _registrar_saude_containers(
                [(item, achados, True, motivo) for item, achados, motivo in pares]
                + [(restante, None, False, "ml_session_expired")
                   for restante in cupons[indice:]],
                agora,
            )
            if pares:
                _confirmar_todos(pares, idx, agora)
            raise
        if ids is None:
            pendentes.append((cupom, url))
        else:
            pares.append((cupom, ids, ""))

    if pendentes:
        if state is None:
            # O HTTP público foi tentado, mas os containers restantes exigem uma
            # sessão. Abrir Chromium anônimo só repetiria o vazio e faria o pipeline
            # relatar "0 falhas". Persiste os pares públicos já confirmados e degrada
            # explicitamente a etapa autenticada.
            _registrar_saude_containers(
                [(cupom, ids, True, motivo) for cupom, ids, motivo in pares]
                + [(cupom, None, False, "ml_session_missing")
                   for cupom, _url in pendentes],
                agora,
            )
            if pares:
                _confirmar_todos(pares, idx, agora)
            raise SessaoMLObrigatoriaError(
                "A sessão sistêmica do Mercado Livre é necessária para consultar "
                f"{len(pendentes)} container(s) de cupom."
            )
        restante = max(fim - time.monotonic(), 0)
        logger.info("Casamento de container: %s por HTTP, %s vão pro navegador",
                    len(pares), len(pendentes))
        # A gravação fica FORA do `with`: dentro dele o ORM levanta
        # SynchronousOnlyOperation (event loop do Playwright vivo nesta thread).
        with ml_site_browser_resource(
            usuario, owner_kind="coupon_container",
        ) as acquired:
            if not acquired:
                raise BrowserResourceUnavailable(
                    "Capacidade de browser ocupada; casamento de containers será retomado."
                )
            with iniciar_browser(storage_state=state, headless=True) as (page, _context):
                pares_browser = _coletar(
                    [c for c, _ in pendentes],
                    lambda url, paginas: _ids_do_container(page, url, paginas),
                    max_paginas, restante)
        _registrar_saude_containers(
            [(cupom, ids, True, motivo) for cupom, ids, motivo in pares]
            + [(cupom, ids, False, motivo)
               for cupom, ids, motivo in pares_browser],
            agora,
        )
        pares += pares_browser
    else:
        _registrar_saude_containers(
            [(cupom, ids, True, motivo) for cupom, ids, motivo in pares], agora,
        )
    return _confirmar_todos(pares, idx, agora)
