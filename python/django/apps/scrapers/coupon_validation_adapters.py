"""Adaptadores conservadores de validação de cupom no carrinho.

O adaptador não visita checkout/pagamento e nunca clica em ações de compra. Para
evitar atribuir ao produto-alvo um desconto causado por outro item, ele só trabalha
com um carrinho inicialmente vazio. Texto de sucesso não basta: a aceitação exige
queda do total monetário, que ainda é recalculada pelo gate central.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .auxiliar import BrowserError, iniciar_browser
from .carga import BrowserResourceUnavailable, coordinated_ml_browser
from .coupon_validation_runner import ValidationObservation
from . import ml_auth


logger = logging.getLogger(__name__)
ML_CART_URL = "https://www.mercadolivre.com.br/gz/cart/v2"
_LOGIN_PATHS = ("/login", "/lgz/", "/registration", "loginhub")
_CHALLENGE_MARKERS = (
    "/gz/account-verification", "captcha", "não sou um robô", "nao sou um robo",
    "verifique que você é humano", "verifique que voce e humano",
)
_MONEY_RE = re.compile(r"R\$\s*([0-9][0-9.]*)(?:,([0-9]{1,2}))?", re.I)
_PRODUCT_TOKEN_RE = re.compile(r"\bMLB\d{6,}\b", re.I)


def _fold(value) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).casefold()


def _safe_body(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)[:50000]
    except Exception:
        return ""


def _valid_ml_product_url(value) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br"))
        and bool(parsed.path and parsed.path != "/")
    )


def _parse_money(value):
    match = _MONEY_RE.search(str(value or ""))
    if not match:
        return None
    raw = f"{match.group(1).replace('.', '')}.{(match.group(2) or '00')[:2]}"
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _cart_total(body):
    """Extrai o total do resumo, sem confundir preço unitário ou parcela."""
    lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    labels = ("total", "subtotal", "total dos produtos", "produtos")
    for wanted in labels:
        for index in range(len(lines) - 1, -1, -1):
            folded = _fold(lines[index]).strip(" :")
            if folded != wanted and not folded.startswith(f"{wanted} "):
                continue
            same_line = _parse_money(lines[index])
            if same_line is not None:
                return same_line
            for candidate in lines[index + 1:index + 4]:
                parsed = _parse_money(candidate)
                if parsed is not None:
                    return parsed
    return None


def _cart_empty(body):
    text = _fold(body)
    if any(marker in text for marker in (
        "seu carrinho esta vazio", "carrinho vazio",
        "voce ainda nao tem produtos", "adicione produtos ao carrinho",
    )):
        return True
    if any(marker in text for marker in (
        "resumo da compra", "remover produto", "excluir produto",
        "salvar para depois",
    )) and (_cart_total(body) or _MONEY_RE.search(str(body or ""))):
        return False
    return None


def _session_problem(url, body):
    folded_url = _fold(url)
    folded_body = _fold(body)
    if any(marker in folded_url for marker in _LOGIN_PATHS):
        return "session_expired"
    if any(_fold(marker) in folded_url or _fold(marker) in folded_body
           for marker in _CHALLENGE_MARKERS):
        return "challenge"
    return ""


def _coupon_feedback(body):
    """Classifica somente mensagens explícitas do marketplace."""
    text = _fold(body)
    groups = (
        ("expired", (
            "cupom expirou", "cupom esta expirado", "cupom expirado",
            "promocao terminou", "promocao ja terminou",
        )),
        ("usage_exhausted", (
            "limite de usos", "limite de uso", "voce ja usou este cupom",
            "cupom ja foi usado", "cupons disponiveis acabaram",
        )),
        ("invalid_code", (
            "cupom invalido", "codigo invalido", "nao reconhecemos esse cupom",
            "nao encontramos esse cupom", "cupom nao existe",
        )),
        ("minimum_not_met", (
            "valor minimo", "compra minima", "minimo para usar",
            "falta r$", "adicione mais produtos",
        )),
        ("target_not_eligible", (
            "nao se aplica", "nao e valido para estes produtos",
            "produto nao participa", "itens selecionados",
        )),
        ("payment_method_required", (
            "meio de pagamento", "forma de pagamento", "cartao mercado pago",
        )),
        ("account_not_eligible", (
            "usuarios selecionados", "clientes selecionados", "nao esta disponivel para voce",
            "nao e elegivel",
        )),
        ("applied", (
            "cupom aplicado", "codigo aplicado", "desconto aplicado",
            "voce economizou",
        )),
    )
    for reason, markers in groups:
        if any(marker in text for marker in markers):
            return reason
    return ""


def _first_visible(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _click_first(page, selectors) -> bool:
    locator = _first_visible(page, selectors)
    if locator is None:
        return False
    locator.click(timeout=7000)
    return True


def _open_cart(page):
    page.goto(ML_CART_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return _safe_body(page)


def _remove_target_from_cart(page, product_url) -> bool:
    token_match = _PRODUCT_TOKEN_RE.search(str(product_url or ""))
    token = token_match.group(0) if token_match else ""
    scopes = []
    if token:
        try:
            link = page.locator(f'a[href*="{token}" i]').first
            if link.count():
                scopes.extend((
                    link.locator("xpath=ancestor::li[1]"),
                    link.locator("xpath=ancestor::*[@data-testid][1]"),
                ))
        except Exception:
            pass
    scopes.append(page.locator("body"))
    selectors = (
        'button:has-text("Remover")', 'button:has-text("Excluir")',
        'a:has-text("Remover")', 'a:has-text("Excluir")',
        '[aria-label*="remover" i]', '[aria-label*="excluir" i]',
    )
    for scope in scopes:
        try:
            target = _first_visible(scope, selectors)
            if target is not None:
                target.click(timeout=5000)
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


def _observe_ml_cart(page, validation) -> ValidationObservation:
    evidence = {
        "no_purchase_boundary": True,
        "isolated_empty_cart": False,
        "cart_cleanup_attempted": False,
        "cart_cleanup_verified": False,
    }
    initial_body = _open_cart(page)
    problem = _session_problem(page.url, initial_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A sessão do Mercado Livre exige reconexão ou verificação.",
            evidence=evidence,
        )
    empty = _cart_empty(initial_body)
    if empty is not True:
        reason = "cart_not_empty" if empty is False else "cart_layout_unknown"
        return ValidationObservation(
            status="inconclusive", reason_code=reason,
            safe_detail=(
                "O carrinho precisa estar vazio para isolar o desconto do produto-alvo."
                if empty is False else
                "Não foi possível confirmar com segurança que o carrinho está vazio."
            ),
            evidence=evidence,
        )
    evidence["isolated_empty_cart"] = True

    page.goto(validation.product_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    product_body = _safe_body(page)
    problem = _session_problem(page.url, product_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A página do produto exigiu reconexão ou verificação.",
            evidence=evidence,
        )
    added = _click_first(page, (
        'button:has-text("Adicionar ao carrinho")',
        'a:has-text("Adicionar ao carrinho")',
        '[aria-label*="adicionar ao carrinho" i]',
    ))
    if not added:
        feedback = _fold(product_body)
        reason = "product_unavailable" if any(marker in feedback for marker in (
            "produto indisponivel", "anuncio pausado", "estoque esgotado",
        )) else "add_to_cart_control_missing"
        return ValidationObservation(
            status="inconclusive", reason_code=reason,
            safe_detail="Não foi possível adicionar o produto-alvo ao carrinho.",
            evidence=evidence,
        )

    observation = None
    try:
        page.wait_for_timeout(1200)
        cart_body = _open_cart(page)
        before = _cart_total(cart_body)
        if before is None:
            observation = ValidationObservation(
                status="inconclusive", reason_code="cart_total_missing",
                safe_detail="O total do carrinho não pôde ser medido.", evidence=evidence,
            )
            return observation

        # Algumas versões escondem o campo atrás de um expansor; nenhum seletor
        # abaixo inclui Comprar/Continuar/Checkout.
        _click_first(page, (
            'button:has-text("Inserir código do cupom")',
            'button:has-text("Adicionar cupom")',
            'button:has-text("Cupom")',
            'a:has-text("Inserir código do cupom")',
            '[aria-label*="cupom" i]',
        ))
        coupon_input = _first_visible(page, (
            'input[name*="coupon" i]', 'input[name*="cupom" i]',
            'input[placeholder*="cupom" i]', 'input[placeholder*="código" i]',
            'input[aria-label*="cupom" i]', 'input[aria-label*="código" i]',
        ))
        if coupon_input is None:
            observation = ValidationObservation(
                status="inconclusive", reason_code="coupon_control_missing",
                safe_detail="O campo de cupom não apareceu no carrinho.",
                subtotal_before=before, evidence=evidence,
            )
            return observation
        coupon_input.fill(str(validation.cupom.codigo or "").strip().upper())
        applied = False
        try:
            form = coupon_input.locator("xpath=ancestor::form[1]")
            applied = _click_first(form, (
                'button:has-text("Aplicar")', 'button:has-text("Usar")',
                'button[type="submit"]',
            ))
        except Exception:
            pass
        if not applied:
            applied = _click_first(page, (
                'button:has-text("Aplicar")', 'button:has-text("Usar cupom")',
            ))
        if not applied:
            observation = ValidationObservation(
                status="inconclusive", reason_code="coupon_apply_control_missing",
                safe_detail="O botão de aplicar cupom não apareceu.",
                subtotal_before=before, evidence=evidence,
            )
            return observation

        page.wait_for_timeout(1800)
        after_body = _safe_body(page)
        after = _cart_total(after_body)
        feedback = _coupon_feedback(after_body)
        evidence["marketplace_feedback"] = feedback or "none"
        evidence["monetary_transition_observed"] = bool(
            after is not None and after < before
        )
        if after is not None and after < before:
            observation = ValidationObservation(
                status="accepted", reason_code="checkout_discount_observed",
                safe_detail="O total do carrinho caiu após aplicar o código.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
            return observation
        if feedback in {
            "expired", "usage_exhausted", "invalid_code", "minimum_not_met",
            "target_not_eligible", "payment_method_required", "account_not_eligible",
        }:
            observation = ValidationObservation(
                status="rejected", reason_code=feedback,
                safe_detail="O Mercado Livre recusou o código neste carrinho isolado.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
            return observation
        observation = ValidationObservation(
            status="inconclusive", reason_code="discount_not_observed",
            safe_detail="Não houve redução monetária comprovável no carrinho.",
            subtotal_before=before, subtotal_after=after, evidence=evidence,
        )
        return observation
    finally:
        evidence["cart_cleanup_attempted"] = True
        try:
            # Remove primeiro o cupom, quando a interface oferecer esse controle.
            _click_first(page, (
                'button:has-text("Remover cupom")', 'a:has-text("Remover cupom")',
                '[aria-label*="remover cupom" i]',
            ))
            _remove_target_from_cart(page, validation.product_url)
            page.wait_for_timeout(500)
            evidence["cart_cleanup_verified"] = _cart_empty(_safe_body(page)) is True
        except Exception:
            logger.warning(
                "Falha ao conferir limpeza do carrinho na validação ML id=%s",
                validation.pk,
            )


def validate_mercadolivre(validation) -> ValidationObservation:
    """Valida um código ML com a sessão do próprio usuário, sem efetuar compra."""
    code = str(getattr(validation.cupom, "codigo", "") or "").strip().upper()
    if not code or len(code) > 60 or not _valid_ml_product_url(validation.product_url):
        return ValidationObservation(
            status="inconclusive", reason_code="invalid_input",
            safe_detail="Código ou URL de produto inválido para validação.",
            evidence={"no_purchase_boundary": True},
        )
    state = ml_auth.storage_state_para(validation.usuario)
    if state is None:
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="Conecte a conta do Mercado Livre para validar no carrinho.",
            evidence={"no_purchase_boundary": True},
        )
    try:
        with coordinated_ml_browser(
            usuario=validation.usuario, authenticated=True,
            owner_kind="coupon_checkout_validation",
        ), iniciar_browser(
            storage_state=state, session_user=validation.usuario, headless=True,
        ) as (page, _context):
            return _observe_ml_cart(page, validation)
    except BrowserResourceUnavailable:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_busy",
            safe_detail="O navegador compartilhado está ocupado; a fila tentará novamente.",
            evidence={"no_purchase_boundary": True},
        )
    except BrowserError:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_error",
            safe_detail="O navegador não conseguiu observar o carrinho.",
            evidence={"no_purchase_boundary": True},
        )


CHECKOUT_VALIDATION_ADAPTERS = {"mercadolivre": validate_mercadolivre}
