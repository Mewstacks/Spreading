"""Normalizacao e exibicao segura das regras de cupons externos.

As fontes historicamente gravaram dois formatos diferentes no JSONField. Este
modulo e a fronteira unica: tudo que le ou grava regras passa por aqui e nunca
presume que um valor externo seja string.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from django.db.models import Q
from django.conf import settings


_CODIGO_HUMANO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,39}$")
_ESCOPO_GENERICO = {
    "", "geral", "site inteiro", "todo o site", "toda a loja", "todos os produtos",
    "qualquer produto", "todas as categorias",
}
_CONDICAO_PUBLICO = re.compile(
    r"\b(?:usu[aá]rios? selecionad|novos? clientes?|primeira compra|somente no app|"
    r"apenas no app|cart[aã]o|pix)\b", re.I,
)
_NAO_PRODUTO = re.compile(
    r"^(?:compras?|pedidos?|pagamentos?)\b|^(?:acima|a partir)\s+de\s+R\$", re.I,
)


def _texto(valor) -> str:
    return "" if valor is None else str(valor).strip()


# "1.199", "2.000", "10.000": ponto como separador de milhar. O padrão exige
# grupos de exatamente 3 dígitos, então "129.90" (decimal no formato do Google
# Feed) e "1.5" não casam e continuam sendo lidos como decimal.
_MILHAR = re.compile(r"^\d{1,3}(?:\.\d{3})+$")


def _numero(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = _texto(valor).replace("R$", "").replace("%", "").replace("\xa0", " ")
    texto = texto.replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif _MILHAR.fullmatch(texto):
        # Sem esta linha, "compra mínima R$ 1.199" virava R$ 1,20 e o cupom passava
        # a valer para qualquer item — o mesmo defeito que havia no parser do ML.
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except (TypeError, ValueError):
        match = re.search(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?", texto)
        return _numero(match.group()) if match else None


# Fronteira pública do parser de dinheiro. Toda fonte que lê valor em texto deve
# usar isto, em vez de um `float()`/regex próprio — foi a divergência entre esses
# parsers caseiros que produziu compra mínima e preço errados.
numero_br = _numero


def tem_restricao_publico(texto) -> bool:
    """True quando o texto declara restrição de público/pagamento.

    "primeira compra", "somente no app", "cartão", "pix" e afins mudam quem pode
    usar o cupom. Quem publica precisa avisar; até aqui a expressão só era usada
    para LIMPAR o rótulo de escopo, e a condição sumia da mensagem.
    """
    return bool(_CONDICAO_PUBLICO.search(_texto(texto)))


def codigo_humano(valor) -> str:
    codigo = _texto(valor)
    return codigo if _CODIGO_HUMANO.fullmatch(codigo) else ""


def normalizar_regras_cupom(regras, *, external_id="", codigo="") -> dict:
    raw = dict(regras) if isinstance(regras, Mapping) else {}
    valor_bruto = raw.get("valor_desconto")
    tipo = _texto(raw.get("tipo_desconto")).lower()
    if tipo == "percentual":
        tipo = "porcentagem"
    if tipo not in {"porcentagem", "fixo"}:
        texto_valor = _texto(valor_bruto)
        if "%" in texto_valor or raw.get("discount_num") not in (None, ""):
            tipo = "porcentagem"
        elif "R$" in texto_valor:
            tipo = "fixo"
        else:
            tipo = ""

    valor = _numero(raw.get("discount_num"))
    if valor is None:
        valor = _numero(valor_bruto)
    minimo = _numero(raw.get("valor_minimo"))
    if minimo is None:
        minimo = _numero(raw.get("min_compra"))
    maximo = _numero(raw.get("desconto_maximo"))
    if maximo is None:
        maximo = _numero(raw.get("desconto_max"))

    modo = _texto(raw.get("modo_resgate")).lower()
    if modo not in {"codigo", "ativacao"}:
        if _texto(external_id).startswith("campanha:"):
            modo = "ativacao"
        else:
            modo = "codigo" if codigo_humano(codigo) else "ativacao"

    return {
        "tipo_desconto": tipo,
        "valor_desconto": valor,
        "valor_minimo": minimo,
        "desconto_maximo": maximo,
        "modo_resgate": modo,
        "escopo": _texto(raw.get("escopo") or raw.get("acao")),
        "container_url": _texto(raw.get("container_url")),
        "container_name": _texto(raw.get("container_name")),
        "is_mar_aberto": bool(raw.get("is_mar_aberto")),
        "dia_inicio": _texto(raw.get("dia_inicio")),
        "dia_fim": _texto(raw.get("dia_fim")),
    }


def regras_do_cupom(cupom) -> dict:
    regras = normalizar_regras_cupom(
        getattr(cupom, "regras", None),
        external_id=getattr(cupom, "external_id", ""),
        codigo=getattr(cupom, "codigo", ""),
    )
    modo_tipado = str(getattr(cupom, "redemption_mode", "") or "").lower()
    if modo_tipado in {"code", "activation"}:
        regras["modo_resgate"] = "codigo" if modo_tipado == "code" else "ativacao"
    return regras


def classificar_contrato_cupom(*, regras, external_id="", codigo="", evidencia=None,
                               categoria="", owner=None, data_scope="public") -> dict:
    """Materializa o contrato tipado mantendo compatibilidade com ``regras``.

    Fontes e importadores usam esta função única; consumidores legados ainda
    podem ler o JSON até o fim da migração.
    """
    normalizadas = normalizar_regras_cupom(
        regras, external_id=external_id, codigo=codigo,
    )
    redemption_mode = (
        "code" if normalizadas["modo_resgate"] == "codigo" else "activation"
    )
    evidence = evidencia if isinstance(evidencia, Mapping) else {}
    if normalizadas.get("is_mar_aberto"):
        scope_type = "sitewide"
    elif normalizadas.get("container_url") or normalizadas.get("container_name"):
        scope_type = "container"
    elif evidence.get("asins") or evidence.get("product_ids"):
        scope_type = "product"
    elif normalizadas.get("escopo") or categoria:
        scope_type = "category"
    else:
        scope_type = "sitewide" if redemption_mode == "code" else "product"
    audience_scope = (
        "organization" if owner is not None or data_scope == "organization" else "public"
    )
    return {
        "redemption_mode": redemption_mode,
        "scope_type": scope_type,
        "audience_scope": audience_scope,
    }


def cupons_visiveis_q(usuario, *, prefix=""):
    """Predicado único de isolamento do catálogo de cupons por audiência.

    ``owner`` continua cobrindo importações privadas históricas. Cupons coletados
    em área autenticada, porém, pertencem à organização e não podem escapar pelo
    antigo atalho ``owner IS NULL``.
    """
    def campo(nome):
        return f"{prefix}{nome}"
    visiveis = Q(**{campo("owner"): usuario}) | Q(
        **{campo("owner__isnull"): True, campo("audience_scope"): "public"}
    )
    if usuario is not None:
        from apps.accounts.models import organization_for_user

        organization = organization_for_user(usuario)
        if organization is not None:
            visiveis |= Q(**{
                campo("owner__isnull"): True,
                campo("audience_scope"): "organization",
                campo("organization"): organization,
            })
    return visiveis


def coupon_mode_enabled(cupom, *, use_mode=None) -> bool:
    """Kill switch independente por loja e modo, sem apagar o catálogo."""
    marketplace = str(getattr(cupom, "marketplace", "") or "").lower()
    mode = use_mode or (
        "code_notice" if regras_do_cupom(cupom)["modo_resgate"] == "codigo"
        else "product_activation"
    )
    setting_name = {
        ("mercadolivre", "code_notice"): "ML_COUPON_CODES_ENABLED",
        ("mercadolivre", "product_activation"): "ML_CUPONS_ATIVACAO_ENABLED",
        ("amazon", "code_notice"): "AMAZON_COUPON_CODES_ENABLED",
        ("amazon", "product_activation"): "AMAZON_COUPON_ACTIVATION_ENABLED",
    }.get((marketplace, mode))
    return True if not setting_name else bool(getattr(settings, setting_name, True))


def extrair_escopo_produtos(titulo, escopo="") -> str:
    """Retorna marca/categoria/produtos contemplados sem inventar informação.

    A fonte de campanhas do ML costuma colocar o alvo somente no título, por
    exemplo ``R$ 50 OFF em monitores Samsung selecionados``. Condições de público
    ou pagamento ficam de fora daqui e continuam sendo exibidas como condição.
    """
    explicito = _texto(escopo).strip(" .:-")
    normalizado = explicito.casefold()
    if (normalizado not in _ESCOPO_GENERICO and explicito
            and not _CONDICAO_PUBLICO.search(explicito)):
        return explicito[:220]

    texto = _texto(titulo).strip()
    if not texto:
        return ""
    # O trecho após "em"/"para" é o sinal mais confiável presente no título
    # oficial. Evita capturar "em compras acima de...", que é compra mínima.
    matches = list(re.finditer(r"\b(?:em|para)\s+(.+)$", texto, re.I))
    if matches:
        candidato = matches[-1].group(1).strip(" .:-")
        if (candidato and not _NAO_PRODUTO.search(candidato)
                and not _CONDICAO_PUBLICO.search(candidato)
                and candidato.casefold() not in _ESCOPO_GENERICO):
            return candidato[:220]

    # Algumas campanhas omitem a preposição, mas declaram explicitamente que são
    # produtos/itens selecionados. Remove apenas o prefixo comercial do desconto.
    if re.search(r"\b(?:produtos?|itens?)?\s*selecionad[oa]s?\b", texto, re.I):
        candidato = re.sub(
            r"^(?:cupom\s+)?(?:R\$\s*[\d.,]+|[\d.,]+\s*%)\s*"
            r"(?:off|de\s+desconto)?\s*", "", texto, flags=re.I,
        ).strip(" .:-")
        if candidato and not _CONDICAO_PUBLICO.search(candidato):
            return candidato[:220]
    return ""


def escopo_produtos_cupom(cupom) -> str:
    regras = regras_do_cupom(cupom)
    return extrair_escopo_produtos(
        getattr(cupom, "titulo", ""), regras.get("escopo", ""))


def codigo_publicavel(cupom) -> str:
    regras = regras_do_cupom(cupom)
    if regras["modo_resgate"] != "codigo":
        return ""
    return codigo_humano(getattr(cupom, "codigo", ""))


_FONTES_ML_ATIVACAO = (
    "mercadolivre-web", "mercadolivre-campanhas", "ml-cupons-afiliados",
)

# Subdomínio de LISTAGEM do ML. Por construção toda URL aqui é uma lista de anúncios
# filtrada (`/_Container_<id>`, `/_CustId_<id>?coupon_campaign_id=<id>`) — pública e
# reproduzível por qualquer pessoa. `www.mercadolivre.com.br/cupons`, que é o link de
# fallback da projeção de campanhas, fica de fora de propósito: é a vitrine genérica e
# não prova escopo nenhum.
_HOST_LISTAGEM_ML = "lista.mercadolivre.com.br"


def listagem_publica_ml(cupom) -> str:
    """URL pública que delimita o escopo do cupom ML, ou '' quando não há.

    Preferência para `regras.container_url`, que é onde o scraper do carrossel grava
    o container. A projeção de campanhas (`scraper.projetar_catalogo_cupons`) não
    conhece o container e grava só `link` — mas para 2062 dos 2073 cupons de campanha
    em produção esse `link` É a listagem pública da campanha. Exigir apenas
    `container_url` descartava 98% do catálogo publicável do ML sem ganho de
    segurança: `coupon_products._coletar_ml_remoto` já aceitava `cupom.link` como
    listagem para preparar o mesmo cupom.

    O host é conferido com `urlsplit` e comparação exata — `in`/`endswith` numa
    string crua aceitaria `lista.mercadolivre.com.br.evil.com`.
    """
    from urllib.parse import urlsplit

    regras = regras_do_cupom(cupom)
    for candidato in (regras.get("container_url"), getattr(cupom, "link", "")):
        url = _texto(candidato)
        if not url:
            continue
        try:
            partes = urlsplit(url)
        except ValueError:
            continue
        if partes.scheme not in ("http", "https"):
            continue
        if (partes.hostname or "").casefold().rstrip(".") == _HOST_LISTAGEM_ML:
            return url
    return ""


def escopo_delimitado(cupom) -> bool:
    """O cupom recorta um conjunto de produtos que dá para verificar?

    Existe porque "cupom com código publicável" não é a mesma coisa que "cupom que
    vale para este item". Um cupom de CÓDIGO passava direto: `codigo_publicavel`
    olha só o formato do código, e nenhum portão adiante perguntava a que produtos
    o desconto se aplica. Foi assim que o MELIPROMO — 25% em ``Vehicle Parts &
    Accessories``, segundo a própria página oficial de afiliados — saiu anunciado
    num tablet e num jogo de panelas.

    Delimita o escopo quem tem:

    * ``is_mar_aberto`` — vale para o site inteiro, então qualquer item serve;
    * uma listagem pública (``listagem_publica_ml``/``container_url``), que é o
      conjunto de produtos participantes e pode ser aberta por qualquer pessoa;
    * ids explícitos de produto na evidência (ASIN, item id, product id).

    NÃO delimita: um código lido da vitrine sem nenhuma lista associada. Para
    esses, `cupom.link` é uma página genérica de loja (``/ofertas/cupons``, a home)
    e tratá-la como "a lista do cupom" fabrica associação — ver
    `coupon_products._coletar_ml_remoto`.
    """
    regras = regras_do_cupom(cupom)
    if regras.get("is_mar_aberto"):
        return True
    if _texto(regras.get("container_url")):
        return True
    evidencia = getattr(cupom, "evidencia", None)
    if isinstance(evidencia, Mapping):
        for chave in ("product_ids", "asins", "item_ids"):
            if evidencia.get(chave):
                return True
    marketplace = str(getattr(cupom, "marketplace", "") or "").casefold()
    if marketplace == "mercadolivre":
        return bool(listagem_publica_ml(cupom))
    return False


# EvidenceStrength — quão forte é a prova de que este cupom delimita um conjunto
# real de produtos. Sem o conceito, uma URL que o próprio sistema INVENTOU pesava
# o mesmo que um container que a fonte publicou, e candidatos fracos ocupavam o
# preparo e o único slot de Chromium antes das campanhas comprovadas.
EVIDENCIA_CONTAINER = "official_container"     # container observado na fonte
EVIDENCIA_ESTRUTURADA = "structured_listing"   # loja/categoria fornecida pela campanha
EVIDENCIA_SINTETICA = "synthetic_candidate"    # URL construída como hipótese
EVIDENCIA_AUSENTE = ""

# Ordem de atendimento: menor number = primeiro. Códigos oficiais não passam por
# aqui (não dependem de container); entre as ativações, esta é a fila.
FORCA_EVIDENCIA_ORDEM = {
    EVIDENCIA_CONTAINER: 0,
    EVIDENCIA_ESTRUTURADA: 1,
    EVIDENCIA_SINTETICA: 2,
    EVIDENCIA_AUSENTE: 3,
}


def _campanha_do_cupom(cupom) -> str:
    external = str(getattr(cupom, "external_id", "") or "")
    return external.split(":", 1)[1] if external.startswith("campanha:") else ""


def forca_evidencia(cupom) -> str:
    """Classifica a prova de escopo de um cupom de ativação do Mercado Livre.

    O scraper de campanhas monta `link_produtos` por vários caminhos e todos
    terminavam indistinguíveis no banco:

      - `action.value` da própria campanha e `container.name` da segmentação são
        dados que a FONTE publicou -> `official_container`;
      - loja (`/loja/<slug>/`, `_CustId_<id>`) e categoria vêm da segmentação
        estruturada da campanha -> `structured_listing`;
      - `_Container_<campanha_id>`, o último `else` do scraper, é um palpite nosso
        quando a campanha não disse nada -> `synthetic_candidate`.

    Só as duas primeiras classes merecem consumir preparo e geração de link antes
    de qualquer confirmação; a terceira precisa provar produto primeiro.
    """
    regras = regras_do_cupom(cupom)
    if regras.get("container_url"):
        return EVIDENCIA_CONTAINER
    listagem = listagem_publica_ml(cupom)
    if not listagem:
        return EVIDENCIA_AUSENTE
    campanha = _campanha_do_cupom(cupom)
    if campanha and listagem.rstrip("/").casefold().endswith(
            f"/_container_{campanha}".casefold()):
        # O fallback que o scraper usa quando a campanha não trouxe segmentação
        # nenhuma: a URL é o id da campanha, não um container observado.
        return EVIDENCIA_SINTETICA
    if "/_container_" in listagem.casefold():
        return EVIDENCIA_CONTAINER
    return EVIDENCIA_ESTRUTURADA


def evidencia_confirmada(cupom) -> bool:
    """True quando a hipótese já foi comprovada por produto associado.

    É o portão que promove um `synthetic_candidate`: uma vez que exista
    ProdutoCupom confirmado, a URL deixou de ser palpite — ela rendeu item real.
    """
    from apps.scrapers.models import ProdutoCupom

    return ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado",
    ).exists()


def _ativacao_ml_publicavel(cupom, regras, usuario=None) -> bool:
    """Cupom de ATIVAÇÃO do Mercado Livre (clique, não código digitável).

    O ML migrou a página /ofertas/cupons para cupons ativados por clique: o campo
    `code` do payload é um token opaco de sessão, não algo que o comprador digita.
    Por isso a ingestão grava `codigo=""`, e sem este ramo TODO cupom ML de campanha
    ficava inpublicável — em homologação, 2357 de 2379.

    O que difere do token de sessão (que continua fora): a `container_url` do
    carrossel é PÚBLICA (`lista.mercadolivre.com.br/_Container_<id>`), reproduzível
    por qualquer pessoa. É ela que a mensagem divulga.

    A regra aqui é ESTRUTURAL de propósito — nada de consultar ProdutoCupom. Duas
    razões: `preparar_cupom` devolve "vazio" ANTES de criar qualquer ProdutoCupom
    quando o cupom não é publicável, então exigir a relação aqui criaria um
    deadlock em que ela nunca passaria a existir; e esta função é chamada por cupom
    em três laços (views, preparar_lote, content_ranking), onde uma query por
    chamada é o N+1 que já custa caro.

    A prova de associação continua exigida a jusante, em três portões intactos:
    `_base_produtos` (produto tem de bater campanha), `relacoes_preparadas_para_envio`
    (ProdutoCupom confirmado com preços) e `relacoes_prontas_para_envio`
    (LinkAfiliadoUsuario verificado).
    """
    from apps.accounts.feature_flags import enabled_for_user

    ator = usuario or getattr(cupom, "owner", None)
    if not enabled_for_user("ML_CUPONS_ATIVACAO_ENABLED", ator):
        return False
    if getattr(getattr(cupom, "fonte", None), "slug", "") not in _FONTES_ML_ATIVACAO:
        return False
    # Só campanha identificada: é o id que amarra cupom, container e produto.
    if not str(getattr(cupom, "external_id", "") or "").startswith("campanha:"):
        return False
    # Site-wide nunca entra por aqui: sem escopo não há como provar que o desconto
    # se aplica ao item divulgado.
    if regras.get("is_mar_aberto"):
        return False
    if not listagem_publica_ml(cupom):
        return False
    # Sem valor de desconto não há promessa a fazer na mensagem.
    return regras.get("valor_desconto") not in (None, "", 0)


def ativacao_publicavel(cupom, usuario=None) -> bool:
    """Aceita ativação quando a loja prova promoção + produtos.

    Amazon: a página oficial de cupons fornece identificador da promoção, ASINs e
    preço final depois da ativação.
    Mercado Livre: campanha com container público — ver `_ativacao_ml_publicavel`,
    atrás da flag ML_CUPONS_ATIVACAO_ENABLED.
    """
    regras = regras_do_cupom(cupom)
    if regras["modo_resgate"] != "ativacao":
        return False
    marketplace = str(getattr(cupom, "marketplace", "") or "").casefold()
    if marketplace == "mercadolivre":
        return _ativacao_ml_publicavel(cupom, regras, usuario=usuario)
    if marketplace != "amazon":
        return False
    evidence = getattr(cupom, "evidencia", {}) or {}
    source = getattr(getattr(cupom, "fonte", None), "slug", "")
    return bool(
        source == "amazon-public-coupons"
        and evidence.get("association") == "amazon-official-coupon-page"
        and evidence.get("promotion_id")
        and evidence.get("asins")
    )


def cupom_publicavel(cupom, usuario=None) -> bool:
    return bool(codigo_publicavel(cupom) or ativacao_publicavel(cupom, usuario=usuario))


def formatar_numero(valor) -> str:
    numero = _numero(valor)
    if numero is None:
        return ""
    if numero.is_integer():
        return str(int(numero))
    return f"{numero:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def derivar_categoria_cupom(titulo, regras) -> str:
    """Categoria para o filtro da aba Cupons, nunca vazia.

    Precedência: (1) escopo/acao vindo da fonte oficial; (2) classificacao por
    palavra-chave do titulo; (3) faixa de desconto. Cupons de campanha do ML
    gravam escopo vazio, entao sem isto o dropdown de categoria fica vazio em
    producao (era o sintoma relatado).
    """
    raw = regras if isinstance(regras, Mapping) else {}
    escopo = _texto(raw.get("escopo") or raw.get("acao"))
    if escopo:
        return escopo[:100]

    try:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome
        macro = classificar_oferta_por_nome(titulo or "")
        if macro:
            return macro[:100]
    except Exception:
        pass

    valor = _numero(raw.get("valor_desconto") or raw.get("discount_num"))
    tipo = _texto(raw.get("tipo_desconto")).lower()
    if tipo in ("porcentagem", "percentual") and valor is not None:
        return "Até 20%" if valor <= 20 else "Acima de 20%"
    if tipo == "fixo" and valor is not None:
        return "Desconto em reais"
    return "Geral"


def rotulo_anunciante(titulo="", regras=None, categoria_fallback="") -> str:
    """'Sobre o que é o cupom' p/ a coluna Loja e o filtro por anunciante na aba Cupons.

    Cupom de campanha do ML não guarda anunciante; o sinal confiável é o escopo do
    título oficial (marca/produto contemplado, ex.: 'monitores Samsung'). Quando o
    título é genérico ('Cupom Mercado Livre'), cai na `categoria_fallback` — a
    categoria dominante dos produtos cobertos, o que a cliente realmente quer saber.
    Awin e cupons manuais já gravam o anunciante real; a projeção só chama isto p/
    quem fica vazio. Retorna '' quando nada é identificável (o template mantém o
    nome da loja como fallback).
    """
    escopo = ""
    if isinstance(regras, Mapping):
        escopo = _texto(regras.get("escopo") or regras.get("acao"))
    sobre = extrair_escopo_produtos(titulo, escopo).strip(" .:-")
    if sobre:
        return sobre[:100]
    return (categoria_fallback or "").strip()[:100]


def score_cupom(cupom, usuario=None) -> float:
    """Ranking de qualidade de um cupom p/ ordenar a aba Cupons (maior = melhor).

    Combina codigo publicavel (peso alto), valor do desconto, validade futura e
    confianca. A recencia fica como desempate no `order_by`, nao aqui.
    """
    from django.utils import timezone
    regras = regras_do_cupom(cupom)
    score = 0.0
    if codigo_publicavel(cupom):
        score += 50.0
    elif ativacao_publicavel(cupom, usuario=usuario):
        score += 25.0
    valor = _numero(regras.get("valor_desconto"))
    if valor is not None:
        if regras.get("tipo_desconto") == "porcentagem":
            score += min(valor, 60.0)
        else:  # desconto fixo em R$
            score += min(valor / 2.0, 40.0)
    validade = getattr(cupom, "validade", None)
    if validade and validade >= timezone.now():
        score += 10.0
    confianca = getattr(cupom, "confianca", "")
    score += {"alta": 15.0, "media": 5.0}.get(confianca, 0.0)
    return round(score, 2)
