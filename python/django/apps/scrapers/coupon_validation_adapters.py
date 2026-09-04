"""Adaptadores conservadores de validação de cupom no carrinho/revisão.

O adaptador nunca altera pagamento nem clica em ações que criam pedidos. Para
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
AMAZON_CART_URL = "https://www.amazon.com.br/gp/cart/view.html"
SHOPEE_CART_URL = "https://shopee.com.br/cart"
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


def _valid_amazon_product_url(value) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and (host == "amazon.com.br" or host.endswith(".amazon.com.br"))
        and bool(re.search(r"/(?:dp|gp/product|gp/aw/d)/[A-Z0-9]{10}(?:[/?]|$)",
                           parsed.path, re.I))
    )


def _valid_shopee_product_url(value) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path or ""
    return (
        parsed.scheme == "https"
        and (host == "shopee.com.br" or host.endswith(".shopee.com.br"))
        and bool(
            re.search(r"/product/\d+/\d+(?:/|$)", path, re.I)
            or re.search(r"-i\.\d+\.\d+(?:/|$)", path, re.I)
        )
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
            "cupom ja foi usado", "cupons disponiveis acabaram", "cupom esgotado",
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


def _amazon_session_problem(url, body):
    folded_url = _fold(url)
    folded_body = _fold(body)
    if any(marker in folded_url for marker in ("/ap/signin", "/ap/cvf", "/signin")):
        return "session_expired"
    if any(marker in folded_body for marker in (
        "digite os caracteres que voce ve", "resolva este quebra-cabeca",
        "insira os caracteres acima", "verificacao necessaria",
    )):
        return "challenge"
    return ""


def _amazon_cart_empty(body):
    text = _fold(body)
    if any(marker in text for marker in (
        "seu carrinho da amazon esta vazio", "seu carrinho esta vazio",
        "carrinho de compras esta vazio",
    )):
        return True
    if any(marker in text for marker in (
        "subtotal", "excluir", "salvar para mais tarde",
    )) and _MONEY_RE.search(str(body or "")):
        return False
    return None


def _shopee_session_problem(url, body):
    folded_url = _fold(url)
    folded_body = _fold(body)
    if any(marker in folded_url for marker in ("/buyer/login", "/user/login")):
        return "session_expired"
    if "/verify/traffic" in folded_url or any(marker in folded_body for marker in (
        "verifique se voce e humano", "atividade incomum detectada",
        "complete a verificacao", "captcha",
    )):
        return "challenge"
    return ""


def _shopee_cart_empty(body):
    text = _fold(body)
    if any(marker in text for marker in (
        "seu carrinho de compras esta vazio", "seu carrinho esta vazio",
        "carrinho de compras esta vazio",
    )):
        return True
    if any(marker in text for marker in (
        "total", "excluir", "selecionar todos", "finalizar compra",
    )) and _MONEY_RE.search(str(body or "")):
        return False
    return None


def _open_shopee_cart(page):
    page.goto(SHOPEE_CART_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return _safe_body(page)


def _shopee_product_ids(product_url):
    value = str(product_url or "")
    match = re.search(r"/product/(\d+)/(\d+)(?:/|$)", value, re.I)
    if not match:
        match = re.search(r"-i\.(\d+)\.(\d+)(?:[/?]|$)", value, re.I)
    return match.groups() if match else ("", "")


def _remove_shopee_target(page, product_url) -> bool:
    _shop_id, item_id = _shopee_product_ids(product_url)
    scopes = []
    if item_id:
        try:
            link = page.locator(f'a[href*="{item_id}" i]').first
            if link.count():
                scopes.extend((
                    link.locator("xpath=ancestor::div[@data-sqe='item'][1]"),
                    link.locator("xpath=ancestor::*[contains(@class, 'cart-item')][1]"),
                ))
        except Exception:
            pass
    scopes.append(page.locator("body"))
    selectors = (
        'button:has-text("Excluir")', 'button:has-text("Remover")',
        '[aria-label*="excluir" i]', '[aria-label*="remover" i]',
    )
    for scope in scopes:
        try:
            target = _first_visible(scope, selectors)
            if target is not None:
                target.click(timeout=5000)
                page.wait_for_timeout(800)
                # A Shopee pode abrir confirmação; o único clique adicional
                # permitido continua sendo uma ação destrutiva do carrinho.
                _click_first(page, (
                    '[role="dialog"] button:has-text("Excluir")',
                    '[role="dialog"] button:has-text("Remover")',
                ))
                return True
        except Exception:
            continue
    return False


def _observe_shopee_checkout(page, validation) -> ValidationObservation:
    """Valida o código e nunca cruza a fronteira ``Fazer pedido``."""
    evidence = {
        "no_purchase_boundary": True,
        "isolated_empty_cart": False,
        "checkout_review_only": False,
        "address_changed": False,
        "payment_submitted": False,
        "order_created": False,
        "place_order_clicked": False,
        "cart_cleanup_attempted": False,
        "cart_cleanup_verified": False,
    }
    initial_body = _open_shopee_cart(page)
    problem = _shopee_session_problem(page.url, initial_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A sessão da Shopee exige reconexão ou verificação.",
            evidence=evidence,
        )
    empty = _shopee_cart_empty(initial_body)
    if empty is not True:
        return ValidationObservation(
            status="inconclusive",
            reason_code="cart_not_empty" if empty is False else "cart_layout_unknown",
            safe_detail=(
                "O carrinho precisa estar vazio para isolar o desconto do produto-alvo."
                if empty is False else
                "Não foi possível confirmar com segurança que o carrinho está vazio."
            ), evidence=evidence,
        )
    evidence["isolated_empty_cart"] = True

    page.goto(validation.product_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    product_body = _safe_body(page)
    problem = _shopee_session_problem(page.url, product_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A página do produto exigiu reconexão ou verificação.",
            evidence=evidence,
        )
    if not _click_first(page, (
        'button:has-text("Adicionar ao carrinho")',
        '[aria-label*="adicionar ao carrinho" i]',
    )):
        unavailable = any(marker in _fold(product_body) for marker in (
            "produto indisponivel", "esgotado", "sem estoque",
        ))
        return ValidationObservation(
            status="inconclusive",
            reason_code="product_unavailable" if unavailable else "add_to_cart_control_missing",
            safe_detail="Não foi possível adicionar o produto-alvo ao carrinho.",
            evidence=evidence,
        )

    try:
        page.wait_for_timeout(1200)
        cart_body = _open_shopee_cart(page)
        before = _cart_total(cart_body)
        if before is None:
            return ValidationObservation(
                status="inconclusive", reason_code="cart_total_missing",
                safe_detail="O total do carrinho não pôde ser medido.", evidence=evidence,
            )

        # A documentação da Shopee separa "Finalizar compra" de "Fazer
        # pedido". Este é o único avanço permitido e os seletores nunca
        # contêm comprar-agora, pagamento ou fazer-pedido.
        if not _click_first(page, (
            'button:has-text("Finalizar compra")',
            '[data-testid="checkout-button"]',
        )):
            return ValidationObservation(
                status="inconclusive", reason_code="checkout_review_control_missing",
                safe_detail="A revisão da compra não pôde ser aberta com segurança.",
                subtotal_before=before, evidence=evidence,
            )
        page.wait_for_timeout(1500)
        checkout_body = _safe_body(page)
        problem = _shopee_session_problem(page.url, checkout_body)
        if problem:
            return ValidationObservation(
                status="inconclusive", reason_code=problem,
                safe_detail="A revisão da Shopee exigiu reconexão ou verificação.",
                subtotal_before=before, evidence=evidence,
            )
        evidence["checkout_review_only"] = True
        before = _cart_total(checkout_body) or before
        _click_first(page, (
            'button:has-text("Cupom Shopee")',
            'button:has-text("Cupom de desconto")',
            'button:has-text("Inserir código")',
        ))
        coupon_input = _first_visible(page, (
            'input[name*="voucher" i]', 'input[name*="coupon" i]',
            'input[name*="code" i]', 'input[placeholder*="código" i]',
            'input[placeholder*="cupom" i]',
        ))
        if coupon_input is None:
            return ValidationObservation(
                status="inconclusive", reason_code="coupon_control_missing",
                safe_detail="O campo de cupom não apareceu na revisão.",
                subtotal_before=before, evidence=evidence,
            )
        coupon_input.fill(str(validation.cupom.codigo or "").strip().upper())
        if not _click_first(page, (
            'button:has-text("Aplicar")', 'button:has-text("Confirmar")',
        )):
            return ValidationObservation(
                status="inconclusive", reason_code="coupon_apply_control_missing",
                safe_detail="O botão de aplicar o cupom não apareceu.",
                subtotal_before=before, evidence=evidence,
            )
        page.wait_for_timeout(1800)
        after_body = _safe_body(page)
        after = _cart_total(after_body)
        feedback = _coupon_feedback(after_body)
        evidence["marketplace_feedback"] = feedback or "none"
        evidence["monetary_transition_observed"] = bool(
            after is not None and after < before
        )
        if after is not None and after < before:
            return ValidationObservation(
                status="accepted", reason_code="checkout_discount_observed",
                safe_detail="O total da revisão caiu após aplicar o cupom.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
        if feedback in {
            "expired", "usage_exhausted", "invalid_code", "minimum_not_met",
            "target_not_eligible", "payment_method_required", "account_not_eligible",
        }:
            return ValidationObservation(
                status="rejected", reason_code=feedback,
                safe_detail="A Shopee recusou o cupom nesta revisão isolada.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
        return ValidationObservation(
            status="inconclusive", reason_code="discount_not_observed",
            safe_detail="Não houve redução monetária comprovável na revisão.",
            subtotal_before=before, subtotal_after=after, evidence=evidence,
        )
    finally:
        evidence["cart_cleanup_attempted"] = True
        try:
            _open_shopee_cart(page)
            _remove_shopee_target(page, validation.product_url)
            page.wait_for_timeout(500)
            evidence["cart_cleanup_verified"] = (
                _shopee_cart_empty(_safe_body(page)) is True
            )
        except Exception:
            logger.warning(
                "Falha ao conferir limpeza do carrinho na validação Shopee id=%s",
                validation.pk,
            )


def _open_amazon_cart(page):
    page.goto(AMAZON_CART_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return _safe_body(page)


def _remove_amazon_target(page, product_url) -> bool:
    asin_match = re.search(
        r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)",
        str(product_url or ""), re.I,
    )
    asin = asin_match.group(1).upper() if asin_match else ""
    scopes = []
    if asin:
        try:
            row = page.locator(f'[data-asin="{asin}" i]').first
            if row.count():
                scopes.append(row)
        except Exception:
            pass
    scopes.append(page.locator("body"))
    selectors = (
        'input[value="Excluir"]', 'input[aria-label*="excluir" i]',
        'button:has-text("Excluir")', 'button:has-text("Remover")',
        '[data-action="delete"] input',
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


def _observe_amazon_checkout(page, validation) -> ValidationObservation:
    """Aplica o código na revisão da Amazon e nunca aciona pedido/pagamento."""
    evidence = {
        "no_purchase_boundary": True,
        "isolated_empty_cart": False,
        "checkout_review_only": False,
        "address_changed": False,
        "payment_submitted": False,
        "order_created": False,
        "place_order_clicked": False,
        "cart_cleanup_attempted": False,
        "cart_cleanup_verified": False,
    }
    initial_body = _open_amazon_cart(page)
    problem = _amazon_session_problem(page.url, initial_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A sessão de compras da Amazon exige reconexão ou verificação.",
            evidence=evidence,
        )
    empty = _amazon_cart_empty(initial_body)
    if empty is not True:
        return ValidationObservation(
            status="inconclusive",
            reason_code="cart_not_empty" if empty is False else "cart_layout_unknown",
            safe_detail=(
                "O carrinho precisa estar vazio para isolar o desconto do produto-alvo."
                if empty is False else
                "Não foi possível confirmar com segurança que o carrinho está vazio."
            ), evidence=evidence,
        )
    evidence["isolated_empty_cart"] = True

    page.goto(validation.product_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    product_body = _safe_body(page)
    problem = _amazon_session_problem(page.url, product_body)
    if problem:
        return ValidationObservation(
            status="inconclusive", reason_code=problem,
            safe_detail="A página do produto exigiu reconexão ou verificação.",
            evidence=evidence,
        )
    if not _click_first(page, (
        '#add-to-cart-button', 'input[name="submit.add-to-cart"]',
        'button[name="submit.add-to-cart"]',
    )):
        unavailable = any(marker in _fold(product_body) for marker in (
            "nao disponivel", "indisponivel", "sem estoque",
        ))
        return ValidationObservation(
            status="inconclusive",
            reason_code="product_unavailable" if unavailable else "add_to_cart_control_missing",
            safe_detail="Não foi possível adicionar o produto-alvo ao carrinho.",
            evidence=evidence,
        )

    try:
        page.wait_for_timeout(1200)
        cart_body = _open_amazon_cart(page)
        cart_total = _cart_total(cart_body)
        if cart_total is None:
            return ValidationObservation(
                status="inconclusive", reason_code="cart_total_missing",
                safe_detail="O total do carrinho não pôde ser medido.", evidence=evidence,
            )

        # Único passo permitido em direção ao checkout: abrir a revisão. Os
        # seletores são IDs/names próprios do botão do carrinho e nunca casam
        # Fazer pedido, Comprar agora, endereço ou pagamento.
        if not _click_first(page, (
            'input[name="proceedToRetailCheckout"]',
            'button[name="proceedToRetailCheckout"]',
            '#sc-buy-box-ptc-button input',
        )):
            return ValidationObservation(
                status="inconclusive", reason_code="checkout_review_control_missing",
                safe_detail="A revisão do pedido não pôde ser aberta com segurança.",
                subtotal_before=cart_total, evidence=evidence,
            )
        page.wait_for_timeout(1500)
        checkout_body = _safe_body(page)
        problem = _amazon_session_problem(page.url, checkout_body)
        if problem:
            return ValidationObservation(
                status="inconclusive", reason_code=problem,
                safe_detail="A revisão da Amazon exigiu reconexão ou verificação.",
                subtotal_before=cart_total, evidence=evidence,
            )
        evidence["checkout_review_only"] = True
        before = _cart_total(checkout_body) or cart_total
        coupon_input = _first_visible(page, (
            'input[name="ppw-claimCode"]', 'input[name="claimCode"]',
            '#spc-gcpromoinput', 'input[placeholder*="código promocional" i]',
            'input[placeholder*="cupom" i]',
        ))
        if coupon_input is None:
            return ValidationObservation(
                status="inconclusive", reason_code="coupon_control_missing",
                safe_detail="O campo de código promocional não apareceu na revisão.",
                subtotal_before=before, evidence=evidence,
            )
        coupon_input.fill(str(validation.cupom.codigo or "").strip().upper())
        applied = False
        try:
            form = coupon_input.locator("xpath=ancestor::form[1]")
            applied = _click_first(form, (
                'input[type="submit"]', 'button:has-text("Aplicar")',
            ))
        except Exception:
            pass
        if not applied:
            applied = _click_first(page, (
                'input[name="ppw-claimCodeApplyPressed"]',
                'button:has-text("Aplicar")',
            ))
        if not applied:
            return ValidationObservation(
                status="inconclusive", reason_code="coupon_apply_control_missing",
                safe_detail="O botão de aplicar o código não apareceu.",
                subtotal_before=before, evidence=evidence,
            )
        page.wait_for_timeout(1800)
        after_body = _safe_body(page)
        after = _cart_total(after_body)
        feedback = _coupon_feedback(after_body)
        evidence["marketplace_feedback"] = feedback or "none"
        evidence["monetary_transition_observed"] = bool(
            after is not None and after < before
        )
        if after is not None and after < before:
            return ValidationObservation(
                status="accepted", reason_code="checkout_discount_observed",
                safe_detail="O total da revisão caiu após aplicar o código.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
        if feedback in {
            "expired", "usage_exhausted", "invalid_code", "minimum_not_met",
            "target_not_eligible", "payment_method_required", "account_not_eligible",
        }:
            return ValidationObservation(
                status="rejected", reason_code=feedback,
                safe_detail="A Amazon recusou o código nesta revisão isolada.",
                subtotal_before=before, subtotal_after=after, evidence=evidence,
            )
        return ValidationObservation(
            status="inconclusive", reason_code="discount_not_observed",
            safe_detail="Não houve redução monetária comprovável na revisão.",
            subtotal_before=before, subtotal_after=after, evidence=evidence,
        )
    finally:
        evidence["cart_cleanup_attempted"] = True
        try:
            _open_amazon_cart(page)
            _remove_amazon_target(page, validation.product_url)
            page.wait_for_timeout(500)
            evidence["cart_cleanup_verified"] = (
                _amazon_cart_empty(_safe_body(page)) is True
            )
        except Exception:
            logger.warning(
                "Falha ao conferir limpeza do carrinho na validação Amazon id=%s",
                validation.pk,
            )


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
    # Credencial do remetente primeiro; sem ela, a de sistema. O veredito de um
    # carrinho é sobre o CÓDIGO, não sobre quem testa: se ele aplica desconto,
    # aplica para qualquer conta. Medido em 03/09/2026: das 3.840 validações
    # paradas em `session_required`, 3.021 eram de duas contas que simplesmente
    # nunca conectaram o Mercado Livre — trabalho jogado fora todo ciclo, e a
    # prova de checkout nunca acontecendo para ninguém. É o mesmo recuo que
    # `preco_ao_vivo.sessao_ml` já faz e documenta pelo mesmo motivo.
    dono_da_sessao = validation.usuario
    state = ml_auth.storage_state_para(dono_da_sessao)
    if state is None:
        dono_da_sessao = None
        state = ml_auth.storage_state(None)
    if state is None:
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="Conecte a conta do Mercado Livre para validar no carrinho.",
            evidence={"no_purchase_boundary": True},
        )
    try:
        with coordinated_ml_browser(
            usuario=dono_da_sessao, authenticated=True,
            owner_kind="coupon_checkout_validation",
            # Com espera, não pega-ou-desiste: a negativa inscreve esta esteira na
            # fila do Chromium e o lote longo cede entre páginas. Sem isso a
            # validação perdia a corrida para a raspagem em toda tentativa e
            # morria em `browser_busy` — foi o que sobrou depois de resolver
            # sessão e alvo.
            wait_seconds=90,
        ), iniciar_browser(
            storage_state=state, session_user=dono_da_sessao, headless=True,
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


def validate_amazon(validation) -> ValidationObservation:
    """Valida na revisão do pedido e sai antes de endereço/pagamento/pedido."""
    code = str(getattr(validation.cupom, "codigo", "") or "").strip().upper()
    if not code or len(code) > 60 or not _valid_amazon_product_url(validation.product_url):
        return ValidationObservation(
            status="inconclusive", reason_code="invalid_input",
            safe_detail="Código ou URL de produto inválido para validação.",
            evidence={"no_purchase_boundary": True},
        )
    from apps.scrapers.report_sessions import (
        has_report_session, load_report_state, registrar_veredito, save_report_state,
    )

    if not has_report_session(validation.usuario, "amazon_shop"):
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="Conecte a conta de compras da Amazon para validar o código.",
            evidence={"no_purchase_boundary": True},
        )
    try:
        state = load_report_state(validation.usuario, "amazon_shop")
    except ValueError:
        registrar_veredito(
            validation.usuario, "amazon_shop", "suspeito", "session_expired",
        )
        return ValidationObservation(
            status="inconclusive", reason_code="session_expired",
            safe_detail="A sessão de compras da Amazon está ilegível; reconecte.",
            evidence={"no_purchase_boundary": True},
        )
    if state is None:
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="A sessão de compras da Amazon não está mais disponível.",
            evidence={"no_purchase_boundary": True},
        )
    refreshed = None
    try:
        with coordinated_ml_browser(
            usuario=validation.usuario, authenticated=True,
            owner_kind="amazon_coupon_checkout_validation",
        ), iniciar_browser(storage_state=state, headless=True) as (page, context):
            observation = _observe_amazon_checkout(page, validation)
            if observation.reason_code not in {"session_expired", "challenge"}:
                refreshed = context.storage_state()
        if refreshed is not None:
            save_report_state(validation.usuario, "amazon_shop", refreshed)
        if observation.reason_code == "session_expired":
            registrar_veredito(
                validation.usuario, "amazon_shop", "suspeito",
                observation.reason_code,
            )
        elif observation.reason_code != "challenge":
            # CAPTCHA não prova que os cookies morreram e não deve zerar nem somar
            # o contador. Qualquer tela normal já demonstra que a sessão abriu.
            registrar_veredito(
                validation.usuario, "amazon_shop", "conectado",
                observation.reason_code,
            )
        return observation
    except BrowserResourceUnavailable:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_busy",
            safe_detail="O navegador compartilhado está ocupado; a fila tentará novamente.",
            evidence={"no_purchase_boundary": True},
        )
    except BrowserError:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_error",
            safe_detail="O navegador não conseguiu observar a revisão da Amazon.",
            evidence={"no_purchase_boundary": True},
        )


def validate_shopee(validation) -> ValidationObservation:
    """Valida cupom Shopee na revisão, sem criar ou pagar pedido."""
    code = str(getattr(validation.cupom, "codigo", "") or "").strip().upper()
    if not code or len(code) > 60 or not _valid_shopee_product_url(validation.product_url):
        return ValidationObservation(
            status="inconclusive", reason_code="invalid_input",
            safe_detail="Código ou URL de produto inválido para validação.",
            evidence={"no_purchase_boundary": True},
        )
    from apps.scrapers.report_sessions import (
        has_report_session, load_report_state, registrar_veredito, save_report_state,
    )

    if not has_report_session(validation.usuario, "shopee_shop"):
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="Conecte a conta de compras da Shopee para validar o cupom.",
            evidence={"no_purchase_boundary": True},
        )
    try:
        state = load_report_state(validation.usuario, "shopee_shop")
    except ValueError:
        registrar_veredito(
            validation.usuario, "shopee_shop", "suspeito", "session_expired",
        )
        return ValidationObservation(
            status="inconclusive", reason_code="session_expired",
            safe_detail="A sessão da Shopee está ilegível; reconecte.",
            evidence={"no_purchase_boundary": True},
        )
    if state is None:
        return ValidationObservation(
            status="inconclusive", reason_code="session_required",
            safe_detail="A sessão da Shopee não está mais disponível.",
            evidence={"no_purchase_boundary": True},
        )
    refreshed = None
    try:
        with coordinated_ml_browser(
            usuario=validation.usuario, authenticated=True,
            owner_kind="shopee_coupon_checkout_validation",
        ), iniciar_browser(storage_state=state, headless=True) as (page, context):
            observation = _observe_shopee_checkout(page, validation)
            if observation.reason_code not in {"session_expired", "challenge"}:
                refreshed = context.storage_state()
        if refreshed is not None:
            save_report_state(validation.usuario, "shopee_shop", refreshed)
        if observation.reason_code == "session_expired":
            registrar_veredito(
                validation.usuario, "shopee_shop", "suspeito",
                observation.reason_code,
            )
        elif observation.reason_code != "challenge":
            registrar_veredito(
                validation.usuario, "shopee_shop", "conectado",
                observation.reason_code,
            )
        return observation
    except BrowserResourceUnavailable:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_busy",
            safe_detail="O navegador compartilhado está ocupado; a fila tentará novamente.",
            evidence={"no_purchase_boundary": True},
        )
    except BrowserError:
        return ValidationObservation(
            status="inconclusive", reason_code="browser_error",
            safe_detail="O navegador não conseguiu observar a revisão da Shopee.",
            evidence={"no_purchase_boundary": True},
        )


CHECKOUT_VALIDATION_ADAPTERS = {
    "mercadolivre": validate_mercadolivre,
    "amazon": validate_amazon,
    "shopee": validate_shopee,
}
