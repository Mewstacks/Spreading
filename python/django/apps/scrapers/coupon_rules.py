"""Normalizacao e exibicao segura das regras de cupons externos.

As fontes historicamente gravaram dois formatos diferentes no JSONField. Este
modulo e a fronteira unica: tudo que le ou grava regras passa por aqui e nunca
presume que um valor externo seja string.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


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
    return normalizar_regras_cupom(
        getattr(cupom, "regras", None),
        external_id=getattr(cupom, "external_id", ""),
        codigo=getattr(cupom, "codigo", ""),
    )


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


_FONTES_ML_ATIVACAO = ("mercadolivre-web", "ml-cupons-afiliados")


def _ativacao_ml_publicavel(cupom, regras) -> bool:
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

    if not enabled_for_user("ML_CUPONS_ATIVACAO_ENABLED", getattr(cupom, "owner", None)):
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
    if not regras.get("container_url"):
        return False
    # Sem valor de desconto não há promessa a fazer na mensagem.
    return regras.get("valor_desconto") not in (None, "", 0)


def ativacao_publicavel(cupom) -> bool:
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
        return _ativacao_ml_publicavel(cupom, regras)
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


def cupom_publicavel(cupom) -> bool:
    return bool(codigo_publicavel(cupom) or ativacao_publicavel(cupom))


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


def score_cupom(cupom) -> float:
    """Ranking de qualidade de um cupom p/ ordenar a aba Cupons (maior = melhor).

    Combina codigo publicavel (peso alto), valor do desconto, validade futura e
    confianca. A recencia fica como desempate no `order_by`, nao aqui.
    """
    from django.utils import timezone
    regras = regras_do_cupom(cupom)
    score = 0.0
    if codigo_publicavel(cupom):
        score += 50.0
    elif ativacao_publicavel(cupom):
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
