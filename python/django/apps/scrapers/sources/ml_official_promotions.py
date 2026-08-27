"""Códigos publicados na landing oficial de promoções do Mercado Livre."""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.html import strip_tags

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


DEFAULT_URL = "https://www.mercadolivre.com.br/l/promocoes"
_TIMEOUT = (5, 20)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
_SECAO = re.compile(
    r"\bCupom\s+(?P<codigo>[A-Z0-9][A-Z0-9._-]{3,29})\s+Cupom\s+"
    r"(?:v\S{0,3}lido\s+de|dispon\S{0,4}vel\s+apenas\s+em)\s+",
    re.I,
)
_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{2,4})\b")
_PERCENTUAL = re.compile(r"Desconto\s+de\s+at\S{0,2}\s+(\d{1,2})\s*%", re.I)
_FIXO = re.compile(r"Desconto\s+de\s+at\S{0,2}\s+R\$\s*([\d.,]+)", re.I)
_MINIMO = re.compile(r"compra\s+a\s+partir\s+de\s+R\$\s*([\d.,]+)", re.I)
_MAXIMO = re.compile(r"desconto\s+m\S{0,2}ximo\s+de\s+R\$\s*([\d.,]+)", re.I)


def _texto(html):
    # `strip_tags` sozinho cola fronteiras (`</h3><p>` -> `CODIGOCupom`) e o
    # contrato some. Espaçar tags preserva a separação sem confiar no layout.
    bruto = re.sub(r"<[^>]+>", " ", html_lib.unescape(str(html or "")))
    limpo = strip_tags(bruto)
    limpo = limpo.replace("\\n", " ").replace("\\r", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", limpo).strip()


def _data(valor):
    raw = str(valor or "")
    for formato in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, formato).date()
        except ValueError:
            continue
    return None


def extrair_cupons_promocoes(html, *, agora=None):
    texto = _texto(html)
    agora = agora or timezone.now()
    hoje = timezone.localdate(agora)
    marcadores = list(_SECAO.finditer(texto))
    itens = []
    vistos = set()
    for indice, marcador in enumerate(marcadores):
        codigo = marcador.group("codigo").upper()
        if codigo in vistos:
            continue
        fim = marcadores[indice + 1].start() if indice + 1 < len(marcadores) else len(texto)
        trecho = texto[marcador.end():min(fim, marcador.end() + 1800)]
        datas = [_data(raw) for raw in _DATA.findall(trecho[:500])]
        datas = [dia for dia in datas if dia is not None]
        if not datas:
            continue
        inicio = datas[0]
        validade = datas[1] if len(datas) > 1 else datas[0]
        if validade < hoje:
            continue
        percentual = _PERCENTUAL.search(trecho)
        fixo = _FIXO.search(trecho)
        if percentual:
            tipo, valor = "porcentagem", float(percentual.group(1))
        elif fixo:
            tipo, valor = "fixo", normalizar_dinheiro(fixo.group(1))
        else:
            continue
        if valor <= 0 or (tipo == "porcentagem" and valor >= 100):
            continue
        minimo = _MINIMO.search(trecho)
        maximo = _MAXIMO.search(trecho)
        vistos.add(codigo)
        itens.append({
            "codigo": codigo,
            "tipo": tipo,
            "valor": valor,
            "minimo": normalizar_dinheiro(minimo.group(1)) if minimo else 0.0,
            "maximo": normalizar_dinheiro(maximo.group(1)) if maximo else 0.0,
            "inicio": inicio,
            "validade": validade,
            "trecho": trecho[:500],
        })
    return itens, {"sections_seen": len(marcadores), "accepted": len(itens)}


class MLOfficialPromotionsSource(SourceAdapter):
    slug = "ml-official-promotions"
    marketplace = "mercadolivre"
    name = "Mercado Livre — landing oficial de promoções"
    requires_chromium = False
    # A landing mantém regulamentos históricos; nunca representa inventário total.
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def discover_offers(self, **kwargs):
        return []

    def discover_coupons(self, **kwargs):
        url = getattr(settings, "ML_OFFICIAL_PROMOTIONS_URL", "") or DEFAULT_URL
        resposta = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        resposta.raise_for_status()
        agora = timezone.now()
        rows, metrics = extrair_cupons_promocoes(resposta.text, agora=agora)
        self.last_metrics = {**metrics, "complete": False}
        self.last_health_status = (
            "healthy" if rows else "healthy_empty" if metrics["sections_seen"] else "degraded"
        )
        for row in rows:
            inicio = timezone.make_aware(datetime.combine(row["inicio"], datetime.min.time()))
            validade = timezone.make_aware(
                datetime.combine(row["validade"], datetime.max.time())
            )
            regras = normalizar_regras_cupom({
                "tipo_desconto": row["tipo"],
                "valor_desconto": row["valor"],
                "valor_minimo": row["minimo"],
                "desconto_maximo": row["maximo"],
                "modo_resgate": "codigo",
                "escopo": "itens elegíveis na promoção oficial",
            }, external_id=f"ml-oficial:{row['codigo']}:{row['validade']}",
               codigo=row["codigo"])
            yield IngestedItem(
                external_id=f"ml-oficial:{row['codigo']}:{row['validade']}"[:160],
                marketplace=self.marketplace, source=self.slug, kind="coupon",
                canonical_url=url, title=f"Cupom {row['codigo']} — {row['valor']:g}"
                f"{'%' if row['tipo'] == 'porcentagem' else ' reais'} OFF"[:255],
                coupon_code=row["codigo"], coupon_rules=regras,
                content_type="voucher", starts_at=inicio, valid_until=validade,
                restricted=tem_restricao_publico(row["trecho"]),
                flash=(validade - agora).total_seconds() <= 24 * 3600,
                observed_at=agora,
                evidence={"transport": "mercadolivre-official-landing", "url": url},
            )

    def healthcheck(self):
        return {"ok": self.last_health_status in {"healthy", "healthy_empty"},
                "health": self.last_health_status, "metrics": self.last_metrics}
