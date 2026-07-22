import logging
import os
import requests
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from django.db.models import F, FloatField, ExpressionWrapper, Count, Q
from apps.scrapers.models import Produto, Cupom, HistoricoEnvio, Publicacao
from apps.scrapers.precos import stats as _stats_preco
from apps.scrapers.whatsapp_client import TRANSITORIO

logger = logging.getLogger(__name__)


def _motivo_publico_transporte(resultado) -> str:
    """Traduz falhas externas para mensagens estáveis; o detalhe fica no evento."""
    resultado = resultado or {}
    if resultado.get("resultado") == "incerto":
        return ("A entrega não pôde ser confirmada e, para evitar duplicidade, "
                "não será repetida automaticamente.")
    classe = resultado.get("classe")
    erro = str(resultado.get("erro") or "").lower()
    if classe == "transitorio":
        return "O canal está temporariamente indisponível. Tente novamente mais tarde."
    if classe == "permanente":
        if any(p in erro for p in ("destino", "grupo", "chat", "@g.us", "@canal")):
            return "O destino informado é inválido ou não está acessível pelo canal."
        if any(p in erro for p in ("token", "credencial", "conect", "sessão", "bot")):
            return "As credenciais do canal precisam ser reconectadas."
        return "O canal rejeitou o envio. Revise as credenciais e o destino."
    return "Não foi possível confirmar o envio pelo canal selecionado."

def esta_vivo(produto):
    """
    Estado da oferta no ML, em TRÊS valores (A1 — seleção não destrutiva):
      True  -> página 200 e sem texto de "pausado/esgotado".
      False -> CONFIRMADO morto (HTTP 404/410 ou texto de pausa/inexistente).
      None  -> DESCONHECIDO (timeout, erro de conexão, status estranho). NÃO apagar:
               uma instabilidade de rede não pode apagar um produto bom do banco.
    Só o chamador apaga, e somente quando recebe False.
    """
    from apps.scrapers.auxiliar import ua_aleatorio
    headers = {'User-Agent': ua_aleatorio()}
    termos_inativos = [
        "Anúncio pausado",
        "Este anúncio foi pausado",
        "Estoque indisponível",
        "Este item não está mais",
        "Página não encontrada",
    ]
    try:
        r = requests.get(produto.link_produto, headers=headers, timeout=5)
        if r.status_code in (404, 410):
            return False                      # confirmado: não existe mais
        if r.status_code != 200:
            return None                       # 5xx/redirect estranho -> incerto, mantém
        for termo in termos_inativos:
            if termo in r.text:
                return False                  # confirmado: pausado/esgotado
        return True
    except Exception:
        return None                           # timeout/conexão -> incerto, mantém

def _selecionar_item_legacy(macros_selecionadas=None, categorias_selecionadas=None,
                           limite_envio=1, horas_cooldown=24,
                           min_desconto_percent=15.0, termo=None,
                           marketplace=None, usuario=None, grupo_id=None):
    """
    Seleciona produtos da base usando a lógica de 'Roleta Viciada' (Weighted Random Choice).
    Leva em conta: O Desconto percentual, desconto absoluto, preço e novidade do cupom.
    Evita reenviar itens enviados há menos de `horas_cooldown`.
    `termo`: sub-nicho opcional — string com termos separados por vírgula; mantém só
    produtos cujo nome casa com ALGUM termo (ex: "aspirador robo, robot vacuum").
    """
    from django.db.models import Q
    from apps.scrapers.marketplaces.registry import get_marketplace

    # 1. Filtro inicial - tudo menos os cupons de RESGATE legados (origem='cupom'),
    # que não aplicam via link. Entram: 'oferta' (feed), 'busca' (termo), 'cupom_codigo'.
    qs = Produto.objects.exclude(origem="cupom")

    # Multi-tenant: pool COMPARTILHADO (owner=None, ex: ML) + itens PRIVADOS do usuário
    # (owner=usuario, ex: Amazon raspada com a conta dele). Sem usuario -> só o compartilhado.
    if usuario is not None:
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))
    else:
        qs = qs.filter(owner__isnull=True)

    if marketplace:
        qs = qs.filter(marketplace=marketplace)
    if macros_selecionadas:
        qs = qs.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)

    # Sub-nicho: filtra pelo nome (OR entre os termos)
    if termo:
        termos = [t.strip() for t in termo.split(",") if t.strip()]
        if termos:
            cond = Q()
            for t in termos:
                cond |= Q(nome__icontains=t)
            qs = qs.filter(cond)

    # 2. NUNCA repetir oferta: exclui produto já enviado alguma vez POR ESTE usuário.
    # (horas_cooldown ignorado de propósito — dedup é permanente, não janela.)
    # usuario=None (chamadas legadas) -> dedup global, como antes.
    hist = HistoricoEnvio.objects.all()
    if usuario is not None:
        hist = hist.filter(usuario=usuario)
    qs = qs.exclude(id__in=hist.values_list('produto_id', flat=True))

    # 3. Calcula economia e desconto (%) - Mantém apenas os válidos (> 10% e < 90%)
    # Desconto >= 90% indica dado corrompido (ex: cupom fixo maior que o preço do produto)
    produtos_elegiveis = qs.annotate(
        economia_rs=ExpressionWrapper(F('preco_sem_desconto') - F('preco_com_cupom'), output_field=FloatField()),
        desconto_percent=ExpressionWrapper(((F('preco_sem_desconto') - F('preco_com_cupom')) / F('preco_sem_desconto')) * 100, output_field=FloatField())
    ).filter(desconto_percent__gte=min_desconto_percent, desconto_percent__lt=90.0, preco_com_cupom__gt=0)

    # Buscamos cupons numa tacada só para checar 'data_criacao' depois
    campanhas_ids = list(produtos_elegiveis.values_list('campanha_id', flat=True))
    cupons_map = {c.campanha_id: c for c in Cupom.objects.filter(campanha_id__in=campanhas_ids)}

    opcoes_sorteio = []
    pesos_sorteio = []

    for prod in produtos_elegiveis:
        cupom = cupons_map.get(prod.campanha_id)

        # Descarta produto que não atinge o valor mínimo de compra do cupom
        if cupom and cupom.valor_minimo > 0 and prod.preco_sem_desconto < cupom.valor_minimo:
            continue

        # PONTUAÇÃO BASE: O peso foca bastante no Desconto Percentual
        score = prod.desconto_percent * 2.0 
        
        # BÔNUS ECONOMIA (R$): Ajuda produtos caros com bom desconto em R$
        score += (prod.economia_rs / 20.0)
        
        # BÔNUS TICKET BAIXO: Produtos baratos (<R$30) recebem mais chance
        if prod.preco_com_cupom < 30.0:
            score += 20.0
            
        # BÔNUS URGÊNCIA: Cupom novo (criado nas últimas 12h) recebe Boost de 50%
        if cupom and cupom.data_criacao >= timezone.now() - timedelta(hours=12):
            score *= 1.5

        # B1 — HISTÓRICO DE PREÇOS: o "de/por" do ML é frequentemente inflado.
        # Com histórico suficiente (>=3 pontos/30d), comparamos com o próprio preço
        # típico do item: preço "de sempre" -> desconto fictício, NÃO anuncia;
        # perto da mínima de 30 dias -> queda REAL, ganha boost forte.
        h = _stats_preco(prod, dias=30)
        if h and h["n"] >= 3:
            if prod.preco_com_cupom >= h["mediana"] * 0.98:
                continue  # não é oferta de verdade vs. o histórico do item
            if prod.preco_com_cupom <= h["minimo"] * 1.02:
                score *= 1.6  # perto da mínima histórica — oferta genuína

        opcoes_sorteio.append(prod)
        pesos_sorteio.append(score)

    if not opcoes_sorteio:
        return []

    # 4. Sorteio (A 'Roleta Viciada') com VALIDAÇÃO Just-in-Time!
    vencedores = []
    tentativas = 0
    max_tentativas = limite_envio * 10 # proteção contra loop infinito
    
    while len(vencedores) < limite_envio and opcoes_sorteio and tentativas < max_tentativas:
        tentativas += 1
        escolhido = random.choices(population=opcoes_sorteio, weights=pesos_sorteio, k=1)[0]

        # Checa o estado do anúncio (tri-state, A1) pela loja do produto:
        # ML faz GET na PDP; Amazon usa getItems. Agnóstico de marketplace.
        estado = get_marketplace(getattr(escolhido, "marketplace", "mercadolivre")).is_alive(escolhido)
        if estado is True:
            vencedores.append(escolhido)
        elif estado is False:
            # CONFIRMADO morto (404/pausado): aí sim pode limpar o banco.
            logger.info("Oferta morta confirmada; removendo produto id=%s", escolhido.id)
            escolhido.delete()
        else:
            # None = incerto (timeout/erro). NÃO apaga; só pula nesta rodada.
            logger.info("Estado incerto para produto id=%s; mantendo no banco", escolhido.id)

        # Retira o escolhido da lista de sorteio atual
        idx = opcoes_sorteio.index(escolhido)
        opcoes_sorteio.pop(idx)
        pesos_sorteio.pop(idx)

    # NÃO grava HistoricoEnvio aqui! Só após o envio bem-sucedido (ver
    # management/commands/enviar_oferta.py). Gravar antes congelaria o produto
    # no cooldown mesmo se o link/envio falhasse.
    return vencedores


def selecionar_item_para_grupo(macros_selecionadas=None, categorias_selecionadas=None,
                               limite_envio=1, horas_cooldown=24,
                               min_desconto_percent=15.0, termo=None,
                               marketplace=None, usuario=None, grupo_id=None):
    """Ranking determinístico, explicável e personalizado por desempenho."""
    from django.db.models import Q
    from apps.scrapers.marketplaces.registry import get_marketplace

    qs = Produto.objects.exclude(origem="cupom").exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"])
    qs = qs.filter(Q(valido_ate__isnull=True) | Q(valido_ate__gte=timezone.now()))
    qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario)) if usuario else qs.filter(
        owner__isnull=True)
    if marketplace:
        qs = qs.filter(marketplace=marketplace)
    if macros_selecionadas:
        qs = qs.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)
    if termo:
        cond = Q()
        for palavra in [p.strip() for p in termo.split(",") if p.strip()]:
            cond |= Q(nome__icontains=palavra)
        if cond:
            qs = qs.filter(cond)

    elegiveis = qs.annotate(
        economia_rs=ExpressionWrapper(
            F("preco_sem_desconto") - F("preco_com_cupom"),
            output_field=FloatField()),
        desconto_percent=ExpressionWrapper(
            (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0
            / F("preco_sem_desconto"), output_field=FloatField()),
    ).filter(
        desconto_percent__gte=min_desconto_percent,
        desconto_percent__lt=90, preco_com_cupom__gt=0,
    )
    cupons = {
        c.campanha_id: c for c in Cupom.objects.filter(
            campanha_id__in=elegiveis.values_list("campanha_id", flat=True),
            estado="ativo",
        ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
    }
    recentes = {}
    if usuario and grupo_id:
        desde = timezone.now() - timedelta(hours=horas_cooldown)
        for pub in Publicacao.objects.filter(
            usuario=usuario, destino_id=grupo_id, produto__isnull=False,
        ).filter(
            Q(status="enviado", enviada_em__gte=desde)
            | Q(status="incerto", criada_em__gte=desde)
        ).order_by("produto_id", "-criada_em"):
            recentes.setdefault(pub.produto_id, pub.preco_final)
    desempenho = {}
    if usuario:
        for row in Publicacao.objects.filter(
            usuario=usuario, status="enviado"
        ).values("produto_id").annotate(
            posts=Count("id", distinct=True), clicks=Count("cliques")):
            desempenho[row["produto_id"]] = row

    opcoes = []
    for produto in elegiveis:
        cupom = cupons.get(produto.campanha_id)
        if cupom and cupom.valor_minimo > produto.preco_sem_desconto:
            continue
        anterior = recentes.get(produto.id)
        if anterior and produto.preco_com_cupom > anterior * .95:
            continue
        score = produto.desconto_percent * 2 + produto.economia_rs / 20
        motivos = [f"{produto.desconto_percent:.0f}% de desconto"]
        if produto.confianca == "alta":
            score *= 1.15
            motivos.append("fonte de alta confiança")
        elif produto.confianca == "baixa":
            score *= .75
        if produto.preco_com_cupom < 30:
            score += 20
            motivos.append("ticket acessível")
        # Feedback da cliente: cupom e oferta relâmpago vendem muito mais.
        if cupom and cupom.data_criacao >= timezone.now() - timedelta(hours=12):
            score *= 1.5
            motivos.append("cupom recente")
        elif cupom or getattr(produto, "codigo_checkout", ""):
            score *= 1.2
            motivos.append("tem cupom")
        if getattr(produto, "relampago", False):
            score *= 1.4
            motivos.append("oferta relâmpago")
        historico = _stats_preco(produto, dias=30)
        if historico and historico["n"] >= 3:
            if produto.preco_com_cupom >= historico["mediana"] * .98:
                continue
            if produto.preco_com_cupom <= historico["minimo"] * 1.02:
                score *= 1.6
                motivos.append("mínima de 30 dias")
        perf = desempenho.get(produto.id)
        if perf and perf["posts"]:
            score += min(60, perf["clicks"] / perf["posts"] * 12)
            if perf["clicks"]:
                motivos.append(f"{perf['clicks']} clique(s) anteriores")
        produto.score_oferta = round(score, 2)
        produto.motivos_score = motivos
        opcoes.append(produto)

    opcoes.sort(key=lambda p: (-p.score_oferta, p.id))
    escolhidos = []
    for produto in opcoes:
        if len(escolhidos) >= limite_envio:
            break
        estado = get_marketplace(produto.marketplace).is_alive(produto)
        campos = {"ultima_verificacao": timezone.now()}
        if estado is True:
            campos.update(estado="ativo", falha_verificacao="")
            escolhidos.append(produto)
        elif estado is False:
            campos.update(estado="indisponivel",
                          falha_verificacao="Oferta indisponível na verificação")
        else:
            campos["falha_verificacao"] = "Não foi possível confirmar a oferta"
        Produto.objects.filter(pk=produto.pk).update(**campos)
    return escolhidos


def _frase_marketing(produto):
    """Frase de marketing: usa o cache (frase_llm); só chama a API do Claude ao vivo
    como último recurso, com timeout curto (10s, não os 120s antigos que travavam o
    envio), e GRAVA no cache p/ o próximo envio ser instantâneo."""
    cache = getattr(produto, "frase_llm", "") or ""
    if cache:
        return cache
    from apps.scrapers.llm import gerar_descricao
    preco = getattr(produto, "preco_com_cupom", None)
    de = getattr(produto, "preco_sem_desconto", 0) or 0
    desconto = ((de - preco) / de) * 100 if preco and de and de > preco else None
    frase = gerar_descricao(
        produto.nome, timeout=10, preco=preco, desconto_percent=desconto,
        categoria=getattr(produto, "macro_categoria", "") or getattr(produto, "categoria", ""),
    )
    if frase and hasattr(produto, "save") and getattr(produto, "pk", None):
        try:
            produto.frase_llm = frase
            produto.save(update_fields=["frase_llm"])
        except Exception:
            pass
    return frase


def _nome_loja(marketplace, cupom=None) -> str:
    """Nome de exibição da loja (espelha o rótulo da tela de Promoções)."""
    m = str(marketplace or "").strip().lower()
    if m in ("mercadolivre", "mercado livre", "meli"):
        return "Mercado Livre"
    if m == "awin":
        return str(getattr(cupom, "anunciante_nome", "") or "Awin")
    return str(marketplace or "Loja").title()


def montar_mensagem_cupom(cupom, markup=None, link_afiliado=None) -> str:
    """Monta o texto de divulgação de um cupom (CupomNormalizado) p/ envio manual.

    Usa o `Markup` do canal e os dados de `cupom.regras` (valor_desconto/discount_num,
    min_compra, desconto_max) quando existirem — só entra o que houver. Cupom não tem
    foto de produto: sai como mensagem de texto. Segue o modelo pedido:

        Novo cupom ⚡️ Mercado Livre

        🛒 15% DE DESCONTO acima de R$79 (limitado a R$60)
        🎟 Use o cupom TAMOJUNTO

        Clique no link e navegue na página do Meli:

        ➡️ https://mercadolivre.com/sec/2J8HDRK
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    from apps.scrapers.coupon_rules import (
        codigo_publicavel, escopo_produtos_cupom, formatar_numero, regras_do_cupom,
    )
    m = markup or WhatsAppMarkup()
    esc = m.escape
    regras = regras_do_cupom(cupom)
    is_meli = str(getattr(cupom, "marketplace", "") or "").strip().lower() in (
        "mercadolivre", "mercado livre", "meli")
    loja = _nome_loja(getattr(cupom, "marketplace", ""), cupom=cupom)

    linhas = [m.bold(f"Novo cupom ⚡️ {esc(loja)}"), ""]

    # Linha do desconto: "🛒 15% DE DESCONTO acima de R$79 (limitado a R$60)"
    numero_desconto = formatar_numero(regras.get("valor_desconto"))
    valor = ""
    if numero_desconto:
        valor = (f"{numero_desconto}%" if regras.get("tipo_desconto") == "porcentagem"
                 else f"R$ {numero_desconto}" if regras.get("tipo_desconto") == "fixo"
                 else numero_desconto)
    partes = []
    if valor:
        partes.append(f"{valor} DE DESCONTO")
    minimo = formatar_numero(regras.get("valor_minimo"))
    if minimo:
        partes.append(f"acima de R$ {minimo}")
    linha_desc = " ".join(partes).strip()
    desconto_max = formatar_numero(regras.get("desconto_maximo"))
    if desconto_max:
        limite = f"(limitado a R$ {desconto_max})"
        linha_desc = f"{linha_desc} {limite}".strip()
    if linha_desc:
        linhas.append(f"🛒 {m.bold(esc(linha_desc))}")

    escopo_produtos = escopo_produtos_cupom(cupom)
    if escopo_produtos:
        linhas.append(f"🏷️ {m.bold('Válido para:')} {esc(escopo_produtos)}")

    codigo = codigo_publicavel(cupom)
    if codigo:
        linhas.append(f"🎟 Use o cupom {m.bold(esc(codigo))}")
    else:
        linhas.append(f"🎟 {m.bold('Ative o cupom no link')}")

    if getattr(cupom, "restrito", False):
        condicao = str(regras.get("escopo") or "Consulte quem pode usar antes de comprar")
        # Se o único "restrito" é o conjunto de produtos, a linha acima já
        # informa a condição com mais clareza. Restrições de público/pagamento
        # continuam aparecendo obrigatoriamente aqui.
        if not (escopo_produtos and condicao.strip().casefold()
                == escopo_produtos.strip().casefold()):
            linhas.extend(["", f"⚠️ {m.bold('Condição:')} {esc(condicao[:220])}"])

    link = str(link_afiliado or getattr(cupom, "link", "") or "").strip()
    if link:
        onde = "na página do Meli" if is_meli else "na página da loja"
        linhas += ["", f"Clique no link e navegue {onde}:", "", f"➡️ {esc(link)}"]

    return "\n".join(linhas)


def _produto_para_cupom(cupom):
    """Fallback afiliavel comprovadamente compativel com o cupom."""
    from apps.scrapers.coupon_rules import regras_do_cupom
    from apps.scrapers.models import ProdutoCupom

    ativos = Produto.objects.exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).filter(marketplace=getattr(cupom, "marketplace", "mercadolivre"))
    vinculo = (ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=ativos,
    ).select_related("produto").order_by("-verificado_em", "-produto__ultima_observacao").first())
    if vinculo:
        return vinculo.produto

    external_id = str(getattr(cupom, "external_id", "") or "")
    if external_id.startswith("campanha:"):
        produto = ativos.filter(campanha_id=external_id.split(":", 1)[1]).order_by(
            "-ultima_observacao").first()
        if produto:
            return produto

    regras = regras_do_cupom(cupom)
    if regras.get("is_mar_aberto"):
        minimo = regras.get("valor_minimo") or 0
        return ativos.filter(preco_sem_desconto__gte=minimo).order_by(
            "-ultima_observacao").first()
    return None


def _macro_do_cupom(cupom) -> str:
    """Macro-categoria temática do cupom p/ agrupar ofertas. '' se não reconhecer.

    Preferência: a `categoria` do cupom quando já é uma macro real; senão classifica
    o título (ex.: 'produtos de Anadi Ferramentas' → 'Ferramentas e Manutenção').
    """
    cat = (getattr(cupom, "categoria", "") or "").strip()
    if cat in _EMOJI_MACRO:
        return cat
    try:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            classificar_cupom_por_titulo)
        macro = classificar_cupom_por_titulo(getattr(cupom, "titulo", "") or "")
        if macro in _EMOJI_MACRO:
            return macro
    except Exception:
        pass
    return ""


def produtos_do_cupom(cupom, limite=8):
    """Produtos p/ a mensagem-colagem do cupom (multi-item), melhores por desconto.

    (1) Ligação real cupom→produto quando existir — vínculo `ProdutoCupom`
        confirmado > campanha (`external_id` "campanha:X") > cupom de site inteiro
        (`is_mar_aberto`). Hoje isso é raro em produção (produto não guarda campanha).
    (2) Fallback (decisão de produto): sem ligação, agrupa as melhores OFERTAS da
        categoria do cupom — cada uma com o próprio De/por e link verdadeiros; o
        cupom define só o tema. Sem categoria reconhecível => vazio => texto puro.

    Só entra item com foto (a colagem precisa dela).
    """
    from apps.scrapers.coupon_rules import regras_do_cupom
    from apps.scrapers.models import ProdutoCupom

    mkt = getattr(cupom, "marketplace", "mercadolivre") or "mercadolivre"
    ativos = Produto.objects.exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).filter(marketplace=mkt).exclude(imagem_url="")

    def _por_desconto(qs):
        return qs.filter(preco_com_cupom__gt=0, preco_sem_desconto__gt=0).annotate(
            _desc=ExpressionWrapper(
                (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0
                / F("preco_sem_desconto"), output_field=FloatField()),
        ).filter(_desc__lt=90).order_by("-_desc")

    # (1) Ligação real, quando existir.
    conf_ids = list(ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=ativos,
    ).values_list("produto_id", flat=True))
    qs = None
    if conf_ids:
        qs = ativos.filter(id__in=conf_ids)
    else:
        external_id = str(getattr(cupom, "external_id", "") or "")
        if external_id.startswith("campanha:"):
            qs = ativos.filter(campanha_id=external_id.split(":", 1)[1])
        elif regras_do_cupom(cupom).get("is_mar_aberto"):
            minimo = regras_do_cupom(cupom).get("valor_minimo") or 0
            qs = ativos.filter(preco_sem_desconto__gte=minimo)
    itens = list(_por_desconto(qs)[:limite]) if qs is not None else []
    if itens:
        return itens

    # (2) Fallback por categoria.
    macro = _macro_do_cupom(cupom)
    if not macro:
        return []
    return list(_por_desconto(ativos.filter(origem="oferta", macro_categoria=macro))[:limite])


def _preparar_itens_cupom(cupom, usuario, limite=8):
    """[{produto, link}] com link de afiliado válido + foto, p/ a mensagem-colagem.

    Cada produto leva o PRÓPRIO link comissionado (como na imagem-modelo). Usa o
    cache em lote (`situacao_dos_links`) e, só p/ quem não tem, gera via Link
    Builder. Se a sessão do ML cair, para de tentar (evita N falhas lentas) e
    devolve o que houver — o chamador cai no texto puro, que reporta a reconexão.
    """
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.afiliado import situacao_dos_links, salvar_cache
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada

    produtos = produtos_do_cupom(cupom, limite=limite * 2)
    if not produtos:
        return []
    mkt = str(getattr(cupom, "marketplace", "mercadolivre") or "mercadolivre").lower()
    mp = get_marketplace(mkt)
    situacao = situacao_dos_links(usuario, produtos)

    itens, sessao_caiu = [], False
    for p in produtos:
        if len(itens) >= limite:
            break
        link = ((situacao.get(p.id) or {}).get("link_afiliado")
                or getattr(p, "link_afiliado", "") or "")
        if not link and not sessao_caiu:
            try:
                info = mp.build_affiliate_link(p, usuario=usuario)
            except (LoginError, AuthError, SessaoExpirada, BrowserError) as exc:
                logger.warning("Sessão/navegador ao afiliar produto %s do cupom %s: %s",
                               getattr(p, "id", "?"), getattr(cupom, "pk", "?"), exc)
                sessao_caiu = True
                info = None
            except Exception as exc:
                logger.debug("Falha ao afiliar produto %s do cupom: %s",
                             getattr(p, "id", "?"), exc)
                info = None
            if info and info.get("link_afiliado") and info.get("afiliado_ok") is not False:
                link = info["link_afiliado"]
                try:
                    salvar_cache(usuario, p, link, info.get("url_isca", ""), True)
                except Exception:
                    pass
        if link:
            itens.append({"produto": p, "link": link})
    return itens


def montar_mensagem_cupom_produtos(cupom, itens, markup=None) -> str:
    """Mensagem de cupom no formato da imagem-modelo: cabeçalho + lista de produtos.

        Cupom ⚡ Mercado Livre

        📖 Chama de Ferro | Capa dura
        🛒 De ~R$197,90~ por R$83,54
        ➡️ https://meli.la/...

    Cada produto leva nome, preço De/por e o próprio link. A foto vai na colagem
    (imagem única acima da mensagem), montada em `colagem.montar_colagem_b64`.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    m = markup or WhatsAppMarkup()
    esc = m.escape

    loja = _nome_loja(getattr(cupom, "marketplace", ""), cupom=cupom)
    linhas = [m.bold(f"Cupom ⚡ {esc(loja)}"), ""]
    for it in itens:
        p = it["produto"]
        linhas.append(f"📖 {m.bold(esc(p.nome.strip()))}")
        por = _preco_br(p.preco_com_cupom)
        de_val = p.preco_sem_desconto or 0
        pct = ((de_val - p.preco_com_cupom) / de_val * 100) if de_val else 0
        if 0 < pct < 90 and de_val > p.preco_com_cupom:
            de = _preco_br(de_val)
            linhas.append(f"🛒 De {m.strike(f'R${de}')} por {m.bold(f'R${por}')}")
        else:
            linhas.append(f"🛒 {m.bold(f'R${por}')}")
        linhas.append(f"➡️ {esc(it['link'])}")
        linhas.append("")
    return "\n".join(linhas).strip()


def resolver_link_afiliado_cupom(cupom, usuario):
    """Gera link comissionado direto; cai para produto confirmado quando preciso."""
    from apps.scrapers.models import LinkAfiliadoCupomUsuario
    from apps.scrapers.marketplaces.registry import get_marketplace

    if usuario is None:
        return {"sucesso": False, "motivo": "Usuário ausente para gerar o link afiliado."}
    if getattr(cupom, "owner_id", None) and cupom.owner_id != usuario.id:
        return {"sucesso": False, "motivo": "Este cupom pertence a outra conta."}
    marketplace = str(getattr(cupom, "marketplace", "") or "").strip().lower()
    origem = str(getattr(cupom, "link", "") or "").strip()
    if marketplace == "awin":
        integracao = getattr(cupom, "integracao", None)
        programa = getattr(cupom, "programa", None)
        if (integracao and integracao.owner_id == usuario.id
                and integracao.habilitada and integracao.status == "conectada"
                and programa and programa.habilitado and programa.status_vinculo == "joined"
                and programa.link_status == "online" and origem.startswith(("http://", "https://"))):
            return {"sucesso": True, "link": origem, "cache": True}
        return {"sucesso": False, "motivo": "A conta ou o anunciante Awin não está ativo."}
    if marketplace == "amazon":
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        from apps.scrapers.afiliado import tag_amazon
        tag = tag_amazon(usuario)
        if not tag or not origem.startswith(("http://", "https://")):
            return {"sucesso": False, "motivo": "Cadastre sua tag Amazon para usar este cupom."}
        parts = urlsplit(origem)
        if not (parts.hostname or "").lower().endswith("amazon.com.br"):
            return {"sucesso": False, "motivo": "O link informado não pertence à Amazon Brasil."}
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["tag"] = tag
        return {"sucesso": True,
                "link": urlunsplit((parts.scheme, parts.netloc, parts.path,
                                     urlencode(query), parts.fragment))}
    if marketplace != "mercadolivre":
        return {"sucesso": False,
                "motivo": "Esta loja ainda não oferece link afiliado para cupons."}
    cache = LinkAfiliadoCupomUsuario.objects.filter(
        usuario=usuario, cupom=cupom, afiliado_ok=True,
    ).first()
    # O cache pertence ao par usuario+cupom. A URL de origem pode ser a pagina
    # do cupom ou um produto fallback comprovado; em ambos os casos o link salvo
    # já passou pela verificacao de comissionamento.
    if cache and cache.link_afiliado:
        return {"sucesso": True, "link": cache.link_afiliado, "cache": True}

    erro_direto = ""
    if origem:
        try:
            from apps.scrapers.scraper_mercadolivre.link import afiliate_link_builder
            from apps.scrapers.session_paths import ml_auth_path
            link = afiliate_link_builder(origem, auth_path=ml_auth_path(usuario))
            if link and get_marketplace(marketplace).verify_affiliate_tag(
                    link, usuario=usuario):
                LinkAfiliadoCupomUsuario.objects.update_or_create(
                    usuario=usuario, cupom=cupom,
                    defaults={"url_origem": origem, "link_afiliado": link,
                              "afiliado_ok": True},
                )
                return {"sucesso": True, "link": link, "cache": False}
            erro_direto = "A página do cupom não foi aceita pelo programa de afiliados."
        except Exception as exc:
            from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
            from apps.scrapers.auxiliar import SessaoExpirada
            if isinstance(exc, (LoginError, AuthError, SessaoExpirada)):
                logger.warning("Sessão ML expirada ao afiliar cupom %s: %s", cupom.pk, exc)
                return {"sucesso": False,
                        "motivo": "Sessão do Mercado Livre expirada. Reconecte sua conta.",
                        "precisa_login_ml": True}
            logger.warning("Falha ao afiliar pagina do cupom %s: %s", cupom.pk, exc)
            erro_direto = "Não foi possível gerar o link afiliado da página do cupom."

    produto = _produto_para_cupom(cupom)
    if produto:
        mp = get_marketplace(marketplace)
        try:
            info = mp.build_affiliate_link(produto, usuario=usuario)
        except Exception as exc:
            from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
            from apps.scrapers.auxiliar import SessaoExpirada
            if isinstance(exc, (LoginError, AuthError, SessaoExpirada)):
                logger.warning("Sessão ML expirada no fallback do cupom %s: %s", cupom.pk, exc)
                return {"sucesso": False,
                        "motivo": "Sessão do Mercado Livre expirada. Reconecte sua conta.",
                        "precisa_login_ml": True}
            logger.warning("Falha ao afiliar produto fallback do cupom %s: %s", cupom.pk, exc)
            info = None
        if info and info.get("link_afiliado"):
            link = info["link_afiliado"]
            if info.get("afiliado_ok") is not False:
                LinkAfiliadoCupomUsuario.objects.update_or_create(
                    usuario=usuario, cupom=cupom,
                    defaults={"url_origem": produto.link_produto, "link_afiliado": link,
                              "afiliado_ok": True},
                )
                return {"sucesso": True, "link": link, "produto": produto}

    return {"sucesso": False, "motivo": erro_direto or
            "Nenhum produto aplicável permitiu gerar um link afiliado para este cupom."}


def enviar_cupom(cupom, grupo_id, *, canal="whatsapp", usuario=None, destino_nome="",
                 imagem_b64_custom=None, configuracao=None, score=0, motivos_score=None):
    """Nucleo auditavel do envio manual de CupomNormalizado.

    `imagem_b64_custom` (opcional): foto escolhida no envio. Cupom não tem foto de
    produto, então sem ela sai como texto puro (comportamento de sempre); com ela,
    a foto vira a imagem acima da mensagem (só no transporte base64/WhatsApp)."""
    from django.contrib.auth import get_user_model
    from apps.scrapers.coupon_rules import codigo_publicavel
    from apps.scrapers.eventos import log_event
    from apps.scrapers.senders.registry import get_sender

    try:
        sender = get_sender(canal)
    except ValueError as exc:
        return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
    if not usuario or not grupo_id:
        return {"sucesso": False, "motivo": "Usuário ou destino ausente.",
                "classe": "permanente"}
    agora = timezone.now()
    if cupom.estado != "ativo" or (cupom.validade and cupom.validade < agora):
        return {"sucesso": False, "motivo": "Cupom não encontrado, inativo ou vencido.",
                "classe": "permanente"}

    desde = agora - timedelta(hours=24)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=usuario.pk)
        cupom = type(cupom).objects.select_for_update().get(pk=cupom.pk)
        if cupom.estado != "ativo" or (cupom.validade and cupom.validade < agora):
            return {"sucesso": False,
                    "motivo": "Cupom não encontrado, inativo ou vencido.",
                    "classe": "permanente"}
        recente = Publicacao.objects.filter(
            usuario=usuario, origem="cupom", cupom_normalizado=cupom,
            canal=canal, destino_id=grupo_id,
        ).filter(
            Q(status="pendente", criada_em__gte=agora - timedelta(minutes=30))
            | Q(status="enviado", enviada_em__gte=desde)
            | Q(status="incerto", criada_em__gte=desde)
        ).order_by("-criada_em").first()
        if recente:
            motivo = ("Este cupom já está sendo enviado para o destino."
                      if recente.status == "pendente"
                      else "Este destino já recebeu o cupom nas últimas 24h.")
            return {"sucesso": False, "motivo": motivo, "duplicado": True,
                    "classe": "permanente"}

        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.bloqueado:
            return {"sucesso": False, "motivo": "Conta bloqueada para envios.",
                    "classe": "permanente"}
        inicio_dia = timezone.localtime(agora).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
        limite = perfil.cota_max_envios_dia() if perfil else 0
        usados = Publicacao.objects.filter(
            usuario=usuario, criada_em__gte=inicio_dia,
            status__in=("pendente", "enviado", "incerto"),
        ).count()
        if limite and usados >= limite:
            return {"sucesso": False, "motivo": "Limite diário de envios atingido.",
                    "classe": "permanente"}
        publicacao = Publicacao.objects.create(
            usuario=usuario, origem="cupom", cupom_normalizado=cupom,
            configuracao=configuracao,
            canal=canal, destino_id=str(grupo_id)[:100],
            destino_nome=str(destino_nome or "")[:255],
            cupom=str(codigo_publicavel(cupom) or cupom.titulo or "")[:255],
            categoria="Cupom", score=float(score or 0),
            motivos_score=list(motivos_score or []),
        )

    def falhar(motivo, **extra):
        erro_tecnico = extra.pop("_erro_tecnico", "")
        incerto = extra.get("resultado") == "incerto"
        Publicacao.objects.filter(pk=publicacao.pk, status="pendente").update(
            status="incerto" if incerto else "falhou", erro=str(motivo)[:500])
        log_event("publicacao", "send_failed", str(motivo), level="warning",
                  usuario=usuario, contexto={"publicacao_id": publicacao.id,
                                             "cupom_id": cupom.id, "canal": canal,
                                             "destino": destino_nome or grupo_id,
                                             "erro_tecnico": erro_tecnico, **extra})
        return {"sucesso": False, "motivo": str(motivo), **extra}

    try:
        # Caminho preferido (imagem-modelo): produtos do cupom viram uma colagem +
        # lista com De/por e link próprio por item. Sem produtos afiliáveis (ou com
        # foto custom escolhida no envio), cai no texto puro de sempre.
        itens_cupom = [] if imagem_b64_custom else _preparar_itens_cupom(cupom, usuario)
        img_kwargs = {}
        if itens_cupom:
            mensagem = montar_mensagem_cupom_produtos(
                cupom, itens_cupom, markup=sender.markup)
            link_registro = itens_cupom[0]["link"]
            # Colagem só no transporte base64/WhatsApp; Telegram (url) segue a lista em texto.
            if getattr(sender, "prefers_image", "") != "url":
                from apps.scrapers.colagem import montar_colagem_b64
                colagem_b64, colagem_mime = montar_colagem_b64(
                    [it["produto"].imagem_url for it in itens_cupom])
                if colagem_b64:
                    img_kwargs = {"imagem_b64": colagem_b64, "mimetype": colagem_mime}
        else:
            afiliado = resolver_link_afiliado_cupom(cupom, usuario)
            if not afiliado.get("sucesso"):
                return falhar(afiliado.get("motivo") or "Link afiliado indisponível.",
                              precisa_login_ml=afiliado.get("precisa_login_ml", False),
                              classe="permanente")
            mensagem = montar_mensagem_cupom(
                cupom, markup=sender.markup, link_afiliado=afiliado["link"])
            link_registro = afiliado["link"]
            # Só o transporte base64/WhatsApp aceita a foto custom; Telegram segue texto.
            if imagem_b64_custom and getattr(sender, "prefers_image", "") != "url":
                img_kwargs = {"imagem_b64": imagem_b64_custom, "mimetype": "image/jpeg"}
        if not mensagem.strip():
            return falhar("Não foi possível montar uma mensagem válida.", classe="permanente")
        Publicacao.objects.filter(pk=publicacao.pk).update(
            mensagem=mensagem, link_afiliado=link_registro,
            link_rastreado=link_registro)
        resultado = sender.enviar_oferta(
            grupo_id, mensagem, legenda=mensagem, usuario=usuario,
            session=wa_session_de(usuario), **img_kwargs)
        if resultado.get("sucesso"):
            Publicacao.objects.filter(pk=publicacao.pk).update(
                status="enviado", enviada_em=timezone.now())
            log_event("publicacao", "send_ok", "Cupom publicado com sucesso.",
                      usuario=usuario, contexto={"publicacao_id": publicacao.id,
                                                 "cupom_id": cupom.id, "canal": canal,
                                                 "destino": destino_nome or grupo_id,
                                                 "via": resultado.get("via")})
            return {"sucesso": True, "via": resultado.get("via", canal),
                    "canal": resultado.get("canal", canal),
                    "link": afiliado["link"], "mensagem": mensagem,
                    "publicacao": publicacao,
                    "mensagem_id": resultado.get("mensagem_id"),
                    "classe": resultado.get("classe", ""),
                    "resultado": resultado.get("resultado", "confirmado"),
                    "repetir": resultado.get("repetir", False),
                    "etapa": resultado.get("etapa", "transporte"),
                    "duracao_ms": resultado.get("duracao_ms", 0)}
        return falhar(_motivo_publico_transporte(resultado),
                      _erro_tecnico=resultado.get("erro") or "",
                      classe=resultado.get("classe"), resultado=resultado.get("resultado"),
                      repetir=resultado.get("repetir"), etapa=resultado.get("etapa"),
                      duracao_ms=resultado.get("duracao_ms"),
                      falha_infra=resultado.get("falha_infra", False))
    except Exception as exc:
        logger.exception("Erro inesperado ao enviar cupom %s", cupom.pk)
        return falhar("Falha inesperada ao preparar o cupom.", classe="desconhecido",
                      causa=type(exc).__name__)


# Emoji por macro-categoria p/ a linha do produto na mensagem curta. Fallback 🛍️.
_EMOJI_MACRO = {
    "Celulares, Telefonia e Wearables": "📱",
    "Eletrônicos e Informática": "💻",
    "Áudio, Vídeo e Fotografia": "🎧",
    "Eletrodomésticos": "🔌",
    "Cozinha, Mesa e Bar": "🍽️",
    "Casa, Móveis e Decoração": "🛋️",
    "Beleza e Cuidados Pessoais": "💄",
    "Moda, Calçados e Acessórios": "👕",
    "Esportes e Fitness": "🏋️",
    "Games, Brinquedos e Hobbies": "🎮",
    "Ferramentas e Manutenção": "🔧",
    "Automotivo": "🚗",
    "Pets e Animais": "🐾",
    "Bebês e Maternidade": "🍼",
    "Alimentos e Bebidas": "🍫",
    "Saúde, Ortopedia e Equipamentos Médicos": "💊",
    "Papelaria, Escritório e Escola": "✏️",
}


def _emoji_produto(produto) -> str:
    macro = getattr(produto, "macro_categoria", "") or ""
    return _EMOJI_MACRO.get(macro, "🛍️")


def _preco_br(valor) -> str:
    """R$ no formato brasileiro sem 'R$' e sem centavos zerados: 49,90 / 352."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ""
    if numero.is_integer():
        return str(int(numero))
    return f"{numero:.2f}".replace(".", ",")


def montar_mensagem(produto, link_afiliado: str, cupom_pai, markup=None,
                    usuario=None, configuracao=None, variante="A") -> str:
    """
    Monta o texto da oferta usando o `Markup` do canal (WhatsApp *neg*, Telegram <b>).
    Conteúdo dinâmico passa por markup.escape p/ não quebrar HTML do Telegram.

    Formato curto (modelo dos grupos): título da IA em caixa alta, produto, preço
    DE|POR, cupom (quando há código publicável) e link.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    m = markup or WhatsAppMarkup()
    esc = m.escape

    economia_rs = produto.preco_sem_desconto - produto.preco_com_cupom
    desconto_percent = (economia_rs / produto.preco_sem_desconto) * 100 if produto.preco_sem_desconto else 0
    perfil = getattr(usuario, "perfil", None) if usuario else None
    marca = (
        getattr(configuracao, "nome_marca", "")
        or getattr(perfil, "nome_marca", "") or "Ofertas"
    ).strip()
    cta = (
        getattr(configuracao, "chamada_acao", "")
        or getattr(perfil, "chamada_acao", "") or "Compre aqui"
    ).strip()
    disclosure = (
        getattr(configuracao, "divulgacao_afiliado", "")
        or getattr(perfil, "divulgacao_afiliado", "") or ""
    ).strip()
    template = (
        getattr(configuracao, "template_b" if variante == "B" else "template_a", "")
        or getattr(perfil, "template_b" if variante == "B" else "template_a", "")
    )
    if template:
        try:
            return template.format(
                marca=marca, nome=produto.nome,
                preco=f"R$ {produto.preco_com_cupom:.2f}",
                desconto=f"{desconto_percent:.0f}%", link=link_afiliado,
            )
        except (KeyError, ValueError):
            pass

    # Blocos separados por linha em branco, no estilo dos grupos:
    #   TÍTULO
    #   (blank)
    #   {emoji} Produto
    #   (blank)
    #   🔥 DE X | POR Y     [+ 🎟️ CUPOM: ... colado embaixo]
    #   🔗 link
    linhas = []
    # Título da IA (frase_llm) em caixa alta, no topo — a "chamada" do grupo.
    titulo = _frase_marketing(produto)
    if titulo:
        linhas += [m.bold(esc(titulo)), ""]

    linhas += [f"{_emoji_produto(produto)} {m.bold(esc(produto.nome.strip()))}", ""]

    # Guarda final: desconto >= 90% (ou "De:" <= "Por:") indica preço corrompido
    # (ex.: savingBasis em escala errada). Em vez de imprimir "100% OFF" absurdo,
    # esconde a parte "DE" e mostra só o "POR".
    desconto_valido = 0 < desconto_percent < 90 and produto.preco_sem_desconto > produto.preco_com_cupom
    por = _preco_br(produto.preco_com_cupom)
    if desconto_valido:
        de = _preco_br(produto.preco_sem_desconto)
        linhas.append(f"🔥 DE {m.strike(de)} | {m.bold(f'POR {por}')}")
    else:
        linhas.append(f"🔥 {m.bold(f'POR {por}')}")

    # REGRA: cupons NÃO acumulam no ML. Cada item anuncia no máximo UM cupom.
    # Prioridade: cupom do link (cupom_pai) > código do próprio item (codigo_checkout)
    # > melhor código genérico VÁLIDO para este item. Nunca os três juntos.
    cod_item = getattr(produto, "codigo_checkout", "")
    linha_cupom = None
    if cupom_pai is not None:
        linha_cupom = f"🎟️ {m.bold('CUPOM: ative no link')}"
    elif cod_item:
        linha_cupom = f"🎟️ {m.bold(f'CUPOM: {esc(cod_item)}')}"
    elif (getattr(produto, "marketplace", "") == "amazon"
          and (getattr(produto, "evidencia", {}) or {}).get("promotion", {}).get("coupon_confirmed")):
        linha_cupom = f"🎟️ {m.bold('CUPOM: ative na página da Amazon')}"
    else:
        # Códigos genéricos (CupomCodigo) são de checkout do ML — NÃO valem na Amazon.
        mkt = getattr(produto, "marketplace", "mercadolivre")
        codigo = None
        if mkt in ("mercadolivre", ""):
            codigo = _melhor_cupom_normalizado(produto) or _melhor_codigo(produto)
        if codigo:
            linha_cupom = f"🎟️ {m.bold(f'CUPOM: {esc(codigo)}')}"

    if linha_cupom:
        # Com cupom: cola embaixo do preço e separa o link com uma linha em branco.
        linhas += [linha_cupom, ""]
    linhas.append(f"🔗 {esc(link_afiliado)}")
    return "\n".join(linhas)


# Back-compat: chamadas antigas continuam funcionando (markup WhatsApp default).
def montar_mensagem_whatsapp(produto, link_afiliado: str, cupom_pai) -> str:
    return montar_mensagem(produto, link_afiliado, cupom_pai)


def _melhor_codigo(produto):
    """
    Devolve o ÚNICO melhor código de checkout VÁLIDO para este item (ou None).

    Cupons não acumulam: escolhemos um só. Filtra por categoria/mínimo/validade
    via CupomCodigo.aplica_em e prioriza o de maior desconto percentual estimado.
    """
    from apps.scrapers.models import CupomCodigo
    # Códigos descobertos por regex na página do ML não possuem vínculo comprovado
    # com o produto. Permanecem no catálogo, mas nunca entram automaticamente.
    candidatos = [c for c in CupomCodigo.objects.filter(ativo=True)
                  .exclude(descricao="cupom ML (checkout)") if c.aplica_em(produto)]
    if not candidatos:
        return None

    def desconto_est(c):
        if c.tipo_desconto == "porcentagem":
            return produto.preco_com_cupom * (c.valor_desconto / 100.0)
        return c.valor_desconto

    melhor = max(candidatos, key=desconto_est)
    return f"{melhor.codigo} — {melhor.descricao}" if melhor.descricao else melhor.codigo


def _melhor_cupom_normalizado(produto):
    """Melhor CupomNormalizado (catálogo das fontes) VÁLIDO p/ este item ML, ou None.

    GATE DE CONFIANÇA: só entra na mensagem um cupom cuja aplicação a ESTE produto é
    segura — ou ele vale para o site inteiro (regras.is_mar_aberto), ou existe um
    ProdutoCupom 'confirmado' ligando os dois. Cupom de container/categoria sem match
    confirmado NÃO entra: melhor não anunciar cupom do que colar um que o produto não
    aceita no checkout. Respeita a compra mínima (regras.min_compra) e escolhe o de
    maior desconto (regras.discount_num).
    """
    from apps.scrapers.models import CupomNormalizado, ProdutoCupom
    if getattr(produto, "marketplace", "mercadolivre") not in ("mercadolivre", ""):
        return None
    agora = timezone.now()
    base = CupomNormalizado.objects.filter(
        marketplace="mercadolivre", estado="ativo",
    ).filter(Q(validade__isnull=True) | Q(validade__gte=agora))

    ids_confirmados = set()
    if getattr(produto, "pk", None):
        ids_confirmados = set(ProdutoCupom.objects.filter(
            produto=produto, status="confirmado", cupom__in=base,
        ).values_list("cupom_id", flat=True))

    preco = getattr(produto, "preco_com_cupom", 0) or 0
    melhor, melhor_desc = None, -1.0
    for c in base:
        from apps.scrapers.coupon_rules import regras_do_cupom, codigo_publicavel
        regras = regras_do_cupom(c)
        if not (regras.get("is_mar_aberto") or c.id in ids_confirmados):
            continue
        try:
            minimo = float(regras.get("valor_minimo") or 0)
        except (TypeError, ValueError):
            minimo = 0.0
        if preco < minimo:
            continue
        try:
            desc = float(regras.get("valor_desconto") or 0)
        except (TypeError, ValueError):
            desc = 0.0
        if desc > melhor_desc:
            melhor, melhor_desc = c, desc
    return (codigo_publicavel(melhor) or None) if melhor else None


def _baixar_imagem_b64(url):
    """
    Baixa a imagem e converte p/ JPEG -> (base64, 'image/jpeg').
    Converte porque o whatsapp-web.js falha ao enviar webp (formato padrão do ML).
    ('', '') se falhar/sem url.
    """
    if not url or not url.startswith("http"):
        return "", ""
    import base64
    from io import BytesIO
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200 or not r.content:
            return "", ""
        from PIL import Image
        img = Image.open(BytesIO(r.content)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception as e:
        logger.debug("Falha ao processar imagem da oferta: %s", e)
        return "", ""


def _link_publicado(publicacao, link_afiliado: str) -> str:
    """Link que entra na mensagem enviada ao grupo: sempre o link de afiliado
    direto (meli.la / amazon.com.br).

    Uma URL do sistema (spreading-web.fly.dev/r/...) na mensagem denuncia
    promoção automatizada — decisão de produto. O custo aceito é a contagem
    interna de cliques parar nos envios novos; a comissão continua vindo dos
    relatórios das lojas. O redirecionador (/r/<slug>/ e /scrapers/r/<token>/)
    segue no ar só para as mensagens já publicadas.
    """
    return link_afiliado


def enviar_oferta_de_produto(produto, grupo_id, verificar=True, dry_run=False,
                             canal="whatsapp", usuario=None, configuracao=None,
                             destino_nome="", imagem_b64_custom=None):
    """
    Núcleo de envio reutilizável e AGNÓSTICO de loja/canal:
      resolve marketplace (link afiliado + verificação) e sender (transporte) via registry.
      garante link -> checa tag afiliado (A3) -> (opcional) verifica destino -> monta msg
      no markup do canal -> envia. Grava HistoricoEnvio SOMENTE em envio bem-sucedido.

    Retorna dict: {sucesso, motivo?, link?, mensagem?, verificacao?, via?}
    """
    from django.conf import settings
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.senders.registry import get_sender
    from apps.scrapers.eventos import log_event

    mp = get_marketplace(getattr(produto, "marketplace", "mercadolivre"))
    try:
        sender = get_sender(canal)
    except ValueError as exc:
        return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
    publicacao = None
    log_event(
        "publicacao", "send_started", f"Preparando envio para {destino_nome or grupo_id}.",
        usuario=usuario,
        contexto={
            "produto_id": getattr(produto, "id", None),
            "marketplace": getattr(produto, "marketplace", ""),
            "canal": canal,
            "destino": destino_nome or grupo_id,
        },
    )
    if usuario is not None:
        from django.contrib.auth import get_user_model
        agora_abertura = timezone.now()
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=usuario.pk)
            Produto.objects.select_for_update().get(pk=produto.pk)
            perfil = getattr(usuario, "perfil", None)
            inicio_dia = timezone.localtime(agora_abertura).replace(
                hour=0, minute=0, second=0, microsecond=0)
            limite = perfil.cota_max_envios_dia() if perfil else 0
            usados = Publicacao.objects.filter(
                usuario=usuario, criada_em__gte=inicio_dia,
                status__in=("pendente", "enviado", "incerto"),
            ).count()
            if perfil and perfil.bloqueado:
                return {"sucesso": False, "motivo": "Conta bloqueada para envios.",
                        "classe": "permanente"}
            if limite and usados >= limite:
                return {"sucesso": False, "motivo": "Limite diário de envios atingido.",
                        "classe": "permanente"}
            desde = agora_abertura - timedelta(hours=24)
            recente = Publicacao.objects.filter(
                usuario=usuario, origem="produto", produto=produto,
                canal=canal, destino_id=grupo_id,
            ).filter(
                Q(status="pendente", criada_em__gte=agora_abertura - timedelta(minutes=30))
                | Q(status="enviado", enviada_em__gte=desde)
                | Q(status="incerto", criada_em__gte=desde)
            ).order_by("-criada_em").first()
            if recente and produto.preco_com_cupom > recente.preco_final * .95:
                motivo = ("Esta oferta já está sendo enviada para o destino."
                          if recente.status == "pendente"
                          else "Este destino recebeu a oferta nas últimas 24h.")
                return {"sucesso": False, "motivo": motivo, "duplicado": True,
                        "classe": "permanente"}
            publicacao = Publicacao.objects.create(
                usuario=usuario, origem="produto", produto=produto,
                configuracao=configuracao, canal=canal,
                destino_id=str(grupo_id or "")[:100],
                destino_nome=str(destino_nome or "")[:255],
                preco_original=produto.preco_sem_desconto,
                preco_final=produto.preco_com_cupom,
                categoria=produto.macro_categoria or produto.categoria or "",
                score=getattr(produto, "score_oferta", 0),
                motivos_score=getattr(produto, "motivos_score", []),
            )

    def falhar(motivo, **extra):
        erro_tecnico = extra.pop("_erro_tecnico", "")
        texto_motivo = str(motivo).lower()
        etapa = extra.get("etapa") or ""
        causa = extra.get("causa") or (
            "whatsapp_preflight_timeout" if etapa == "getState" and (extra.get("falha_infra") or "timeout" in texto_motivo) else
            "whatsapp_grupo_timeout" if etapa == "verificar_grupo" and (extra.get("falha_infra") or "timeout" in texto_motivo) else
            "whatsapp_store_recarregado" if etapa == "verificar_store" or "módulos internos" in texto_motivo else
            "whatsapp_frame_recarregado" if "frame" in texto_motivo or "recarregando" in texto_motivo else
            "whatsapp_confirmacao" if "confirma" in texto_motivo or "ack" in texto_motivo else
            "link_afiliado_recusado" if "link de afiliado" in texto_motivo or "link builder" in texto_motivo else
            "link_reprovado" if "link reprovado" in texto_motivo else
            "marketplace_login" if extra.get("precisa_login_ml") else
            "publicacao_falhou"
        )
        incerto = bool(extra.get("resultado") == "incerto")
        status = "incerto" if incerto else "falhou"
        if publicacao:
            Publicacao.objects.filter(pk=publicacao.pk).update(
                status=status, erro=str(motivo)[:500])
        contexto = {
            "produto_id": getattr(produto, "id", None),
            "marketplace": getattr(produto, "marketplace", ""),
            "canal": canal,
            "destino": destino_nome or grupo_id,
            "publicacao_id": getattr(publicacao, "id", None),
            "causa": causa,
            "erro_tecnico": erro_tecnico,
            **extra,
        }
        log_event(
            "publicacao", "send_failed", str(motivo), level="warning",
            usuario=usuario, contexto=contexto,
        )
        if extra.get("falha_infra") or incerto:
            log_event(
                "whatsapp", "send_timeout",
                "Serviço WhatsApp não confirmou o envio dentro do prazo.",
                level="error", usuario=usuario, contexto=contexto,
            )
        return {"sucesso": False, "motivo": str(motivo), **extra}

    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError

    # O trabalho roda aninhado para que QUALQUER exceção inesperada (a Publicacao já
    # existe como 'pendente' neste ponto) feche a linha antes de propagar. Sem isto,
    # um erro não previsto deixa a publicação pendente para sempre no dashboard.
    def _executar():
        try:
            info = mp.build_affiliate_link(produto, usuario=usuario)
        except (LoginError, AuthError, SessaoExpirada) as e:
            # Sessão do ML caída: sem link de afiliado NENHUM produto sai. Motivo claro
            # + flag p/ a UI oferecer a reconexão e o chamador parar de retentar.
            logger.warning("Sessão ML expirada ao afiliar produto %s: %s", produto.pk, e)
            return falhar("Sessão do Mercado Livre expirada. Reconecte sua conta.",
                          precisa_login_ml=True, _erro_tecnico=str(e))
        except BrowserError as e:
            texto = str(e)
            logger.warning("Falha do navegador ao afiliar produto %s: %s", produto.pk, e)
            precisa_login = "LOGIN_REQUIRED" in texto
            return falhar(
                "Sessão do Mercado Livre expirada. Reconecte sua conta."
                if precisa_login else "Não foi possível preparar o link afiliado.",
                precisa_login_ml=precisa_login, _erro_tecnico=texto)
        if not info:
            return falhar("falha ao gerar link de afiliado "
                          "(URL não afiliável ou o Link Builder recusou)")
        link = info["link_afiliado"]

        # A3 — sem tag de afiliado o clique não gera comissão. Recusa (ou avisa).
        afiliado_ok = info.get("afiliado_ok")
        if afiliado_ok is None:
            afiliado_ok = mp.verify_affiliate_tag(link, usuario=usuario)
        if not afiliado_ok:
            if getattr(settings, "AFILIADO_EXIGIR", True):
                return falhar("link sem tag de afiliado — não enviado", link=link)
            logger.warning("Link sem tag de afiliado; envio permitido por configuracao")

        verificacao = None
        if verificar:
            # 'oferta'/'busca' têm de/por confirmado na raspagem; 'cupom_codigo' precisa
            # confirmar o desconto/badge na PDP (confiar_desconto=False).
            origem = getattr(produto, "origem", "cupom")
            confiar = origem in ("oferta", "busca")
            try:
                verificacao = mp.verify_link(link, nome_esperado=produto.nome,
                                             confiar_desconto=confiar, usuario=usuario)
            except (LoginError, AuthError, SessaoExpirada) as e:
                # Mesma semântica do build: sessão caída na verificação também precisa
                # marcar a Publicacao como falha e acionar a reconexão na UI.
                logger.warning("Sessão ML expirada ao verificar produto %s: %s", produto.pk, e)
                return falhar("Sessão do Mercado Livre expirada. Reconecte sua conta.",
                              precisa_login_ml=True, _erro_tecnico=str(e))
            except BrowserError as e:
                texto = str(e)
                logger.warning("Falha do navegador ao verificar produto %s: %s", produto.pk, e)
                precisa_login = "LOGIN_REQUIRED" in texto
                return falhar(
                    "Sessão do Mercado Livre expirada. Reconecte sua conta."
                    if precisa_login else "Não foi possível verificar a oferta.",
                    precisa_login_ml=precisa_login, _erro_tecnico=texto)
            if not verificacao.get("ok"):
                return falhar("link reprovado na verificação",
                              link=link, verificacao=verificacao)

        # Ofertas (origem='oferta') não têm Cupom; só busca quando há campanha_id
        cupom = None
        if produto.campanha_id:
            cupom = Cupom.objects.filter(
                campanha_id=produto.campanha_id, estado="ativo",
            ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now())).first()
        variante = "A"
        if configuracao and configuracao.variante_template == "B":
            variante = "B"
        elif configuracao and configuracao.variante_template == "alternar":
            variante = "B" if configuracao.publicacoes.count() % 2 else "A"
        link_publicado = _link_publicado(publicacao, link)
        if publicacao:
            publicacao.variante = variante
            publicacao.link_afiliado = link
            publicacao.link_rastreado = link_publicado
            publicacao.cupom = (
                cupom.titulo if cupom else getattr(produto, "codigo_checkout", "") or "")
            publicacao.save(update_fields=[
                "variante", "link_afiliado", "link_rastreado", "cupom"])
        mensagem = montar_mensagem(
            produto, link_publicado, cupom, markup=sender.markup, usuario=usuario,
            configuracao=configuracao, variante=variante)
        if publicacao:
            publicacao.mensagem = mensagem
            publicacao.save(update_fields=["mensagem"])

        if dry_run:
            if publicacao:
                publicacao.status = "ignorado"
                publicacao.save(update_fields=["status"])
            return {"sucesso": True, "dry_run": True, "link": link,
                    "mensagem": mensagem, "verificacao": verificacao}

        # Sessão WhatsApp do DONO (multi-tenant): envia pela conexão dele, não pela default.
        wa_session = wa_session_de(usuario)

        # Imagem conforme o canal: Telegram aceita URL direto; WhatsApp precisa de base64.
        # Foto custom (opcional, escolhida no envio) só entra no caminho base64/WhatsApp;
        # sem ela, mantém a foto do produto como sempre.
        if sender.prefers_image == "url" and not imagem_b64_custom:
            resultado = sender.enviar_oferta(grupo_id, mensagem,
                                             imagem_url=getattr(produto, "imagem_url", "") or None,
                                             legenda=mensagem, usuario=usuario, session=wa_session)
        else:
            if imagem_b64_custom:
                imagem_b64, img_mime = imagem_b64_custom, "image/jpeg"
            else:
                imagem_b64, img_mime = _baixar_imagem_b64(getattr(produto, "imagem_url", ""))
            resultado = sender.enviar_oferta(grupo_id, mensagem, imagem_b64=imagem_b64 or None,
                                             mimetype=img_mime or "image/jpeg", legenda=mensagem,
                                             usuario=usuario, session=wa_session)

        if resultado.get("sucesso"):
            HistoricoEnvio.objects.create(produto=produto, usuario=usuario)  # só após sucesso
            if publicacao:
                Publicacao.objects.filter(pk=publicacao.pk).update(
                    status="enviado", enviada_em=timezone.now())
            log_event(
                "publicacao", "send_ok", "Oferta publicada com sucesso.",
                usuario=usuario,
                contexto={
                    "produto_id": getattr(produto, "id", None),
                    "marketplace": getattr(produto, "marketplace", ""),
                    "canal": canal,
                    "destino": destino_nome or grupo_id,
                    "via": resultado.get("via"),
                    "publicacao_id": getattr(publicacao, "id", None),
                },
            )
            return {"sucesso": True, "link": link, "mensagem": mensagem,
                    "via": resultado.get("via"), "verificacao": verificacao,
                    "canal": resultado.get("canal", canal),
                    "mensagem_id": resultado.get("mensagem_id"),
                    "classe": resultado.get("classe", ""),
                    "resultado": resultado.get("resultado", "confirmado"),
                    "repetir": resultado.get("repetir", False),
                    "etapa": resultado.get("etapa", "transporte"),
                    "duracao_ms": resultado.get("duracao_ms", 0),
                    "publicacao": publicacao}
        # `classe` decide se esta falha conta contra a config (ver
        # processar_configs_de_envio). Sem propagá-la aqui, toda falha de envio
        # chegaria ao orquestrador como 'desconhecido' e a taxonomia não valeria nada.
        return falhar(_motivo_publico_transporte(resultado),
                      _erro_tecnico=resultado.get("erro") or "",
                      link=link, verificacao=verificacao,
                      classe=resultado.get("classe"),
                      resultado=resultado.get("resultado"),
                      repetir=resultado.get("repetir"),
                      etapa=resultado.get("etapa"),
                      duracao_ms=resultado.get("duracao_ms"),
                      falha_infra=resultado.get("falha_infra", False))

    try:
        return _executar()
    except Exception as e:
        # Fecha a linha SÓ se ainda estiver pendente: uma exceção posterior ao desfecho
        # (ex.: no log do sucesso) não pode reescrever um envio que já deu certo. Depois
        # re-levanta — o estado no banco fica honesto sem alterar o fluxo de controle
        # que os chamadores (e o loop de automacao) já esperam.
        motivo = f"erro inesperado no envio: {e}"
        if publicacao and Publicacao.objects.filter(
            pk=publicacao.pk, status="pendente",
        ).update(status="falhou", erro=motivo[:500]):
            log_event(
                "publicacao", "send_failed", motivo, level="warning", usuario=usuario,
                contexto={
                    "produto_id": getattr(produto, "id", None),
                    "marketplace": getattr(produto, "marketplace", ""),
                    "canal": canal,
                    "destino": destino_nome or grupo_id,
                    "publicacao_id": publicacao.id,
                    "causa": "publicacao_inesperada",
                },
            )
        raise


def wa_session_de(usuario):
    """Sessão WhatsApp do dono (multi-tenant). None = sem dono (pool legado)."""
    if usuario is None:
        return None
    perfil = getattr(usuario, "perfil", None)
    if perfil is not None:
        return perfil.sessao_whatsapp()
    return str(getattr(usuario, "id", "")) or None


def selecionar_e_enviar(macros, grupo_id, min_desconto_percent=15.0,
                        horas_cooldown=24, max_tentativas=8, verificar=True, dry_run=False,
                        termo=None, canal="whatsapp", marketplace=None, usuario=None,
                        configuracao=None, destino_nome=""):
    """
    Seleciona um POOL de candidatos do nicho e tenta enviar um por um até o primeiro
    que passa na verificação. Devolve o resultado do envio bem-sucedido, ou o último
    erro / 'sem item elegível'. Evita abortar por causa de um único item que reprova.
    """
    if configuracao is not None:
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo
        pool = selecionar_conteudo_para_grupo(configuracao, limit=max_tentativas)
    else:
        pool = selecionar_item_para_grupo(
            macros_selecionadas=macros,
            limite_envio=max_tentativas,
            horas_cooldown=horas_cooldown,
            min_desconto_percent=min_desconto_percent,
            termo=termo,
            marketplace=marketplace,
            usuario=usuario,
            grupo_id=grupo_id,
        )
    if not pool:
        # Estoque vazio não é defeito da regra: resolve sozinho quando o scrape
        # traz produto novo. Marcar como transitório é o que impede a config de
        # nicho estreito de se autodesligar por simples falta de oferta.
        return {"sucesso": False, "motivo": "sem item elegível", "classe": TRANSITORIO}

    ultimo = None
    for entry in pool:
        candidate = entry if hasattr(entry, "kind") else None
        prod = candidate.obj if candidate else entry
        logger.debug(
            "Tentando enviar conteúdo id=%s origem=%s marketplace=%s",
            getattr(prod, "id", None), getattr(prod, "origem", "cupom"),
            getattr(prod, "marketplace", "?"),
        )
        if candidate and candidate.kind == "coupon":
            r = enviar_cupom(
                prod, grupo_id, canal=canal, usuario=usuario,
                configuracao=configuracao, destino_nome=destino_nome,
                score=candidate.score, motivos_score=candidate.reasons)
        else:
            if candidate:
                prod.score_oferta = candidate.score
                prod.motivos_score = candidate.reasons
            r = enviar_oferta_de_produto(
                prod, grupo_id, verificar=verificar, dry_run=dry_run, canal=canal,
                usuario=usuario, configuracao=configuracao, destino_nome=destino_nome)
        if r.get("sucesso"):
            return r
        logger.debug("Produto id=%s reprovado no envio: %s", getattr(prod, "id", None), r.get("motivo"))
        ultimo = r
        if r.get("precisa_login_ml"):
            # Sessão do ML caiu: os demais candidatos falhariam igual (cada tentativa
            # abre um browser e leva ~30s). Aborta e devolve o motivo real.
            return r
        if r.get("classe") == TRANSITORIO:
            # Mesma lógica do precisa_login_ml, para o outro lado do envio: o
            # WhatsApp caiu (ou o worker piscou) no meio do tick. Insistir nos 7
            # candidatos restantes custa ~30s de Playwright cada para colecionar
            # a mesma falha 8 vezes — e enchia o histórico de Publicacao 'falhou'.
            return r
    return ultimo or {"sucesso": False, "motivo": "nenhum candidato passou"}


def processar_configs_de_envio():
    """
    Percorre ConfiguracaoEnvio ativas; para cada uma vencida (now - ultimo_envio >=
    intervalo), seleciona 1 item do nicho e envia. Chamado pelo tick do Celery.
    Retorna lista de resultados por config.
    """
    from apps.scrapers import whatsapp_client
    from apps.scrapers.eventos import log_event
    from apps.scrapers.models import ConfiguracaoEnvio

    agora = timezone.now()
    hoje = timezone.localtime(agora).date()
    # Limites do dia LOCAL como datetimes aware. Com __date=hoje o Postgres aplicava
    # timezone(...)::date na coluna e o índice de data_envio/enviada_em virava enfeite;
    # com __range ele compara datetime com datetime e usa o índice.
    _inicio_hoje = timezone.make_aware(
        timezone.datetime.combine(hoje, timezone.datetime.min.time()),
        timezone.get_current_timezone())
    _hoje_range = (_inicio_hoje, _inicio_hoje + timedelta(days=1) - timedelta(microseconds=1))
    resultados = []
    # Cache por-owner dentro do tick: quantos envios já saíram hoje (cota diária).
    _envios_hoje: dict = {}
    # Mesmo padrão, para o estado da sessão WhatsApp: uma leitura por sessão por
    # tick, não uma por config.
    _wa_status: dict = {}

    def _wa_pronto(cfg) -> bool:
        """Dá para enviar pelo WhatsApp deste dono agora?

        Este gate é o que impede o pior efeito de uma sessão caída: sem ele,
        `selecionar_e_enviar` gasta ~30s de Playwright por candidato (8 deles)
        montando link de afiliado para só então descobrir, no POST, que não há
        WhatsApp do outro lado — por config, por tick, indefinidamente.

        Também religa a sessão 'inativo'. É o único estado em que POST
        /api/sessoes reconecta sem humano: o worker tem a credencial no volume
        mas ela não está no Map (restore pulado por capacidade no boot, ou
        runtime destruído depois). 'expirado' fica DE FORA de propósito — o Node
        só chega nele depois de purgar a credencial (session_policy.reconnectOutcome
        só devolve 'expire' com authPurges > 0), então revivê-lo aqui não
        reconecta ninguém: só fabrica um QR que ninguém está olhando e prende um
        dos 4 slots de Chromium. Quem precisa de QR abre o painel, e o painel já
        chama iniciar_sessao (views.whatsapp_painel).
        """
        sessao = wa_session_de(cfg.owner)
        if not sessao:
            return True   # pool legado sem dono: mantém o caminho de antes
        if sessao not in _wa_status:
            estado = whatsapp_client.status(sessao)
            if not estado.get("conectado") and estado.get("fase") == "inativo":
                whatsapp_client.iniciar_sessao(sessao)
                # Não relê o status: initializeSession é assíncrono no Node e
                # ainda não terminou. Este tick não envia; o próximo encontra a
                # sessão de pé. Reler aqui só somaria latência para o mesmo 'não'.
                logger.info("Sessão WhatsApp %s estava inativa; religada.", sessao)
            _wa_status[sessao] = estado
        return bool(_wa_status[sessao].get("conectado"))

    def _cota_estourada(owner) -> bool:
        """True se o dono está suspenso ou já bateu a cota diária de envios.
        owner=None (pool legado/compartilhado) não tem dono → sem cota/bloqueio."""
        if owner is None:
            return False
        perfil = getattr(owner, "perfil", None)
        if perfil and perfil.bloqueado:
            return True
        if owner.id not in _envios_hoje:
            _envios_hoje[owner.id] = Publicacao.objects.filter(
                usuario=owner, criada_em__range=_hoje_range,
                status__in=("pendente", "enviado", "incerto"),
            ).count()
        limite = perfil.cota_max_envios_dia() if perfil else 0
        return bool(limite) and _envios_hoje[owner.id] >= limite

    for cfg in ConfiguracaoEnvio.objects.filter(ativo=True).select_related("owner__perfil"):
        # 0. Dono suspenso ou cota diária estourada → nunca envia.
        if _cota_estourada(cfg.owner):
            continue
        # 1. Respeita a janela de horário (ex: 8h-20h). Fora dela, nunca envia.
        if not cfg.dentro_da_janela(agora):
            continue
        # 2. Vencido = sem agendamento ainda OU passou do proximo_envio (intervalo + jitter).
        vencido = cfg.proximo_envio is None or agora >= cfg.proximo_envio
        if not vencido:
            continue
        enviados_config_hoje = Publicacao.objects.filter(
            configuracao=cfg, status="enviado", enviada_em__range=_hoje_range).count()
        if cfg.max_envios_dia and enviados_config_hoje >= cfg.max_envios_dia:
            continue
        # 3. WhatsApp do dono fora do ar: não é falha da regra. Sai sem tocar em
        # falhas_consecutivas e sem reagendar — quando a sessão voltar, a config
        # continua vencida e envia no primeiro tick seguinte.
        if getattr(cfg, "canal", "whatsapp") == "whatsapp" and not _wa_pronto(cfg):
            logger.info(
                "Config %s pulada: WhatsApp do dono não está conectado.", cfg.id)
            continue

        macros = [cfg.macro_categoria] if cfg.macro_categoria else None  # vazio = qualquer (inclui ofertas)
        r = selecionar_e_enviar(
            macros, cfg.grupo_id,
            min_desconto_percent=cfg.min_desconto_percent,
            horas_cooldown=cfg.horas_cooldown,
            verificar=True,
            termo=cfg.termo_busca,
            canal=getattr(cfg, "canal", "whatsapp"),
            marketplace=getattr(cfg, "marketplace", "") or None,
            usuario=cfg.owner,
            configuracao=cfg,
            destino_nome=cfg.grupo_nome,
        )
        # Reagenda sempre (sucesso ou não) p/ não ficar martelando o mesmo tick;
        # jitter ±1-10min deixa o ritmo humano. ultimo_envio só em sucesso (display).
        cfg.agendar_proximo(agora)
        if r.get("sucesso"):
            cfg.ultimo_envio = agora
            cfg.falhas_consecutivas = 0
            cfg.motivo_pausa = ""
            if cfg.owner_id is not None:
                _envios_hoje[cfg.owner_id] = _envios_hoje.get(cfg.owner_id, 0) + 1
        elif r.get("classe") == TRANSITORIO:
            # Falha que some sozinha (worker piscou, timeout, 429, estoque vazio).
            # Não conta e não pausa: era exatamente isto que desligava a automação
            # de quem não tinha defeito nenhum na regra. Também não zera o
            # contador — uma falha permanente intercalada com blips transitórios
            # ainda precisa chegar ao teto.
            logger.info("Config %s: falha transitória ignorada (%s).",
                        cfg.id, r.get("motivo"))
        else:
            # 'permanente' e 'desconhecido' seguem contando. Pausar no 'desconhecido'
            # é o comportamento que já existia: na dúvida, para de martelar o grupo.
            cfg.falhas_consecutivas += 1
            if cfg.pausar_apos_falhas and cfg.falhas_consecutivas >= cfg.pausar_apos_falhas:
                cfg.ativo = False
                cfg.motivo_pausa = (r.get("motivo") or "Falhas consecutivas")[:255]
                # Nível error: a automação do usuário acabou de morrer e só volta
                # com ação humana. É a falha mais cara do produto (ele para de
                # receber ofertas e não é avisado), então precisa saltar no relatório.
                log_event(
                    "publicacao", "config_pausada",
                    f"Automação pausada após {cfg.falhas_consecutivas} falhas: {cfg.motivo_pausa}",
                    level="error", usuario=cfg.owner,
                    contexto={
                        "config_id": cfg.id,
                        "destino": cfg.grupo_nome or cfg.grupo_id,
                        "canal": getattr(cfg, "canal", "whatsapp"),
                        "falhas_consecutivas": cfg.falhas_consecutivas,
                        "motivo": cfg.motivo_pausa,
                    },
                )
        cfg.save(update_fields=[
            "proximo_envio", "ultimo_envio", "falhas_consecutivas",
            "motivo_pausa", "ativo"])
        resultados.append({"config": cfg.id, **r})
    return resultados
