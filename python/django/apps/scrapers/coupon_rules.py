"""Normalizacao e exibicao segura das regras de cupons externos.

As fontes historicamente gravaram dois formatos diferentes no JSONField. Este
modulo e a fronteira unica: tudo que le ou grava regras passa por aqui e nunca
presume que um valor externo seja string.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


_CODIGO_HUMANO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,39}$")


def _texto(valor) -> str:
    return "" if valor is None else str(valor).strip()


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
    try:
        return float(texto)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:[.,]\d+)?", texto)
        return float(match.group().replace(",", ".")) if match else None


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


def codigo_publicavel(cupom) -> str:
    regras = regras_do_cupom(cupom)
    if regras["modo_resgate"] != "codigo":
        return ""
    return codigo_humano(getattr(cupom, "codigo", ""))


def formatar_numero(valor) -> str:
    numero = _numero(valor)
    if numero is None:
        return ""
    if numero.is_integer():
        return str(int(numero))
    return f"{numero:.2f}".rstrip("0").rstrip(".").replace(".", ",")

