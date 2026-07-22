"""Cliente Awin por usuario: conexao, programas, ofertas e Link Builder."""

from __future__ import annotations

import re
import hashlib
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlparse

import requests
from django.utils import timezone

from apps.scrapers.coupon_rules import normalizar_regras_cupom
from apps.scrapers.sources.base import IngestedItem


API_BASE = "https://api.awin.com"
RESTRICTION_WORDS = (
    "selected", "selecionad", "new customer", "novo cliente", "first purchase",
    "primeira compra", "app only", "somente no app", "cartão", "cartao", "pix",
)


class AwinError(RuntimeError):
    def __init__(self, public_message, *, status_code=0, retry_after=None):
        super().__init__(public_message)
        self.public_message = public_message
        self.status_code = status_code
        self.retry_after = retry_after


def _headers(token):
    return {"Authorization": f"Bearer {str(token).strip()}", "Accept": "application/json",
            "User-Agent": "Spreading/1.0 (+awin-publisher)"}


def _request(method, path, token, **kwargs):
    from django.core.cache import cache
    token_fingerprint = hashlib.sha256(str(token).encode()).hexdigest()[:20]
    rate_key = f"awin-rate:{token_fingerprint}"
    if cache.add(rate_key, 1, timeout=60):
        call_count = 1
    else:
        try:
            call_count = cache.incr(rate_key)
        except ValueError:
            cache.set(rate_key, 1, timeout=60)
            call_count = 1
    # Reserva duas chamadas para recuperacao/operacoes manuais e nunca bloqueia o
    # worker com sleep. O scheduler tenta de novo na janela seguinte.
    if call_count > 18:
        raise AwinError("Limite temporário da Awin atingido; a sincronização será repetida.",
                        status_code=429, retry_after=60)
    try:
        response = requests.request(
            method, f"{API_BASE}{path}", headers=_headers(token), timeout=20, **kwargs)
    except requests.RequestException as exc:
        raise AwinError("A Awin demorou demais para responder. Tente novamente.") from exc
    if response.status_code in (401, 403):
        raise AwinError("Token Awin inválido ou sem permissão. Gere um novo token.",
                        status_code=response.status_code)
    if response.status_code == 429:
        try:
            retry_after = max(60, int(response.headers.get("Retry-After") or 60))
        except ValueError:
            retry_after = 60
        raise AwinError("Limite temporário da Awin atingido; a sincronização será repetida.",
                        status_code=429, retry_after=retry_after)
    if response.status_code >= 400:
        raise AwinError("A Awin recusou temporariamente a sincronização.",
                        status_code=response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise AwinError("A Awin devolveu uma resposta inválida.") from exc


def listar_contas(token):
    payload = _request("GET", "/publishers", token)
    rows = payload if isinstance(payload, list) else payload.get("publishers", [])
    contas = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        contas.append({"id": str(row["id"]), "nome": str(row.get("name") or row["id"])[:160]})
    if not contas:
        raise AwinError("Nenhuma conta Publisher foi encontrada para este token.")
    return contas


def _domain(value):
    raw = str(value or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower()


def sincronizar_programas(integracao):
    from apps.scrapers.models import ProgramaAfiliado

    publisher_id = str(integracao.identificador_conta or "").strip()
    if not publisher_id or not integracao.token:
        raise AwinError("Selecione uma conta Publisher antes de sincronizar.")
    payload = _request(
        "GET", f"/publishers/{publisher_id}/programmes", integracao.token,
        params={"relationship": "joined", "countryCode": "BR"},
    )
    rows = payload if isinstance(payload, list) else payload.get("programmes", [])
    encontrados = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        external_id = str(row["id"])
        encontrados.add(external_id)
        valid_domains = []
        for value in row.get("validDomains") or []:
            value = value.get("domain") if isinstance(value, dict) else value
            domain = _domain(value)
            if domain and domain not in valid_domains:
                valid_domains.append(domain)
        defaults = {
            "nome": str(row.get("name") or external_id)[:180],
            "dominio": _domain(row.get("displayUrl")),
            "dominios_validos": valid_domains,
            "logo_url": str(row.get("logoUrl") or "")[:1000],
            "status_vinculo": "joined",
            "link_status": str(row.get("linkStatus") or "online").lower()[:30],
        }
        ProgramaAfiliado.objects.update_or_create(
            integracao=integracao, external_id=external_id, defaults=defaults)
    ProgramaAfiliado.objects.filter(integracao=integracao).exclude(
        external_id__in=encontrados).update(status_vinculo="indisponivel", link_status="offline")
    return ProgramaAfiliado.objects.filter(
        integracao=integracao, status_vinculo="joined").count()


def sincronizar_comissoes(integracao, limit=5):
    """Atualiza poucos programas por ciclo para ficar abaixo de 20 chamadas/minuto."""
    from django.db.models import F, Q
    from apps.scrapers.models import ProgramaAfiliado

    cutoff = timezone.now() - timedelta(days=1)
    programs = ProgramaAfiliado.objects.filter(
        integracao=integracao, habilitado=True, status_vinculo="joined",
    ).filter(Q(comissao_sincronizada_em__isnull=True)
             | Q(comissao_sincronizada_em__lt=cutoff)).order_by(
        F("comissao_sincronizada_em").asc(nulls_first=True))[:limit]
    updated = 0
    for program in programs:
        payload = _request(
            "GET", f"/publishers/{integracao.identificador_conta}/programmedetails",
            integracao.token,
            params={"advertiserId": program.external_id, "relationship": "joined"},
        )
        ranges = payload.get("commissionRange", []) if isinstance(payload, dict) else []
        info = payload.get("programmeInfo", {}) if isinstance(payload, dict) else {}
        if ranges:
            values_min = [float(row["min"]) for row in ranges if row.get("min") is not None]
            values_max = [float(row["max"]) for row in ranges if row.get("max") is not None]
            program.comissao_min = min(values_min) if values_min else None
            program.comissao_max = max(values_max) if values_max else None
            program.comissao_tipo = str(ranges[0].get("type") or "")[:20]
        if info:
            program.deeplink_habilitado = bool(info.get("deeplinkEnabled", True))
            program.link_status = str(info.get("linkStatus") or program.link_status)[:30].lower()
            valid_domains = [_domain(row.get("domain") if isinstance(row, dict) else row)
                             for row in info.get("validDomains") or []]
            valid_domains = [domain for domain in valid_domains if domain]
            if valid_domains:
                program.dominios_validos = list(dict.fromkeys(valid_domains))
        program.comissao_sincronizada_em = timezone.now()
        program.save(update_fields=[
            "comissao_min", "comissao_max", "comissao_tipo", "deeplink_habilitado",
            "link_status", "dominios_validos", "comissao_sincronizada_em",
        ])
        updated += 1
    return updated


def _dt(value, end_of_day=False):
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if end_of_day and len(text) <= 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone=dt_timezone.utc)
    return parsed


def _discount(text):
    text = str(text or "")
    percent = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", text)
    fixed = re.search(r"R\$\s*(\d+(?:[.,]\d+)?)", text, re.I)
    minimum = re.search(r"(?:acima de|a partir de|mínim[oa])\s*R\$?\s*(\d+(?:[.,]\d+)?)",
                        text, re.I)
    if percent:
        kind, value = "porcentagem", percent.group(1)
    elif fixed:
        kind, value = "fixo", fixed.group(1)
    else:
        kind, value = "", None
    return kind, value, minimum.group(1) if minimum else None


def _offer_rows(payload):
    if isinstance(payload, list):
        return payload, False
    for key in ("promotions", "offers", "items", "data"):
        if isinstance(payload.get(key), list):
            rows = payload[key]
            pagination = payload.get("pagination") or {}
            total_pages = pagination.get("totalPages") or payload.get("totalPages")
            current = pagination.get("page") or payload.get("page") or 1
            return rows, bool(total_pages and int(current) < int(total_pages))
    return [], False


def coletar_ofertas(integracao):
    from apps.scrapers.models import ProgramaAfiliado

    enabled = {str(p.external_id): p for p in ProgramaAfiliado.objects.filter(
        integracao=integracao, habilitado=True, status_vinculo="joined",
        link_status="online")}
    if not enabled:
        return []
    page = 1
    result = []
    now = timezone.now()
    while True:
        payload = _request(
            "POST", f"/publisher/{integracao.identificador_conta}/promotions",
            integracao.token,
            json={
                "filters": {"advertiserIds": [int(x) for x in enabled],
                            "membership": "joined", "regionCodes": ["BR"],
                            "status": "active", "type": "all"},
                "pagination": {"page": page, "pageSize": 200},
            },
        )
        rows, has_next = _offer_rows(payload)
        for row in rows:
            if not isinstance(row, dict):
                continue
            advertiser = row.get("advertiser") or {}
            advertiser_id = str(advertiser.get("id") or "")
            if advertiser_id not in enabled:
                continue
            content_type = str(row.get("type") or "promotion").lower()
            voucher = row.get("voucher") or {}
            code = str(voucher.get("code") or "").strip()
            if content_type == "voucher" and not code:
                continue
            tracking_url = str(row.get("urlTracking") or "").strip()
            if not _domain(tracking_url):
                continue
            title = str(row.get("title") or row.get("description") or "Promoção")[:255]
            terms = str(row.get("terms") or "")
            description = str(row.get("description") or "")
            searchable = " ".join((title, description, terms)).lower()
            start = _dt(row.get("startDate"))
            end = _dt(row.get("endDate"), end_of_day=True)
            kind, discount, minimum = _discount(searchable)
            restricted = any(word in searchable for word in RESTRICTION_WORDS)
            flash = bool(
                (start and end and end - start <= timedelta(days=1))
                or (end and now <= end <= now + timedelta(hours=6)))
            external_id = f"awin:{integracao.identificador_conta}:{row.get('promotionId')}"
            rules = normalizar_regras_cupom({
                "tipo_desconto": kind, "valor_desconto": discount,
                "valor_minimo": minimum,
                "modo_resgate": "codigo" if content_type == "voucher" else "ativacao",
                "escopo": description, "dia_inicio": row.get("startDate"),
                "dia_fim": row.get("endDate"),
            }, external_id=external_id, codigo=code)
            result.append(IngestedItem(
                external_id=external_id[:160], marketplace="awin", source="awin-offers-api",
                kind="coupon", canonical_url=tracking_url[:1000], title=title,
                coupon_code=code[:120], coupon_rules=rules, content_type=content_type,
                starts_at=start, valid_until=end, restricted=restricted, flash=flash,
                observed_at=now,
                evidence={"transport": "awin-offers-api", "advertiser_id": advertiser_id,
                          "advertiser_name": str(advertiser.get("name") or enabled[advertiser_id].nome),
                          "exclusive": bool(voucher.get("exclusive")),
                          "attributable": bool(voucher.get("attributable")),
                          "terms": terms[:3000]},
            ))
        if not has_next or not rows:
            break
        page += 1
    return result


def gerar_deeplink(integracao, programa, destination_url):
    payload = _request(
        "POST", f"/publishers/{integracao.identificador_conta}/linkbuilder/generate",
        integracao.token,
        json={"advertiserId": int(programa.external_id),
              "destinationUrl": destination_url, "shorten": False},
    )
    url = str(payload.get("url") or "").strip() if isinstance(payload, dict) else ""
    if not _domain(url):
        raise AwinError("A Awin não conseguiu gerar um link para esta página.")
    return url


def url_permitida(programa, url):
    host = _domain(url)
    allowed = [str(x).lower() for x in (programa.dominios_validos or []) if x]
    if programa.dominio:
        allowed.append(programa.dominio.lower())
    return bool(host and any(host == domain or host.endswith(f".{domain}") for domain in allowed))


def sincronizar_integracao(integracao, *, forcar_programas=False):
    """Sincroniza uma conta isoladamente, preservando dados em falha externa."""
    from django.core.cache import cache
    from apps.scrapers.models import CupomNormalizado, FonteIngestao
    from apps.scrapers.sources.persistence import persist_items

    lock = f"awin-sync:{integracao.pk}"
    if not cache.add(lock, "1", timeout=14 * 60):
        return {"status": "running", "coupons": 0}
    now = timezone.now()
    integracao.ultima_tentativa = now
    integracao.save(update_fields=["ultima_tentativa"])
    try:
        if (forcar_programas or not integracao.programas_sincronizados_em
                or integracao.programas_sincronizados_em < now - timedelta(days=1)):
            sincronizar_programas(integracao)
            integracao.programas_sincronizados_em = now
        items = coletar_ofertas(integracao)
        persist_items(items, owner=integracao.owner, integration=integracao)
        source, _ = FonteIngestao.objects.get_or_create(
            slug="awin-offers-api",
            defaults={"marketplace": "awin", "nome": "Awin — cupons e promoções"},
        )
        source.status = "ok"
        source.ultima_tentativa = now
        source.ultimo_sucesso = now
        source.ultimo_total = len(items)
        source.erro_publico = ""
        source.falhas_consecutivas = 0
        source.save(update_fields=["status", "ultima_tentativa", "ultimo_sucesso",
                                   "ultimo_total", "erro_publico", "falhas_consecutivas"])
        active_ids = [item.external_id for item in items]
        CupomNormalizado.objects.filter(
            owner=integracao.owner, integracao=integracao, fonte=source,
        ).exclude(external_id__in=active_ids).update(estado="expirado")
        commission_retry_after = 0
        try:
            sincronizar_comissoes(integracao)
        except AwinError as commission_error:
            # Comissão é apenas desempate; nunca invalida um catálogo já sincronizado.
            if commission_error.status_code == 429:
                commission_retry_after = commission_error.retry_after or 60
            else:
                raise
        integracao.status = "conectada"
        integracao.ultimo_sucesso = now
        integracao.proxima_sincronizacao = now + timedelta(
            seconds=max(15 * 60, commission_retry_after))
        integracao.erro_publico = ""
        integracao.falhas_consecutivas = 0
        integracao.save(update_fields=[
            "status", "ultimo_sucesso", "ultima_tentativa", "proxima_sincronizacao",
            "programas_sincronizados_em", "erro_publico", "falhas_consecutivas",
        ])
        return {"status": "ok", "coupons": len(items)}
    except AwinError as exc:
        integracao.falhas_consecutivas += 1
        integracao.erro_publico = exc.public_message[:255]
        if exc.status_code in (401, 403):
            integracao.status = "reconectar"
            integracao.proxima_sincronizacao = None
        else:
            integracao.status = "degradada"
            delay = exc.retry_after or min(3600, 60 * (2 ** min(integracao.falhas_consecutivas, 5)))
            integracao.proxima_sincronizacao = now + timedelta(seconds=delay)
        integracao.save(update_fields=[
            "status", "ultima_tentativa", "proxima_sincronizacao", "erro_publico",
            "falhas_consecutivas",
        ])
        raise
    finally:
        cache.delete(lock)
