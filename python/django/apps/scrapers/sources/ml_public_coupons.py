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
import hashlib
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings
from django.utils import timezone

from .base import IngestedItem, SourceAdapter
from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from apps.scrapers.source_diagnostics import capture_public_text_diagnostic

DEFAULT_URL = ("https://afiliadosmercadolivre.github.io/"
               "cupons-afiliadosmercadolivre/")
DEFAULT_GENERAL_DESTINATION = "https://www.mercadolivre.com.br/"
_HTTP_CACHE = {}


def _recortar_json_balanceado(html, inicio):
    """Recorta um array/objeto JSON sem contar delimitadores dentro de strings."""
    if inicio < 0 or inicio >= len(html) or html[inicio] not in "[{":
        return None
    abertura = html[inicio]
    fechamento = "]" if abertura == "[" else "}"
    profundidade = 0
    em_string = escape = False
    for i in range(inicio, len(html)):
        ch = html[i]
        if em_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                em_string = False
            continue
        if ch == '"':
            em_string = True
        elif ch == abertura:
            profundidade += 1
        elif ch == fechamento:
            profundidade -= 1
            if profundidade == 0:
                return html[inicio:i + 1]
    return None


def _extrair_array_js(html, nome_var):
    """Extrai o array JS `const <nome_var> = [ ... ]` do HTML por contagem de
    colchetes (robusto contra `];` que apareça dentro de alguma string)."""
    marcador = re.search(rf"{nome_var}\s*=\s*\[", html)
    if not marcador:
        return []
    bruto = _recortar_json_balanceado(html, marcador.end() - 1)
    try:
        return json.loads(bruto) if bruto else []
    except json.JSONDecodeError:
        return []


def _parece_lista_cupons(valor):
    if not isinstance(valor, list):
        return False
    if not valor:
        # Um array vazio qualquer (menus, banners, analytics) não comprova que o
        # catálogo de cupons está legitimamente vazio.
        return False
    amostra = [item for item in valor[:10] if isinstance(item, dict)]
    if not amostra:
        return False
    assinaturas = ({"nome", "dia_fim"}, {"codigo", "validade"},
                   {"coupon_code", "end_date"})
    return any(any(chaves <= set(item) for chaves in assinaturas) for item in amostra)


def _procurar_listas_cupons(valor):
    """Encontra listas pela assinatura dos itens, inclusive dentro de JSON SSR."""
    achados = []
    if isinstance(valor, list):
        if _parece_lista_cupons(valor):
            achados.append(valor)
        else:
            for item in valor:
                achados.extend(_procurar_listas_cupons(item))
    elif isinstance(valor, dict):
        for item in valor.values():
            achados.extend(_procurar_listas_cupons(item))
    return achados


def _extrair_cupons_html(html):
    """Extrai cupons sem depender do nome ``COUPONS``.

    A precedência é deliberada: contrato histórico conhecido, demais atribuições
    JavaScript e, por fim, scripts JSON/SSR. O retorno distingue schema ilegível de
    uma lista explicitamente vazia para que zero itens não pareça saúde por engano.
    """
    diagnostico = {"parser": "none", "schema_ok": False, "explicit_empty": False}
    tradicional = _extrair_array_js(html, "COUPONS")
    if tradicional:
        return tradicional, {**diagnostico, "parser": "named", "schema_ok": True}
    if re.search(r"\bCOUPONS\s*=\s*\[\s*\]", html or ""):
        return [], {**diagnostico, "parser": "named", "schema_ok": True,
                    "explicit_empty": True}

    candidatos = []
    for match in re.finditer(
            r"(?:\b(?:const|let|var)\s+)?(?P<name>[A-Za-z_$][\w$\.]*)\s*=\s*\[",
            html or ""):
        inicio = match.end() - 1
        bruto = _recortar_json_balanceado(html, inicio)
        if not bruto:
            continue
        try:
            valor = json.loads(bruto)
        except json.JSONDecodeError:
            continue
        if _parece_lista_cupons(valor):
            candidatos.append(valor)
        elif valor == [] and re.search(r"coupon|cupom", match.group("name"), re.I):
            return [], {
                **diagnostico, "parser": "schema-array", "schema_ok": True,
                "explicit_empty": True,
            }
    if candidatos:
        return max(candidatos, key=len), {
            **diagnostico, "parser": "schema-array", "schema_ok": True,
            "explicit_empty": not any(candidatos),
        }

    for match in re.finditer(
            r"<script[^>]+type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
            html or "", re.I | re.S):
        try:
            valor = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        listas = _procurar_listas_cupons(valor)
        if listas:
            return max(listas, key=len), {
                **diagnostico, "parser": "script-json", "schema_ok": True,
            }
    return [], diagnostico


def _validade_fim_do_dia(dia_fim):
    """"dd/mm/aaaa" -> datetime aware no fim daquele dia (cupom vale o dia inteiro)."""
    if not dia_fim:
        return None
    raw = str(dia_fim or "").strip()
    d = None
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(raw[:10], formato)
            break
        except ValueError:
            continue
    if d is None:
        return None
    return timezone.make_aware(d.replace(hour=23, minute=59, second=59))


def _inicio_do_dia(dia_inicio):
    if not dia_inicio:
        return None
    raw = str(dia_inicio or "").strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return timezone.make_aware(datetime.strptime(raw[:10], formato))
        except ValueError:
            continue
    return None


def _campo(cupom, *nomes, default=None):
    for nome in nomes:
        valor = cupom.get(nome)
        if valor not in (None, ""):
            return valor
    return default


def _boolean(valor):
    if isinstance(valor, str):
        return valor.strip().casefold() in {"1", "true", "sim", "yes"}
    return bool(valor)


def _safe_source_url(url):
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _safe_ml_container_url(url):
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower()
    allowed = (
        hostname == "meli.la" or hostname.endswith(".meli.la")
        or hostname == "mercadolivre.com.br"
        or hostname.endswith(".mercadolivre.com.br")
        or hostname == "mercadolivre.com"
        or hostname.endswith(".mercadolivre.com")
    )
    if parsed.scheme not in {"http", "https"} or not allowed:
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


class MLPublicCouponsSource(SourceAdapter):
    """Cupons ativos publicados na página oficial de afiliados do Mercado Livre."""
    slug = "ml-cupons-afiliados"
    marketplace = "mercadolivre"
    name = "Cupons de afiliados (Mercado Livre)"

    def __init__(self):
        self.last_metrics = {}
        self.last_health = "unknown"

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
            self.last_metrics = dict(cached.get("metrics") or {})
            self.last_health = cached.get("health", "unknown")
            return cached.get("coupons", [])
        resp.raise_for_status()
        coupons, diagnostico = _extrair_cupons_html(resp.text)
        schema_ok = bool(diagnostico["schema_ok"])
        explicit_empty = bool(diagnostico["explicit_empty"])
        schema_signature = "|".join([
            str(diagnostico.get("parser") or "none"),
            *sorted({
                str(key) for coupon in coupons[:20] if isinstance(coupon, dict)
                for key in coupon.keys()
            }),
        ])
        fingerprint = hashlib.sha256(
            schema_signature.encode("utf-8")
        ).hexdigest()
        self.last_health = (
            "healthy_empty" if schema_ok and explicit_empty
            else "healthy" if coupons and schema_ok
            else "degraded"
        )
        if self.last_health == "degraded":
            capture_public_text_diagnostic(
                resp.text, self.slug, "coupon_schema_unrecognized",
            )
        self.last_metrics = {
            "items_seen": len(coupons),
            "parser": diagnostico["parser"],
            "schema_ok": schema_ok,
            "explicit_empty": explicit_empty,
            # O payload oficial é um inventário, não uma página truncada. Zero só
            # é completo quando a própria fonte publica um vazio explícito.
            "complete": bool(schema_ok and (coupons or explicit_empty)),
            "schema_fingerprint": fingerprint,
        }
        _HTTP_CACHE[url] = {
            "etag": resp.headers.get("ETag", ""),
            "modified": resp.headers.get("Last-Modified", ""),
            "coupons": coupons,
            "metrics": self.last_metrics,
            "health": self.last_health,
        }
        return coupons

    def discover_coupons(self, **kwargs):
        agora = timezone.now()
        vistos = set()
        aceitos = duplicados = rejeitados = 0
        rejeicoes = {}
        for c in self._cupons_brutos():
            nome = str(_campo(c, "nome", "codigo", "coupon_code", default="") or "").strip()
            if not nome:
                rejeitados += 1
                rejeicoes["missing_code"] = rejeicoes.get("missing_code", 0) + 1
                continue
            dia_inicio = _campo(c, "dia_inicio", "inicio", "start_date", default="")
            dia_fim = _campo(c, "dia_fim", "validade", "end_date", default="")
            inicio = _inicio_do_dia(dia_inicio)
            validade = _validade_fim_do_dia(dia_fim)
            if validade is None:
                rejeitados += 1
                rejeicoes["invalid_end_date"] = rejeicoes.get("invalid_end_date", 0) + 1
                continue
            # A página só lista cupons com verba confirmada, mas um dia_fim já vencido
            # (fuso/atualização atrasada) não deve entrar como ativo.
            if validade and validade < agora:
                rejeitados += 1
                rejeicoes["expired"] = rejeicoes.get("expired", 0) + 1
                continue
            container_name = str(_campo(
                c, "container_name", "container", "scope_id", default="",
            ) or "").strip()
            is_site = _boolean(_campo(
                c, "is_mar_aberto", "site_wide", default=False,
            ))
            # external_id estável por (código + escopo + vigência): o mesmo código
            # pode ser reutilizado em campanhas/períodos ou containers diferentes.
            normalized_code = nome.upper()
            period_key = f"{dia_inicio or 'unknown'}:{dia_fim}"
            scope_key = "site" if is_site else container_name or "geral"
            identity_digest = hashlib.sha256(
                f"{normalized_code}\0{scope_key}\0{period_key}".encode("utf-8")
            ).hexdigest()[:16]
            external_id = (
                f"afiliados:{normalized_code[:70]}:{scope_key[:40]}:"
                f"{identity_digest}"
            )
            if external_id in vistos:
                duplicados += 1
                continue
            vistos.add(external_id)
            aceitos += 1

            valor = str(_campo(
                c, "valor_desconto", "desconto", "discount", default="",
            ) or "").strip()
            acao = str(_campo(c, "acao", "scope", "categoria", default="") or "").strip()
            escopo = "site inteiro" if is_site else (acao or "produtos selecionados")
            titulo = f"{nome} — {valor} OFF ({escopo})".strip()[:255]
            raw_link = str(_campo(
                c, "container_url", "url", "scope_url", default="",
            ) or "").strip()
            # Um código digitável é aplicado no checkout e não precisa carregar
            # ``coupon_campaign_id``. A fonte oficial publica vários códigos por
            # categoria com ``-``/id de categoria no destino; descartá-los por não
            # serem containers completos eliminava promoções válidas. Mantemos o
            # escopo textual e usamos uma landing pública allowlisted, cuja versão
            # afiliada ainda precisa passar pelo verificador por usuário.
            link = _safe_ml_container_url(raw_link)
            if not link:
                fallback = getattr(
                    settings, "ML_GENERAL_COUPON_DESTINATION", "",
                ) or DEFAULT_GENERAL_DESTINATION
                link = fallback if str(fallback).startswith(
                    "https://www.mercadolivre.com.br/"
                ) else DEFAULT_GENERAL_DESTINATION

            regras = normalizar_regras_cupom({
                "tipo_desconto": "porcentagem",
                "discount_num": _campo(c, "discount_num", "discount_value"),
                "valor_desconto": valor,
                "min_compra": _campo(c, "min_compra", "minimum_purchase"),
                "desconto_max": _campo(c, "desconto_max", "max_discount"),
                "acao": acao,
                "container_url": _safe_ml_container_url(raw_link),
                "container_name": container_name,
                "is_mar_aberto": is_site,
                "dia_inicio": dia_inicio,
                "dia_fim": dia_fim,
                "modo_resgate": "codigo",
                "escopo": escopo,
            }, external_id=external_id, codigo=nome)
            if regras.get("valor_desconto") in (None, "", 0):
                rejeitados += 1
                aceitos -= 1
                vistos.discard(external_id)
                rejeicoes["invalid_discount"] = rejeicoes.get("invalid_discount", 0) + 1
                continue

            yield IngestedItem(
                external_id=external_id[:160],
                marketplace=self.marketplace,
                source=self.slug,
                kind="coupon",
                canonical_url=link[:1000],
                title=titulo,
                coupon_code=normalized_code[:120],
                coupon_rules=regras,
                restricted=tem_restricao_publico(
                    " ".join(str(_campo(c, key, default="") or "")
                             for key in ("acao", "descricao", "condicoes", "title"))
                ),
                flash=bool(_campo(c, "days_left", "dias_restantes") in (0, "0")),
                starts_at=inicio,
                valid_until=validade,
                observed_at=agora,
                evidence={"fonte": "afiliados-github",
                          "url": _safe_source_url(self._url())},
            )
        self.last_metrics.update({
            "accepted": aceitos,
            "duplicates": duplicados,
            "rejected": rejeitados,
            "rejections": rejeicoes,
        })

    def healthcheck(self):
        try:
            self._cupons_brutos()
            return {"ok": self.last_health in {"healthy", "healthy_empty"},
                    "health": self.last_health, "metrics": self.last_metrics}
        except Exception as exc:  # noqa: BLE001 — healthcheck não deve propagar
            return {"ok": False, "erro": "Falha temporária na fonte pública.",
                    "cause": type(exc).__name__}
