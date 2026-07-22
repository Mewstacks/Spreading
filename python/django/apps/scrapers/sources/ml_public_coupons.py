"""Fonte de cupons a partir da página pública oficial de afiliados do Mercado Livre.

A página https://afiliadosmercadolivre.github.io/cupons-afiliadosmercadolivre/ é um
site estático (GitHub Pages) atualizado de hora em hora por um GitHub Action. Os
cupons NÃO vêm de uma API JSON: ficam embutidos no próprio HTML como um array
JavaScript `const COUPONS = [ ... ];`. Aqui a gente baixa a página, extrai esse
array e o projeta em IngestedItem(kind="coupon"), que o persistence.py grava em
CupomNormalizado — sem precisar de Playwright nem de login.

Formato de cada cupom (campos que a página emite):
    nome            -> código digitável do cupom (ex.: "PROMOCERTA")
    acao            -> categoria/ação (Sellers, Fashion, ...)
    dia_inicio      -> "dd/mm/aaaa"
    dia_fim         -> "dd/mm/aaaa" (validade)
    valor_desconto  -> texto exibido (ex.: "20%")
    min_compra      -> compra mínima em reais
    desconto_max    -> teto de desconto em reais
    container_url   -> lista de produtos a que o cupom se aplica
    container_name  -> slug do container
    is_mar_aberto   -> True = vale para o site todo (não só um container)
    days_left       -> dias restantes
    discount_num    -> desconto numérico (ex.: 20)
"""
import json
import re
from datetime import datetime

import requests
from django.conf import settings
from django.utils import timezone

from .base import IngestedItem, SourceAdapter
from apps.scrapers.coupon_rules import normalizar_regras_cupom

DEFAULT_URL = ("https://afiliadosmercadolivre.github.io/"
               "cupons-afiliadosmercadolivre/")
_HTTP_CACHE = {}


def _extrair_array_js(html, nome_var):
    """Extrai o array JS `const <nome_var> = [ ... ]` do HTML por contagem de
    colchetes (robusto contra `];` que apareça dentro de alguma string)."""
    marcador = re.search(rf"{nome_var}\s*=\s*\[", html)
    if not marcador:
        return []
    inicio = marcador.end() - 1  # aponta para o '[' de abertura
    profundidade = 0
    for i in range(inicio, len(html)):
        ch = html[i]
        if ch == "[":
            profundidade += 1
        elif ch == "]":
            profundidade -= 1
            if profundidade == 0:
                try:
                    return json.loads(html[inicio:i + 1])
                except json.JSONDecodeError:
                    return []
    return []


def _validade_fim_do_dia(dia_fim):
    """"dd/mm/aaaa" -> datetime aware no fim daquele dia (cupom vale o dia inteiro)."""
    if not dia_fim:
        return None
    try:
        d = datetime.strptime(dia_fim.strip(), "%d/%m/%Y")
    except (ValueError, AttributeError):
        return None
    return timezone.make_aware(d.replace(hour=23, minute=59, second=59))


class MLPublicCouponsSource(SourceAdapter):
    """Cupons ativos publicados na página oficial de afiliados do Mercado Livre."""
    slug = "ml-cupons-afiliados"
    marketplace = "mercadolivre"
    name = "Cupons de afiliados (Mercado Livre)"

    def _url(self):
        return getattr(settings, "ML_CUPONS_AFILIADOS_URL", "") or DEFAULT_URL

    def _cupons_brutos(self):
        url = self._url()
        cached = _HTTP_CACHE.get(url, {})
        headers = {"User-Agent": "Spreading/1.0 (+cupons)"}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("modified"):
            headers["If-Modified-Since"] = cached["modified"]
        resp = requests.get(url, timeout=20, headers=headers)
        if resp.status_code == 304:
            return cached.get("coupons", [])
        resp.raise_for_status()
        coupons = _extrair_array_js(resp.text, "COUPONS")
        _HTTP_CACHE[url] = {
            "etag": resp.headers.get("ETag", ""),
            "modified": resp.headers.get("Last-Modified", ""),
            "coupons": coupons,
        }
        return coupons

    def discover_coupons(self, **kwargs):
        agora = timezone.now()
        vistos = set()
        for c in self._cupons_brutos():
            nome = str(c.get("nome") or "").strip()
            if not nome:
                continue
            validade = _validade_fim_do_dia(c.get("dia_fim"))
            # A página só lista cupons com verba confirmada, mas um dia_fim já vencido
            # (fuso/atualização atrasada) não deve entrar como ativo.
            if validade and validade < agora:
                continue
            container_name = str(c.get("container_name") or "").strip()
            is_site = bool(c.get("is_mar_aberto"))
            # external_id estável por (código + container): o mesmo código pode existir
            # para containers diferentes; site-wide colapsa em um só.
            external_id = f"afiliados:{nome}:{'site' if is_site else container_name or 'geral'}"
            if external_id in vistos:
                continue
            vistos.add(external_id)

            valor = str(c.get("valor_desconto") or "").strip()
            acao = str(c.get("acao") or "").strip()
            escopo = "site inteiro" if is_site else (acao or "produtos selecionados")
            titulo = f"{nome} — {valor} OFF ({escopo})".strip()[:255]
            link = str(c.get("container_url") or "").strip()

            yield IngestedItem(
                external_id=external_id[:160],
                marketplace=self.marketplace,
                source=self.slug,
                kind="coupon",
                canonical_url=link[:1000],
                title=titulo,
                coupon_code=nome[:120],
                coupon_rules=normalizar_regras_cupom({
                    "tipo_desconto": "porcentagem",
                    "discount_num": c.get("discount_num"),
                    "valor_desconto": valor,
                    "min_compra": c.get("min_compra"),
                    "desconto_max": c.get("desconto_max"),
                    "acao": acao,
                    "container_url": link,
                    "container_name": container_name,
                    "is_mar_aberto": is_site,
                    "dia_inicio": c.get("dia_inicio"),
                    "dia_fim": c.get("dia_fim"),
                    "modo_resgate": "codigo",
                    "escopo": escopo,
                }, external_id=external_id, codigo=nome),
                restricted=not is_site,
                flash=bool(c.get("days_left") in (0, "0")),
                valid_until=validade,
                observed_at=agora,
                evidence={"fonte": "afiliados-github", "url": self._url()},
            )

    def healthcheck(self):
        try:
            return {"ok": bool(self._cupons_brutos())}
        except Exception as exc:  # noqa: BLE001 — healthcheck não deve propagar
            return {"ok": False, "erro": str(exc)}
