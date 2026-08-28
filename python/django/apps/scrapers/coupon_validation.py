"""Contrato comum para validar cupons no carrinho sem efetuar uma compra.

Adaptadores de marketplace podem automatizar a preparação do carrinho e a ação
"aplicar cupom", mas entregam o resultado a este módulo antes de qualquer etapa de
pagamento. A redução monetária é recalculada aqui; texto ou badge da página não é
suficiente para produzir um veredito aceito.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import organization_for_user

from .models import CupomValidacao


ACCEPTED_TTL_HOURS = 6
REJECTED_TTL_MINUTES = 30
INCONCLUSIVE_RETRY_MINUTES = {
    "browser_busy": 5,
    "challenge": 30,
    "browser_error": 30,
    "session_expired": 360,
    "session_required": 360,
    "cart_not_empty": 360,
    "cart_layout_unknown": 60,
    "add_to_cart_control_missing": 60,
    "coupon_control_missing": 60,
    "coupon_apply_control_missing": 60,
}
TERMINAL_REJECTIONS = frozenset({
    "invalid_code", "expired", "usage_exhausted", "promotion_ended",
})
_CATEGORY_HINTS = (
    (("LIVRO", "LEIA", "KINDLE"), ("livro", "ebook", "kindle", "leitura")),
    (("DECOR",), ("decor", "moveis", "movel", "cama", "mesa", "banho")),
    (("CASA",), ("casa", "lar", "moveis", "movel", "decor")),
    (("OBRA", "CONSTRO", "TOOLS", "FERRAMENT"),
     ("construcao", "ferrament", "furadeira", "parafusadeira", "serra", "obra")),
    (("PNEU",), ("pneu",)),
    (("AUTO",), ("automot", "carro", "moto", "pneu")),
    (("TV",), ("tv", "televisor", "eletron")),
    (("CELULAR",), ("celular", "smartphone", "telefonia")),
    (("TECH", "ELETRON"), ("eletron", "informatica", "tecnologia")),
    (("PET",), ("pet", "cachorro", "gato", "racao")),
    (("MODA", "ROUPA", "TENIS"), ("moda", "roupa", "tenis", "calcado")),
    (("BELEZA", "MAKE", "COSMET"), ("beleza", "maquiagem", "cosmet", "perfume")),
    (("ESPORTE", "FITNESS", "TREINO"), ("esporte", "fitness", "academia", "treino")),
    (("BRINQUED",), ("brinqued",)),
    (("BEBE",), ("bebe", "infantil")),
)


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _fold(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def target_terms(cupom):
    rules = cupom.regras if isinstance(cupom.regras, dict) else {}
    identity = _fold(" ".join((
        str(cupom.codigo or ""), str(cupom.categoria or ""),
        str(rules.get("container_name") or ""), str(rules.get("escopo") or ""),
    ))).upper()
    terms = set()
    for triggers, candidates in _CATEGORY_HINTS:
        if any(trigger in identity for trigger in triggers):
            terms.update(candidates)
    return tuple(sorted(terms))


def target_matches_coupon(cupom, product):
    terms = target_terms(cupom)
    if not terms:
        return True

    def matches(value):
        tokens = set(re.findall(r"[a-z0-9]+", _fold(value)))
        for term in terms:
            if term in tokens or f"{term}s" in tokens:
                return True
            # Radicais explícitos (eletron/automot/brinqued...) precisam casar o
            # começo de uma palavra, nunca um trecho arbitrário como ebook/notebook.
            if len(term) >= 6 and any(token.startswith(term) for token in tokens):
                return True
        return False

    structured = " ".join((
        str(product.categoria or ""), str(product.macro_categoria or ""),
    ))
    meaningful = {
        token for token in re.findall(r"[a-z0-9]+", _fold(structured))
        if token not in {"desconhecido", "sem", "categoria"}
    }
    if meaningful:
        return matches(structured)
    return matches(" ".join((str(product.nome or ""), str(product.nome_norm or ""))))


def cart_fingerprint(cupom, *, product_key="", cart_context=None):
    context = cart_context if isinstance(cart_context, dict) else {}
    stable = "|".join((
        str(cupom.marketplace or "").casefold(),
        str(cupom.codigo or "").strip().upper(),
        str(product_key or "").strip(),
        str(context.get("quantity") or 1),
        str(context.get("seller") or ""),
        str(context.get("payment_method") or ""),
    ))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def agendar_validacao(cupom, usuario, *, product_key="", product_url="",
                      cart_context=None):
    """Cria uma tentativa idempotente; não abre rede nem altera carrinho."""
    organization = organization_for_user(usuario)
    if organization is None:
        raise ValueError("Usuário sem organização ativa.")
    fingerprint = cart_fingerprint(
        cupom, product_key=product_key, cart_context=cart_context,
    )
    validation, created = CupomValidacao.objects.get_or_create(
        usuario=usuario, cupom=cupom, cart_fingerprint=fingerprint,
        defaults={
            "organization": organization,
            "marketplace": str(cupom.marketplace or "").casefold(),
            "product_key": str(product_key or "")[:160],
            "product_url": str(product_url or "")[:1500],
            "evidence": {"cart_context": cart_context or {}},
            "no_purchase": True,
        },
    )
    return validation, created


def registrar_resultado(validation, *, status, reason_code="", safe_detail="",
                        subtotal_before=None, subtotal_after=None, evidence=None):
    """Persiste um veredito conservador e recalcula a economia observada.

    `accepted` só sobrevive se os dois subtotais forem monetários e o posterior for
    estritamente menor. Sinais de compra ou de confirmação de pedido invalidam a
    tentativa inteira: o validador não tem autorização para chegar a essa etapa.
    """
    if status not in {"accepted", "rejected", "inconclusive"}:
        raise ValueError("Status final de validação inválido.")
    evidence = dict(evidence or {})
    before = _decimal(subtotal_before)
    after = _decimal(subtotal_after)
    purchase_signal = any(bool(evidence.get(key)) for key in (
        "purchase_completed", "order_created", "payment_submitted",
    ))
    if purchase_signal:
        status = "inconclusive"
        reason_code = "purchase_boundary_breached"
        safe_detail = "A automação ultrapassou o limite seguro; resultado descartado."
        before = after = None
    discount = before - after if before is not None and after is not None else None
    if status == "accepted" and (discount is None or discount <= 0):
        status = "inconclusive"
        reason_code = "discount_not_observed"
        safe_detail = "O subtotal não comprovou redução após aplicar o código."
        discount = None

    now = timezone.now()
    retry_at = None
    if status == "inconclusive":
        retry_at = now + timezone.timedelta(
            minutes=INCONCLUSIVE_RETRY_MINUTES.get(reason_code, 15),
        )
    elif status == "rejected" and reason_code not in TERMINAL_REJECTIONS:
        retry_at = now + timezone.timedelta(minutes=30)
    with transaction.atomic():
        locked = CupomValidacao.objects.select_for_update().get(pk=validation.pk)
        locked.status = status
        locked.reason_code = str(reason_code or "")[:64]
        locked.safe_detail = str(safe_detail or "")[:255]
        locked.subtotal_before = before
        locked.subtotal_after = after
        locked.discount_amount = discount if discount and discount > 0 else None
        # Preserva o alvo/fingerprint do agendamento e acrescenta somente a
        # observação do checkout. Substituir o JSON apagava justamente qual produto
        # havia sido testado, tornando uma aceitação impossível de auditar depois.
        merged_evidence = dict(locked.evidence or {})
        original_cart_context = merged_evidence.get("cart_context")
        merged_evidence.update(evidence)
        if original_cart_context is not None:
            merged_evidence["cart_context"] = original_cart_context
        locked.evidence = merged_evidence
        locked.no_purchase = True
        locked.attempts += 1
        locked.verified_at = now
        locked.retry_at = retry_at
        locked.save(update_fields=(
            "status", "reason_code", "safe_detail", "subtotal_before",
            "subtotal_after", "discount_amount", "evidence", "no_purchase",
            "attempts", "verified_at", "retry_at", "updated_at",
        ))
    return locked


def validacoes_recentes_por_codigo(usuario, cupons):
    """Último veredito utilizável por (marketplace, código), em uma consulta."""
    cupons = list(cupons)
    ids = [cupom.pk for cupom in cupons if getattr(cupom, "codigo", "")]
    if not ids:
        return {}
    now = timezone.now()
    rows = CupomValidacao.objects.filter(
        usuario=usuario, cupom_id__in=ids,
        status__in=("accepted", "rejected"), verified_at__isnull=False,
    ).select_related("cupom").order_by("-verified_at", "-pk")
    result = {}
    for row in rows:
        age = now - row.verified_at
        if row.status == "accepted":
            fresh = age <= timezone.timedelta(hours=ACCEPTED_TTL_HOURS)
        else:
            fresh = age <= timezone.timedelta(minutes=REJECTED_TTL_MINUTES)
        if not fresh:
            continue
        key = (
            str(row.marketplace or row.cupom.marketplace or "").casefold(),
            str(row.cupom.codigo or "").strip().upper(),
        )
        result.setdefault(key, row)
    return result


def veredito_para_cupom(cupom, validations):
    key = (
        str(cupom.marketplace or "").casefold(),
        str(cupom.codigo or "").strip().upper(),
    )
    row = (validations or {}).get(key)
    if row is None:
        return "", ""
    if row.status == "accepted" and row.discount_amount and row.discount_amount > 0:
        return "accepted", "checkout_discount_observed"
    if row.status == "rejected" and row.reason_code in TERMINAL_REJECTIONS:
        return "rejected", row.reason_code
    return "", ""


def agendar_lote_validacao(usuario, *, limite=30, alvos_por_cupom=1,
                            channel="whatsapp"):
    """Materializa carrinhos candidatos para claims comunitários ainda retidos."""
    from apps.scrapers.coupon_rules import regras_do_cupom
    from apps.scrapers.maintenance import produtos_frescos_q
    from apps.scrapers.models import CupomDisponibilidade, Produto

    availabilities = list(CupomDisponibilidade.objects.filter(
        usuario=usuario, channel=channel, stage="collected",
        reason_code="community_uncorroborated", cupom__codigo__gt="",
        cupom__estado="ativo",
    ).select_related("cupom").order_by(
        "-cupom__relampago", "-cupom__ultima_observacao", "cupom_id",
    )[:max(limite * 3, limite)])
    marketplaces = {row.cupom.marketplace for row in availabilities}
    products = list(Produto.objects.filter(
        produtos_frescos_q(), marketplace__in=marketplaces,
        link_produto__gt="",
    ).filter(
        Q(owner__isnull=True) | Q(owner=usuario),
    ).exclude(
        estado__in=("indisponivel", "invalido", "expirado", "stale"),
    ).order_by("-ultima_observacao")[:2000])
    by_marketplace = {}
    for product in products:
        by_marketplace.setdefault(product.marketplace, []).append(product)

    # Fecha alvos evidentemente incompatíveis antes que um executor gaste sessão e
    # Chromium neles. São hipóteses geradas por nós, não observações de checkout.
    pending = list(CupomValidacao.objects.filter(
        usuario=usuario, status="pending",
    ).select_related("cupom"))
    product_ids = {
        (row.evidence.get("cart_context") or {}).get("product_id")
        for row in pending if isinstance(row.evidence, dict)
    }
    products_by_id = {
        product.pk: product for product in Produto.objects.filter(
            pk__in={pk for pk in product_ids if pk},
        )
    }
    invalid_pending = []
    for row in pending:
        product_id = (row.evidence.get("cart_context") or {}).get("product_id")
        product = products_by_id.get(product_id)
        if product is not None and not target_matches_coupon(row.cupom, product):
            row.status = "inconclusive"
            row.reason_code = "target_scope_mismatch"
            row.safe_detail = "Produto-alvo incompatível com a categoria indicada pelo cupom."
            row.verified_at = timezone.now()
            row.retry_at = None
            invalid_pending.append(row)
    if invalid_pending:
        CupomValidacao.objects.bulk_update(
            invalid_pending,
            ("status", "reason_code", "safe_detail", "verified_at", "retry_at"),
            batch_size=200,
        )

    scheduled = reused = no_target = 0
    now = timezone.now()
    for availability in availabilities:
        if scheduled >= limite:
            break
        coupon = availability.cupom
        rules = regras_do_cupom(coupon)
        minimum = _decimal(rules.get("valor_minimo")) or Decimal("0")
        candidates = []
        for product in by_marketplace.get(coupon.marketplace, []):
            price = _decimal(product.preco_efetivo or product.preco_com_cupom)
            if price is None or price <= 0 or price < minimum:
                continue
            if not target_matches_coupon(coupon, product):
                continue
            candidates.append(product)
            if len(candidates) >= max(1, alvos_por_cupom):
                break
        if not candidates:
            no_target += 1
            continue
        for product in candidates:
            if scheduled >= limite:
                break
            key = product.asin or f"produto:{product.pk}"
            validation, created = agendar_validacao(
                coupon, usuario, product_key=key,
                product_url=product.link_produto,
                cart_context={
                    "quantity": 1,
                    "catalog_price": str(product.preco_efetivo or product.preco_com_cupom),
                    "product_id": product.pk,
                },
            )
            if not created:
                if validation.status == "accepted":
                    reused += 1
                    continue
                if validation.retry_at and validation.retry_at > now:
                    reused += 1
                    continue
                validation.status = "pending"
                validation.reason_code = ""
                validation.safe_detail = ""
                validation.retry_at = None
                validation.save(update_fields=(
                    "status", "reason_code", "safe_detail", "retry_at", "updated_at",
                ))
                reused += 1
            scheduled += 1
    return {
        "scheduled": scheduled,
        "reused": reused,
        "without_product_target": no_target,
        "candidates_seen": len(availabilities),
        "invalid_targets_closed": len(invalid_pending),
    }
