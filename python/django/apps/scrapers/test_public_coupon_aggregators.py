import json
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.coupon_abundance import classe_da_fonte
from apps.scrapers.coupon_rules import (
    FONTES_COMUNIDADE, FONTES_COMUNIDADE_SEM_LISTAGEM,
)
from apps.scrapers.sources.persistence import _SOURCE_PRECEDENCE
from apps.scrapers.sources.public_coupon_aggregators import (
    BiaGarimpaCouponsSource, CupomSpotCouponsSource,
)
from apps.scrapers.sources.registry import SOURCES


def _bia_page(*rows):
    return "\n".join(
        '\\"coupon\\":' + json.dumps(row, separators=(",", ":")).replace('"', '\\"')
        for row in rows
    )


class BiaGarimpaCouponsTests(SimpleTestCase):
    def test_reads_typed_discount_validity_and_purchase_rules(self):
        body = _bia_page({
            "coupon_id": "cp_test", "retailer": "shopee", "code": "SHOPEE20",
            "discount_type": "fixed", "discount_value": 2000,
            "min_purchase": 6000, "max_discount": 2000,
            "categories": ["beleza"], "first_purchase_only": True,
            "valid_from": "2099-08-01T10:00:00Z",
            "valid_until": "2099-09-03T23:59:00Z",
            "validation_score": 85, "feedback_positive": 7, "feedback_total": 8,
        })
        source = BiaGarimpaCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "SHOPEE20")
        self.assertEqual(coupon.coupon_rules["tipo_desconto"], "fixo")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 20.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 60.0)
        self.assertEqual(coupon.coupon_rules["desconto_maximo"], 20.0)
        self.assertTrue(coupon.restricted)
        self.assertEqual(coupon.evidence["validation_score"], 85)

    def test_rejects_placeholders_free_shipping_and_wrong_store(self):
        body = _bia_page(
            {"coupon_id": "a", "retailer": "amazon", "code": "RESGATE NO LINK",
             "discount_type": "fixed", "discount_value": 2000,
             "valid_until": "2099-09-03T23:59:00Z"},
            {"coupon_id": "b", "retailer": "shopee", "code": "FRETEGRATIS20",
             "discount_type": "freeShipping", "discount_value": 1,
             "valid_until": "2099-09-03T23:59:00Z"},
        )
        source = BiaGarimpaCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "unsupported_discount": 1, "wrong_marketplace": 1,
        })


class CupomSpotCouponsTests(SimpleTestCase):
    def test_reads_schema_offer_and_uses_end_of_validity_day(self):
        body = """
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
          {"@type":"ListItem","item":{"@type":"Offer",
           "name":"Cupom Shopee — R$ 20 OFF em R$ 60",
           "couponCode":"GL4SSK1N","discount":"R$ 20.00 OFF",
           "priceValidUntil":"2099-09-02"}}
        ]}
        </script>
        """
        source = CupomSpotCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "GL4SSK1N")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 20.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 60.0)
        self.assertEqual(coupon.valid_until.year, 2099)
        self.assertEqual(coupon.valid_until.hour, 23)

    def test_offer_without_numeric_discount_is_rejected(self):
        body = """
        <script type="application/ld+json">
        {"@type":"Offer","name":"Cupom imperdível","couponCode":"REALCODE"}
        </script>
        """
        source = CupomSpotCouponsSource()
        with patch.object(source, "_download", return_value=body):
            self.assertEqual(
                list(source.discover_coupons(marketplaces=["amazon"])), [],
            )
        self.assertEqual(
            source.last_metrics["rejected_by_reason"],
            {"invalid_code_or_discount": 1},
        )


class PublicCouponAggregatorRegistrationTests(SimpleTestCase):
    def test_sources_are_registered_as_weak_independent_aggregators(self):
        for slug in ("bia-garimpa-cupons", "cupomspot-cupons"):
            self.assertIn(slug, SOURCES)
            self.assertIn(slug, FONTES_COMUNIDADE)
            self.assertIn(slug, FONTES_COMUNIDADE_SEM_LISTAGEM)
            self.assertEqual(classe_da_fonte(slug), "agregador")
            self.assertGreater(_SOURCE_PRECEDENCE[slug], 10)
            self.assertFalse(SOURCES[slug].inventario_completo)
