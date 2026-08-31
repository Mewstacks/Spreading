from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.coupon_validation_adapters import (
    _cart_empty, _cart_total, _coupon_feedback, _observe_amazon_checkout,
    _observe_ml_cart, _observe_shopee_checkout, _session_problem,
    _valid_amazon_product_url, _valid_ml_product_url, _valid_shopee_product_url,
    validate_amazon, validate_mercadolivre, validate_shopee,
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


class _FakeAmazonLocator(_FakeLocator):
    def click(self, timeout=None):
        selector = self.selector.casefold()
        self.page.clicked.append(selector)
        if "add-to-cart" in selector:
            self.page.added = True
        elif "proceedtoretailcheckout" in selector:
            self.page.url = "https://www.amazon.com.br/gp/buy/spc/handlers/display.html"
        elif "submit" in selector or "aplicar" in selector:
            self.page.applied = True
        elif "excluir" in selector or "delete" in selector:
            self.page.added = False

    def locator(self, selector):
        if selector.startswith("xpath=ancestor"):
            return _FakeAmazonLocator(self.page, "form", exists=self.exists)
        return self.page.locator(selector)


class _FakeAmazonPage(_FakePage):
    def body(self):
        if "/gp/cart/" in self.url:
            if not self.added:
                return "Seu carrinho da Amazon está vazio"
            return "Subtotal (1 produto)\nR$ 100,00\nExcluir"
        if "/gp/buy/spc/" in self.url:
            total = "R$ 80,00" if self.applied else "R$ 100,00"
            feedback = "Código aplicado\n" if self.applied else ""
            return f"Resumo do pedido\n{feedback}Total\n{total}"
        return "Produto disponível"

    def locator(self, selector):
        folded = selector.casefold()
        if selector == "body":
            return _FakeAmazonLocator(self, selector, exists=True)
        exists = False
        if ("add-to-cart" in folded and "/dp/" in self.url):
            exists = True
        elif "proceedtoretailcheckout" in folded and self.added and "/gp/cart/" in self.url:
            exists = True
        elif ("claimcode" in folded or "gcpromoinput" in folded) and "/gp/buy/spc/" in self.url:
            exists = True
        elif ("submit" in folded or "aplicar" in folded) and "/gp/buy/spc/" in self.url:
            exists = True
        elif "data-asin" in folded and self.added:
            exists = True
        elif ("excluir" in folded or "data-action=\"delete\"" in folded) and self.added:
            exists = True
        return _FakeAmazonLocator(self, selector, exists=exists)


class _FakeShopeeLocator(_FakeLocator):
    def click(self, timeout=None):
        selector = self.selector.casefold()
        self.page.clicked.append(selector)
        if "adicionar ao carrinho" in selector:
            self.page.added = True
        elif "finalizar compra" in selector or "checkout-button" in selector:
            self.page.url = "https://shopee.com.br/checkout"
        elif "aplicar" in selector or "confirmar" in selector:
            self.page.applied = True
        elif "excluir" in selector or "remover" in selector:
            self.page.added = False

    def locator(self, selector):
        if selector.startswith("xpath=ancestor"):
            return _FakeShopeeLocator(self.page, "cart-item", exists=self.exists)
        return self.page.locator(selector)


class _FakeShopeePage(_FakePage):
    def body(self):
        if self.url.endswith("/cart"):
            if not self.added:
                return "Seu carrinho de compras está vazio"
            return "Selecionar todos\nTotal\nR$ 100,00\nExcluir\nFinalizar compra"
        if self.url.endswith("/checkout"):
            total = "R$ 80,00" if self.applied else "R$ 100,00"
            feedback = "Cupom aplicado\n" if self.applied else ""
            return f"Revisão da compra\nCupom Shopee\n{feedback}Total\n{total}\nFazer pedido"
        return "Produto disponível"

    def locator(self, selector):
        folded = selector.casefold()
        if selector == "body":
            return _FakeShopeeLocator(self, selector, exists=True)
        exists = False
        if "adicionar ao carrinho" in folded and "-i." in self.url:
            exists = True
        elif ("finalizar compra" in folded or "checkout-button" in folded) and self.added and self.url.endswith("/cart"):
            exists = True
        elif any(marker in folded for marker in ("voucher", "coupon", "code", "código", "cupom")) and "input" in folded and self.url.endswith("/checkout"):
            exists = True
        elif ("aplicar" in folded or "confirmar" in folded) and self.url.endswith("/checkout"):
            exists = True
        elif "href" in folded and "987654321" in folded and self.added:
            exists = True
        elif ("excluir" in folded or "remover" in folded) and self.added:
            exists = True
        return _FakeShopeeLocator(self, selector, exists=exists)


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


class AmazonCheckoutAdapterTests(SimpleTestCase):
    def test_accepts_only_canonical_amazon_product_urls(self):
        self.assertTrue(_valid_amazon_product_url(
            "https://www.amazon.com.br/dp/B012345678?tag=minha-20",
        ))
        self.assertFalse(_valid_amazon_product_url(
            "https://amazon.com.br.evil.test/dp/B012345678",
        ))
        self.assertFalse(_valid_amazon_product_url("http://amazon.com.br/dp/B012345678"))
        self.assertFalse(_valid_amazon_product_url("https://amazon.com.br/ofertas"))

    def test_review_observes_reduction_and_never_places_order(self):
        page = _FakeAmazonPage()
        validation = SimpleNamespace(
            pk=10, cupom=SimpleNamespace(codigo="AMAZON20"),
            product_url="https://www.amazon.com.br/dp/B012345678",
        )

        result = _observe_amazon_checkout(page, validation)

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.subtotal_before, Decimal("100.00"))
        self.assertEqual(result.subtotal_after, Decimal("80.00"))
        self.assertTrue(result.evidence["checkout_review_only"])
        self.assertTrue(result.evidence["cart_cleanup_verified"])
        self.assertFalse(result.evidence["place_order_clicked"])
        self.assertFalse(page.added)
        self.assertFalse(any(
            marker in selector for selector in page.clicked
            for marker in ("place-order", "fazer pedido", "comprar agora", "payment")
        ))

    @patch("apps.scrapers.report_sessions.has_report_session", return_value=False)
    def test_missing_shopper_session_fails_closed(self, _session):
        validation = SimpleNamespace(
            usuario=SimpleNamespace(id=7), cupom=SimpleNamespace(codigo="AMAZON20"),
            product_url="https://www.amazon.com.br/dp/B012345678",
        )

        result = validate_amazon(validation)

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "session_required")
        self.assertTrue(result.evidence["no_purchase_boundary"])


class ShopeeCheckoutAdapterTests(SimpleTestCase):
    def test_accepts_only_canonical_shopee_product_urls(self):
        self.assertTrue(_valid_shopee_product_url(
            "https://shopee.com.br/Fone-Bluetooth-i.123456789.987654321",
        ))
        self.assertTrue(_valid_shopee_product_url(
            "https://shopee.com.br/product/123456789/987654321",
        ))
        self.assertFalse(_valid_shopee_product_url(
            "https://shopee.com.br.evil.test/Fone-i.123.456",
        ))
        self.assertFalse(_valid_shopee_product_url(
            "http://shopee.com.br/Fone-i.123.456",
        ))
        self.assertFalse(_valid_shopee_product_url("https://shopee.com.br/cart"))

    def test_review_observes_reduction_cleans_cart_and_never_places_order(self):
        page = _FakeShopeePage()
        validation = SimpleNamespace(
            pk=11, cupom=SimpleNamespace(codigo="SHOPEE20"),
            product_url="https://shopee.com.br/Fone-i.123456789.987654321",
        )

        result = _observe_shopee_checkout(page, validation)

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.subtotal_before, Decimal("100.00"))
        self.assertEqual(result.subtotal_after, Decimal("80.00"))
        self.assertTrue(result.evidence["checkout_review_only"])
        self.assertTrue(result.evidence["cart_cleanup_verified"])
        self.assertFalse(result.evidence["place_order_clicked"])
        self.assertFalse(page.added)
        self.assertTrue(any("finalizar compra" in value for value in page.clicked))
        self.assertFalse(any(
            marker in selector for selector in page.clicked
            for marker in ("fazer pedido", "comprar agora", "payment", "pagamento")
        ))

    @patch("apps.scrapers.report_sessions.has_report_session", return_value=False)
    def test_missing_shopper_session_fails_closed(self, _session):
        validation = SimpleNamespace(
            usuario=SimpleNamespace(id=7), cupom=SimpleNamespace(codigo="SHOPEE20"),
            product_url="https://shopee.com.br/Fone-i.123456789.987654321",
        )

        result = validate_shopee(validation)

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "session_required")
        self.assertTrue(result.evidence["no_purchase_boundary"])
