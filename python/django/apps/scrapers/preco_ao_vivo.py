"""Revalidação de preço imediatamente antes de publicar.

O catálogo é montado por raspagens periódicas — `task_scrape` roda uma vez por dia
e `expire_stale` só marca um item como velho depois de 48h. Sem esta checagem a
mensagem anuncia um preço de dois dias atrás, ora acima ora abaixo do real.

Política (decidida com o time):
  - variação dentro da tolerância  -> segue, nada é gravado;
  - preço caiu                     -> atualiza e segue (anunciar mais caro do que
                                      a página cobra nunca gera reclamação);
  - preço subiu                    -> atualiza e só aborta quando o desconto cai
                                      abaixo do mínimo configurado;
  - erro/timeout/challenge         -> INCONCLUSIVO, segue com o valor do banco.

REGRA CENTRAL — bloqueio de anti-bot é inconclusivo, NUNCA reprovação. É a mesma
regra de link_http: o ML devolve challenge a rajadas para o IP de datacenter da
Fly, e transformar isso em veredito pararia todos os envios de uma vez. O preço
das amostras bloqueadas é o do banco, e é a telemetria de `fonte=inconclusivo`
que diz se esta checagem está de fato pegando.

Fontes por marketplace:
  - amazon        -> Creators API (1 chamada HTTP); PDP pública atrás de flag.
  - mercadolivre  -> GET com os cookies do storage_state. O GET ANÔNIMO NÃO SERVE:
                     o ML bloqueia por TLS/JA3 e devolve /gz/account-verification
                     a qualquer cliente sem fingerprint de browser (medição no topo
                     de scraper_mercadolivre/link_http.py). A API pública também
                     não serve — exige token de aplicação, e a credencial daqui é
                     sessão web. Sobra o transporte autenticado, que é o mesmo que
                     coupon_products já usa como caminho primário contra este host.

Escrita: sob RLS o `save()` numa linha do pool compartilhado do ML é negado (ver
apps/accounts/rls.py). Por isso o que a mensagem usa é o objeto MUTADO EM MEMÓRIA;
a persistência é um bônus best-effort.
"""
import logging
import time

from django.conf import settings
from django.utils import timezone

from apps.scrapers import precos

# Quanto a medição do deal espera pelo Chromium antes de desistir. Precisa ser
# maior que o intervalo entre as páginas de um lote longo (é aí que ele checa a
# fila e cede), e menor que o tique de envio.
PRECO_ESPERA_BROWSER_S = 60
# Vida da varredura de ofertas. Curta de proposito: cobre os candidatos de UM
# tique de envio, nao serve como preco guardado para o tique seguinte.
PRECO_VARREDURA_TTL_S = 120

logger = logging.getLogger(__name__)

# Abaixo disso a diferença é arredondamento de centavo, não mudança de preço.
TOLERANCIA = 0.005


def _resultado(ok, preco, *, mudou=False, fonte="", motivo=""):
    return {"ok": ok, "preco": preco, "mudou": mudou, "fonte": fonte, "motivo": motivo}


def _preco_da_creators_api(produto):
    """Preço oficial via Creators API — uma chamada HTTP, sem Playwright."""
    from apps.scrapers.scraper_amazon import creators_api
    from apps.scrapers.scraper_amazon.ofertas_scraper import _mapear_item

    asin = getattr(produto, "asin", "")
    if not asin:
        return None
    creds = creators_api.creds_de_usuario(getattr(produto, "owner", None))
    itens = creators_api.get_items([asin], creds=creds)
    if not itens:
        return None
    mapeado = _mapear_item(itens[0])
    if not mapeado or mapeado.get("preco_com_cupom", 0) <= 0:
        return None
    return {
        "preco": mapeado["preco_com_cupom"],
        "preco_de": mapeado["preco_sem_desconto"],
        "fonte": "creators-api",
    }


def _preco_da_pdp(produto):
    """Raspagem da PDP. Cara (Playwright) — só atrás da flag."""
    from apps.scrapers.sources.amazon_public import verify_product_url

    resultado = verify_product_url(produto.link_produto)
    preco = (resultado or {}).get("preco") or 0
    if preco <= 0:
        return None
    return {"preco": preco, "preco_de": 0, "fonte": "pdp-publica"}


def _preco_amazon(produto, url="", usuario=None):
    vivo = _preco_da_creators_api(produto)
    if vivo is None and getattr(settings, "PRECO_REVALIDA_PLAYWRIGHT", False):
        vivo = _preco_da_pdp(produto)
    return vivo


def sessao_ml(usuario=None):
    """Sessão HTTP com os cookies do ML, ou None quando não há credencial.

    Cai para a credencial do sistema quando o remetente não tem ML conectado: o
    catálogo do ML é um pool compartilhado (owner=None), então é a mesma página
    que o worker já lê com essa mesma credencial. Ler não escreve nada e não
    toca no estado da sessão.
    """
    from apps.scrapers import ml_auth

    state = ml_auth.storage_state(usuario)
    if state is None and usuario is not None:
        state = ml_auth.storage_state_do_sistema()
    if state is None:
        return None
    return ml_auth.http_session(state)


def _preco_ml(produto, url="", usuario=None):
    """Preço vivo do ML pela página que o assinante vai abrir.

    Confere a `url` PUBLICADA (o link de afiliado) e não `link_produto`: um GET
    segue meli.la -> PDP e mede o preço da página de destino real. É também o que
    fecha o buraco de o envio não tocar a página nenhuma quando o link já estava
    aprovado em cache.

    Nunca invalida a sessão do ML a partir daqui: challenge da Fly não é logout, e
    a fonte única desse estado é `sondar_sessao_ml`.
    """
    from apps.scrapers.scraper_mercadolivre import link_http

    sessao = sessao_ml(usuario)
    if sessao is None:
        # Sem credencial o GET anônimo cairia no challenge de qualquer forma.
        # Não chamamos avisar_sem_sessao: gera evento e isto roda POR ITEM.
        return None
    alvo = url or getattr(produto, "link_produto", "")
    relatorio = link_http.relatorio_de_preco(alvo, sessao=sessao)
    if relatorio.get("bloqueio") or relatorio.get("preco", 0) <= 0:
        return None
    return {
        "preco": relatorio["preco"],
        "preco_de": relatorio.get("preco_de") or 0,
        "fonte": "ml-http-sessao",
    }


def varrer_ofertas_ml(paginas=None):
    """{item_id: (preco, preco_de)} lido AGORA da vitrine `/ofertas`.

    Uma varredura por tique, nao uma por candidato. Cacar o item pagina a pagina
    falhava a maior parte das vezes — `/ofertas` e vitrine rotativa e o item podia
    simplesmente nao estar nas primeiras paginas naquele instante — e ainda pagava
    o custo de navegacao a cada candidato.

    O cache e curtissimo e proposital: dentro do mesmo tique o envio tenta varios
    candidatos, e todos devem ser conferidos contra a MESMA leitura. Dois minutos
    nao e "preco salvo": e a medicao deste envio, reaproveitada entre os candidatos
    dele.
    """
    from django.core.cache import caches

    from apps.scrapers.auxiliar import iniciar_browser
    from apps.scrapers.carga import BrowserResourceUnavailable, coordinated_ml_browser
    from apps.scrapers.ml_auth import storage_state
    from apps.scrapers.scraper_mercadolivre.link import _extrair_item_id
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _coletar_cards

    paginas = max(1, int(
        paginas or getattr(settings, "PRECO_JIT_PAGINAS_OFERTAS", 4)))
    cache = caches["default"]
    chave = f"ml-ofertas-jit:{paginas}"
    guardado = cache.get(chave)
    if guardado is not None:
        return guardado

    mapa = {}
    try:
        with coordinated_ml_browser(
            usuario=None, authenticated=True, owner_kind="preco_ao_vivo",
            wait_seconds=PRECO_ESPERA_BROWSER_S,
        ):
            with iniciar_browser(
                storage_state=storage_state(None), headless=True,
            ) as (page, _contexto):
                for numero in range(1, paginas + 1):
                    page.goto(
                        f"https://www.mercadolivre.com.br/ofertas?page={numero}",
                        wait_until="domcontentloaded", timeout=45000,
                    )
                    if "captcha" in page.url or "account-verification" in page.url:
                        break
                    for card in (_coletar_cards(page) or []):
                        item = _extrair_item_id(str(card.get("link_produto") or ""))
                        preco = float(card.get("preco_com_cupom") or 0)
                        if item and preco > 0:
                            mapa[item] = (
                                preco, float(card.get("preco_sem_desconto") or 0))
    except BrowserResourceUnavailable:
        return {}
    except Exception as exc:
        logger.info("varredura de ofertas falhou: %s", str(exc)[:120])
        return {}
    if mapa:
        cache.set(chave, mapa, PRECO_VARREDURA_TTL_S)
    logger.info("varredura de ofertas ML: %s itens em %s pagina(s)",
                len(mapa), paginas)
    return mapa


def _preco_ml_navegador(produto, url="", usuario=None):
    """Preco medido AGORA, conferido contra a varredura da vitrine.

    Medido em 03/09/2026 deste IP: PDP, APIs publicas e a busca em
    `lista.mercadolivre.com.br` respondem muro de CAPTCHA mesmo com navegador
    logado. `/ofertas` responde — e a porta por onde a raspagem continua lendo, e
    a unica que sobrou. Nenhum CAPTCHA e resolvido ou contornado.

    Sem o item na varredura, devolve None: o envio fica transitorio e tenta o
    proximo candidato, que sera conferido contra a MESMA leitura. Nunca cai no
    preco do banco — aquele e a verificacao da ingestao, nao a do envio.
    """
    from apps.scrapers.scraper_mercadolivre.link import _extrair_item_id

    alvo = _extrair_item_id(str(getattr(produto, "link_produto", "") or ""))
    if not alvo:
        return None
    achado = varrer_ofertas_ml().get(alvo)
    if not achado:
        return None
    preco, preco_de = achado
    return {"preco": preco, "preco_de": preco_de, "fonte": "ml-ofertas-jit"}


# Uma fonte por marketplace. Sem entrada aqui = não revalidável (segue com o banco).
_FONTES = {"amazon": _preco_amazon, "mercadolivre": _preco_ml}
# Segunda tentativa, cara, só para quem EXIGE medição (a camada Deal).
_FONTES_EXIGENTES = {"mercadolivre": _preco_ml_navegador}


def _desconto(preco_de, preco):
    if not preco_de or preco_de <= preco:
        return 0.0
    return (preco_de - preco) / preco_de * 100


def revalidar(produto, usuario=None, configuracao=None, *, url="",
              exigir_medicao=False) -> dict:
    """Confere o preço ao vivo e atualiza o produto. Ver política no topo.

    `url` é o link EFETIVAMENTE publicado; sem ela cai no `link_produto`.
    """
    mkt = getattr(produto, "marketplace", "") or ""
    fonte = _FONTES.get(mkt)
    if fonte is None:
        return _resultado(True, 0, fonte="nao_suportado")
    if mkt == "mercadolivre" and not getattr(settings, "PRECO_REVALIDA_ML", True):
        return _resultado(True, 0, fonte="desligado")

    atual = getattr(produto, "preco_com_cupom", 0) or 0
    if atual <= 0:
        return _resultado(True, atual, fonte="sem_preco_base")

    # Fonte de cupom-de-ativação (página oficial de cupons da Amazon): o que a
    # mensagem anuncia é `preco_efetivo`, e nenhuma fonte ao vivo daqui devolve o
    # pós-cupom — a Creators API devolve a VITRINE. Revalidar acabaria achatando o
    # efetivo contra a vitrine e a mensagem passaria a prometer um valor maior que
    # o combinado, logo abaixo da linha "ative o cupom". Melhor um pós-cupom de
    # algumas horas atrás do que o número errado.
    efetivo = getattr(produto, "preco_efetivo", 0) or 0
    if 0 < efetivo < atual:
        return _resultado(True, efetivo, fonte="cupom_ativacao_nao_revalidavel")

    inicio = time.monotonic()
    vivo = None
    try:
        vivo = fonte(produto, url=url, usuario=usuario)
    except Exception as exc:
        logger.warning(
            "preco_ao_vivo inconclusivo %s id=%s: %s", mkt, getattr(produto, "pk", ""), exc,
        )
        return _resultado(True, atual, fonte="inconclusivo", motivo=str(exc)[:120])

    decorrido_ms = (time.monotonic() - inicio) * 1000
    if vivo is None and exigir_medicao:
        # Quem exige medição paga o preço dela. Sem esta passada, o bloqueio de IP
        # do ML transforma "não consegui medir" em "publica com o preço velho".
        cara = _FONTES_EXIGENTES.get(mkt)
        if cara is not None:
            try:
                vivo = cara(produto, url=url, usuario=usuario)
            except Exception as exc:
                logger.info("preco_ao_vivo navegador falhou id=%s: %s",
                            getattr(produto, "pk", ""), str(exc)[:120])
    if vivo is None:
        logger.info(
            "preco_ao_vivo %s id=%s fonte=inconclusivo ms=%.0f",
            mkt, getattr(produto, "pk", ""), decorrido_ms,
        )
        return _resultado(True, atual, fonte="inconclusivo", motivo="sem dado ao vivo")

    novo = vivo["preco"]
    variacao = abs(novo - atual) / atual
    logger.info(
        "preco_ao_vivo %s id=%s asin=%s fonte=%s banco=%.2f vivo=%.2f variacao=%.4f ms=%.0f",
        mkt, getattr(produto, "pk", ""), getattr(produto, "asin", ""),
        vivo["fonte"], atual, novo, variacao, decorrido_ms,
    )
    if variacao <= TOLERANCIA:
        return _resultado(True, atual, fonte=vivo["fonte"])

    preco_de = vivo.get("preco_de") or getattr(produto, "preco_sem_desconto", 0) or 0
    _aplicar(produto, novo, preco_de)

    if novo < atual:
        return _resultado(True, novo, mudou=True, fonte=vivo["fonte"],
                          motivo="preço caiu")

    minimo = _minimo_desconto(usuario, configuracao)
    desconto = _desconto(preco_de, novo)
    if minimo and desconto < minimo:
        return _resultado(
            False, novo, mudou=True, fonte=vivo["fonte"],
            motivo=f"desconto caiu para {desconto:.0f}% (mínimo {minimo:.0f}%)",
        )
    return _resultado(True, novo, mudou=True, fonte=vivo["fonte"], motivo="preço subiu")


def revalidar_colagem(cupom, itens, *, usuario=None, orcamento_s=None):
    """Confere os preços da colagem de cupom. Devolve (mantidos, removidos).

    A colagem anuncia "De X por Y" por item, com Y calculado sobre a vitrine que
    estava no banco no momento do preparo (até 3h atrás). Aqui a vitrine é medida
    ao vivo e a conta do cupom é REFEITA por `coupon_products.calcular_precos` —
    fonte única das regras (valor mínimo, teto, tipo de desconto, guarda de 90%).

    MUTAÇÃO SÓ EM MEMÓRIA. Este código roda no contexto RLS do usuário, e
    Produto/ProdutoCupom do pool compartilhado não são graváveis ali; além disso
    `relacoes_preparadas_para_envio` é deliberadamente um predicado de leitura —
    quem materializa preparo é o worker.

    Política por item: recalcular é o padrão; remover só quando não existe linha
    correta a escrever (o item saiu das regras do cupom, ou o anúncio morreu).
    Bloqueio/timeout mantém a linha preparada — inconclusivo nunca reprova.
    """
    from concurrent.futures import ThreadPoolExecutor, wait
    from apps.scrapers import coupon_products
    from apps.scrapers.scraper_mercadolivre import link_http

    if not itens:
        return [], []
    if str(getattr(cupom, "marketplace", "") or "").lower() != "mercadolivre":
        return list(itens), []
    if not getattr(settings, "PRECO_REVALIDA_ML", True):
        return list(itens), []

    # Resolvido UMA vez: storage_state lê arquivo/banco, e fazer isso por item
    # dentro das threads misturaria ORM com o contexto RLS de outra conexão.
    sessao = sessao_ml(usuario)
    if sessao is None:
        return list(itens), []
    orcamento = orcamento_s or getattr(settings, "PRECO_REVALIDA_ORCAMENTO_S", 6.0)

    def medir(item):
        # SÓ HTTP aqui dentro. O contexto de tenant é por conexão e não atravessa
        # thread; qualquer ORM nesta função leria o banco sem RLS aplicado.
        return link_http.relatorio_de_preco(item["link"], sessao=sessao)

    inicio = time.monotonic()
    medidos = {}
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futuros = {pool.submit(medir, item): id(item) for item in itens}
        concluidos, _pendentes = wait(futuros, timeout=orcamento)
        for futuro in concluidos:
            try:
                medidos[futuros[futuro]] = futuro.result()
            except Exception as exc:
                logger.info("revalidar_colagem: item inconclusivo: %s", exc)
    finally:
        # wait=False é o que faz o orçamento valer: o `with` do executor chama
        # shutdown(wait=True) e ficaria pendurado nas threads que já começaram,
        # justamente as lentas que o orçamento existe para cortar. Quem estourou
        # segue em background e morre no timeout do próprio GET.
        pool.shutdown(wait=False, cancel_futures=True)

    mantidos, removidos = [], []
    for item in itens:
        relatorio = medidos.get(id(item))
        if relatorio is None or relatorio.get("bloqueio"):
            mantidos.append(item)  # inconclusivo: segue com o preparo
            continue
        if relatorio.get("morto"):
            removidos.append(item)
            continue
        produto = item["produto"]
        vitrine_banco = getattr(produto, "preco_com_cupom", 0) or 0
        produto.preco_com_cupom = relatorio["preco"]
        produto.preco_efetivo = relatorio["preco"]
        de_vivo = relatorio.get("preco_de") or 0
        if de_vivo > relatorio["preco"]:
            produto.preco_sem_desconto = de_vivo
        precos_novos = coupon_products.calcular_precos(cupom, produto)
        if not precos_novos:
            # Saiu das regras do cupom (caiu abaixo do valor mínimo, desconto
            # sumiu). Não há linha "De X por Y" verdadeira para escrever.
            produto.preco_com_cupom = vitrine_banco
            removidos.append(item)
            continue
        relacao = item.get("relacao")
        if relacao is not None:
            relacao.preco_original, relacao.preco_atual, relacao.preco_final = precos_novos
        mantidos.append(item)

    logger.info(
        "revalidar_colagem cupom=%s itens=%s medidos=%s removidos=%s ms=%.0f",
        getattr(cupom, "pk", ""), len(itens), len(medidos), len(removidos),
        (time.monotonic() - inicio) * 1000,
    )
    return mantidos, removidos


def _aplicar(produto, novo, preco_de):
    """Grava o preço fresco e invalida o texto da IA quando ele cita o antigo.

    A MUTAÇÃO EM MEMÓRIA é o que importa: é este objeto que `montar_mensagem` lê
    logo em seguida. O `save()` é best-effort porque, sob o RLS do usuário, uma
    linha do pool compartilhado do ML não é gravável (rls.MIXED_TENANT_TABLES).
    Nunca reordene isto para depender do save.
    """
    campos = ["preco_com_cupom", "preco_efetivo", "ultima_verificacao"]
    produto.preco_com_cupom = novo
    produto.preco_efetivo = novo
    produto.ultima_verificacao = timezone.now()
    if preco_de and preco_de > novo:
        produto.preco_sem_desconto = preco_de
        campos.append("preco_sem_desconto")
    # frase_llm/nome_llm são cache do texto gerado, e o título cita o preço.
    # Mantê-los faria a IA anunciar um valor que a linha "POR" já não mostra.
    if getattr(produto, "frase_llm", ""):
        produto.frase_llm = ""
        campos.append("frase_llm")
    if getattr(produto, "nome_llm", ""):
        produto.nome_llm = ""
        campos.append("nome_llm")
    try:
        produto.save(update_fields=campos)
    except Exception:
        # Esperado sob RLS no pool compartilhado; a mensagem já usa o valor novo.
        logger.info("preco_ao_vivo não gravou id=%s (segue em memória)",
                    getattr(produto, "pk", ""), exc_info=True)
    # O marketplace era "amazon" fixo aqui. Com o ML ligado isso gravaria o
    # histórico dele sob a chave `amazon:url:...`, que `precos.chave_produto` nunca
    # consulta — e o selo "mínima de 30 dias" pararia de casar.
    precos.registrar(getattr(produto, "marketplace", "") or "mercadolivre",
                     getattr(produto, "asin", ""),
                     getattr(produto, "link_produto", ""), novo)


def _minimo_desconto(usuario, configuracao):
    valor = getattr(configuracao, "min_desconto_percent", None)
    try:
        return float(valor) if valor else 0.0
    except (TypeError, ValueError):
        return 0.0
