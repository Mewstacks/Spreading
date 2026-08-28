from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.coupon_validation_adapters import (
    _cart_empty, _cart_total, _coupon_feedback, _observe_ml_cart, _session_problem,
    _valid_ml_product_url, validate_mercadolivre,
)


class _FakeLocator:
    def __init__(self, page, selector, *, exists=False):
        self.page = page
        self.selector = selector
        self.exists = exists

    @property
    def first(self):
        return self

    def count(self):
        return int(self.exists)

    def is_visible(self):
        return self.exists

    def inner_text(self, timeout=None):
        return self.page.body()

    def fill(self, value):
        self.page.filled = value

    def click(self, timeout=None):
        selector = self.selector.casefold()
        self.page.clicked.append(selector)
        if "adicionar ao carrinho" in selector:
            self.page.added = True
        elif "aplicar" in selector or "type=\"submit\"" in selector:
            self.page.applied = True
        elif "remover cupom" in selector:
            self.page.applied = False
        elif "remover" in selector or "excluir" in selector:
            self.page.added = False

    def locator(self, selector):
        if selector.startswith("xpath=ancestor"):
            return _FakeLocator(self.page, "cart-item-scope", exists=self.exists)
        return self.page.locator(selector)


class _FakePage:
    def __init__(self):
        self.url = ""
        self.added = False
        self.applied = False
        self.filled = ""
        self.clicked = []

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def body(self):
        if "/gz/cart/" not in self.url:
            return "Produto disponível"
        if not self.added:
            return "Seu carrinho está vazio"
        total = "R$ 80,00" if self.applied else "R$ 100,00"
        feedback = "Cupom aplicado\n" if self.applied else ""
        return f"Resumo da compra\n{feedback}Total\n{total}\nRemover produto"

    def locator(self, selector):
        folded = selector.casefold()
        if selector == "body":
            return _FakeLocator(self, selector, exists=True)
        exists = False
        if "adicionar ao carrinho" in folded and "/gz/cart/" not in self.url:
            exists = True
        elif "adicionar cupom" in folded and self.added:
            exists = True
        elif "input" in folded and ("coupon" in folded or "cupom" in folded):
            exists = self.added
        elif ("aplicar" in folded or "type=\"submit\"" in folded) and self.added:
            exists = True
        elif "remover cupom" in folded:
            exists = self.applied
        elif "href" in folded and "mlb12345678" in folded:
            exists = self.added
        elif ("remover" in folded or "excluir" in folded) and self.added:
            exists = True
        return _FakeLocator(self, selector, exists=exists)


class MercadoLivreCheckoutAdapterTests(SimpleTestCase):
    def test_accepts_only_mercadolivre_https_product_urls(self):
        self.assertTrue(_valid_ml_product_url(
            "https://www.mercadolivre.com.br/produto/p/MLB12345678"
        ))
        self.assertFalse(_valid_ml_product_url("http://mercadolivre.com.br/p/MLB1"))
        self.assertFalse(_valid_ml_product_url("https://mercadolivre.com.br.evil.test/p/MLB1"))
        self.assertFalse(_valid_ml_product_url("https://meli.la/abc"))

    def test_extracts_total_from_purchase_summary_not_first_product_price(self):
        body = """Produto\nR$ 999,90\nResumo da compra\nProdutos (1)\nR$ 999,90\nCupom\n-R$ 100,00\nTotal\nR$ 899,90"""
        self.assertEqual(_cart_total(body), Decimal("899.90"))

    def test_understands_empty_and_nonempty_cart_without_guessing_unknown_layout(self):
        self.assertIs(_cart_empty("Seu carrinho está vazio\nConfira nossas ofertas"), True)
        self.assertIs(_cart_empty("Resumo da compra\nTotal\nR$ 120,00\nRemover produto"), False)
        self.assertIsNone(_cart_empty("Boas-vindas ao Mercado Livre"))

    def test_classifies_explicit_marketplace_feedback(self):
        self.assertEqual(_coupon_feedback("Este cupom expirou"), "expired")
        self.assertEqual(_coupon_feedback("Valor mínimo de R$ 200"), "minimum_not_met")
        self.assertEqual(_coupon_feedback("Cupom aplicado. Você economizou"), "applied")
        self.assertEqual(_coupon_feedback("Tente novamente"), "")

    def test_detects_login_and_challenge(self):
        self.assertEqual(_session_problem(
            "https://www.mercadolivre.com.br/login", ""), "session_expired"
        )
        self.assertEqual(_session_problem(
            "https://www.mercadolivre.com.br/gz/account-verification", ""), "challenge"
        )

    def test_isolated_cart_observes_reduction_and_cleans_up_without_checkout(self):
        page = _FakePage()
        validation = SimpleNamespace(
            pk=9, cupom=SimpleNamespace(codigo="TESTE20"),
            product_url="https://www.mercadolivre.com.br/produto/p/MLB12345678",
        )

        result = _observe_ml_cart(page, validation)

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.subtotal_before, Decimal("100.00"))
        self.assertEqual(result.subtotal_after, Decimal("80.00"))
        self.assertTrue(result.evidence["cart_cleanup_verified"])
        self.assertFalse(page.added)
        self.assertFalse(any(
            marker in selector for selector in page.clicked
            for marker in ("comprar", "checkout", "pagamento", "finalizar")
        ))

    @patch("apps.scrapers.coupon_validation_adapters.ml_auth.storage_state_para", return_value=None)
    def test_missing_user_session_fails_closed_without_opening_browser(self, _state):
        validation = SimpleNamespace(
            usuario=SimpleNamespace(id=7),
            cupom=SimpleNamespace(codigo="TESTE20"),
            product_url="https://www.mercadolivre.com.br/produto/p/MLB12345678",
        )
        result = validate_mercadolivre(validation)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "session_required")
        self.assertTrue(result.evidence["no_purchase_boundary"])

    def test_invalid_input_fails_before_session_lookup(self):
        validation = SimpleNamespace(
            usuario=SimpleNamespace(id=7), cupom=SimpleNamespace(codigo="TESTE20"),
            product_url="https://example.com/produto",
        )
        with patch("apps.scrapers.coupon_validation_adapters.ml_auth.storage_state_para") as state:
            result = validate_mercadolivre(validation)
        state.assert_not_called()
        self.assertEqual(result.reason_code, "invalid_input")
