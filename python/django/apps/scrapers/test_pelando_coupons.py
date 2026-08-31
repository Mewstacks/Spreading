from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.scrapers.coupon_abundance import classe_da_fonte
from apps.scrapers.coupon_rules import (
    FONTES_COMUNIDADE, FONTES_COMUNIDADE_SEM_LISTAGEM,
)
from apps.scrapers.sources.pelando_coupons import (
    PelandoCouponsSource, _official_url, _parse_coupon,
)
from apps.scrapers.sources.persistence import _SOURCE_PRECEDENCE
from apps.scrapers.sources.registry import SOURCES


def _payload(coupons, *, slug="amazon"):
    return {
        "data": {
            "stores": [
                {"slug": "amaro", "coupons": [{"couponCode": "ERRADO"}]},
                {"slug": slug, "coupons": coupons},
            ],
        },
    }


class PelandoCouponParserTests(SimpleTestCase):
    def test_extracts_active_code_and_rules_without_affiliate_redirect(self):
        row = {
            "id": "coupon-1", "status": "active", "couponCode": "NATORCIDA",
            "title": "30% off para contas novas",
            "rulesDescription": "Desconto de 30% com teto de R$30.",
            "discountPercentage": 30, "temperature": 141, "commentCount": 4,
            "sourceUrl": "https://www.amazon.com.br/promotion/ABC?ref=pelando",
            "redirectUrl": "https://dpl.pelando.com.br/r/affiliate-token",
        }

        item, reason = _parse_coupon("amazon", row, None)

        self.assertEqual(reason, "")
        self.assertEqual(item.coupon_code, "NATORCIDA")
        self.assertEqual(item.coupon_rules["valor_desconto"], 30)
        self.assertEqual(item.coupon_rules["desconto_maximo"], 30)
        self.assertTrue(item.restricted)
        self.assertEqual(item.canonical_url, "")
        self.assertEqual(
            item.evidence["official_source_url"],
            "https://www.amazon.com.br/promotion/ABC",
        )
        self.assertNotIn("redirect", item.evidence)
        self.assertEqual(item.evidence["temperature"], 141)

    def test_rejects_missing_code_placeholder_inactive_and_missing_discount(self):
        base = {"id": "x", "status": "active", "discountPercentage": 10}
        for code, reason in (
            (None, "missing_or_invalid_code"),
            ("RESGATENOLINK", "placeholder_code"),
        ):
            _, actual = _parse_coupon("shopee", {**base, "couponCode": code}, None)
            self.assertEqual(actual, reason)

        _, reason = _parse_coupon(
            "shopee", {**base, "status": "expired", "couponCode": "HOJE20"}, None,
        )
        self.assertEqual(reason, "inactive")
        _, reason = _parse_coupon(
            "shopee", {"id": "x", "status": "active", "couponCode": "HOJE20"}, None,
        )
        self.assertEqual(reason, "missing_discount")

    def test_fixed_discount_and_minimum_are_parsed(self):
        item, _ = _parse_coupon("mercadolivre", {
            "id": "coupon-2", "status": "active", "couponCode": "PROMOMELI30",
            "title": "R$ 30 off em compras acima de R$ 199",
            "discountFixed": 30,
        }, None)

        self.assertEqual(item.coupon_rules["tipo_desconto"], "fixo")
        self.assertEqual(item.coupon_rules["valor_desconto"], 30)
        self.assertEqual(item.coupon_rules["valor_minimo"], 199)

    def test_only_exact_official_domains_are_retained(self):
        self.assertEqual(
            _official_url("shopee", "https://seller.shopee.com.br/item/1?x=2"),
            "https://seller.shopee.com.br/item/1",
        )
        self.assertEqual(
            _official_url("shopee", "https://shopee.com.br.attacker.example/item"), "",
        )
        self.assertEqual(
            _official_url("amazon", "javascript:alert(1)"), "",
        )


class PelandoCouponSourceTests(SimpleTestCase):
    @patch("apps.scrapers.sources.pelando_coupons.time.sleep")
    def test_reads_only_exact_store_and_deduplicates(self, sleep):
        source = PelandoCouponsSource()
        body = _payload([
            {"id": "1", "status": "active", "couponCode": "NIVEA20",
             "discountPercentage": 20},
            {"id": "2", "status": "active", "couponCode": "NIVEA20",
             "discountPercentage": 20},
            {"id": "3", "status": "active", "couponCode": None,
             "discountPercentage": 15},
        ])
        with patch.object(PelandoCouponsSource, "_download", return_value=body):
            items = list(source.discover_coupons(marketplaces=["amazon"]))

        self.assertEqual([item.coupon_code for item in items], ["NIVEA20"])
        self.assertEqual(source.last_metrics["cupons_vistos"], 3)
        self.assertEqual(source.last_metrics["rejected_by_reason"]["duplicate_code"], 1)
        self.assertEqual(
            source.last_metrics["rejected_by_reason"]["missing_or_invalid_code"], 1,
        )
        self.assertFalse(source.last_metrics["complete"])
        self.assertEqual(source.last_health_status, "healthy")
        sleep.assert_not_called()

    def test_http_request_is_identified_and_does_not_use_a_browser_ua(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = _payload([])
        source = PelandoCouponsSource()
        with patch("apps.scrapers.sources.pelando_coupons.requests.get",
                   return_value=response) as get:
            source._download("amazon")

        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["params"], {"term": "amazon"})
        self.assertIn("SpreadingCouponRadar", kwargs["headers"]["User-Agent"])
        self.assertNotIn("Mozilla", kwargs["headers"]["User-Agent"])
        self.assertNotIn("redirect", kwargs)

    def test_missing_exact_store_marks_source_degraded(self):
        source = PelandoCouponsSource()
        with patch.object(PelandoCouponsSource, "_download",
                          return_value=_payload([], slug="amaro")):
            items = list(source.discover_coupons(marketplaces=["amazon"]))

        self.assertEqual(items, [])
        self.assertEqual(source.last_health_status, "degraded")
        self.assertEqual(source.last_metrics["rejected_by_reason"]["exact_store_missing"], 1)


class PelandoCouponIntegrationPolicyTests(SimpleTestCase):
    def test_registered_as_partial_low_precedence_aggregator(self):
        self.assertIn("pelando-cupons", SOURCES)
        self.assertFalse(SOURCES["pelando-cupons"].inventario_completo)
        self.assertEqual(classe_da_fonte("pelando-cupons"), "agregador")
        self.assertGreater(
            _SOURCE_PRECEDENCE["pelando-cupons"],
            _SOURCE_PRECEDENCE["ml-cupons-afiliados"],
        )

    def test_never_becomes_ready_by_itself(self):
        self.assertIn("pelando-cupons", FONTES_COMUNIDADE)
        self.assertIn("pelando-cupons", FONTES_COMUNIDADE_SEM_LISTAGEM)
