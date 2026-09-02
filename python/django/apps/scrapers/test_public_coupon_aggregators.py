import json
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.coupon_abundance import classe_da_fonte
from apps.scrapers.coupon_rules import (
    FONTES_COMUNIDADE, FONTES_COMUNIDADE_SEM_LISTAGEM,
)
from apps.scrapers.sources.persistence import _SOURCE_PRECEDENCE
from apps.scrapers.sources.public_coupon_aggregators import (
    BiaGarimpaCouponsSource, CashbeShopeeCouponsSource,
    CuponationShopeeCouponsSource,
    CupomSpotCouponsSource, DiscoupShopeeCouponsSource, PrimaRycaCouponsSource,
    PegueiBaratoAmazonCouponsSource, PromomiaShopeeCouponsSource,
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

    def test_product_price_is_not_misread_as_fixed_coupon_discount(self):
        body = """
        <script type="application/ld+json">
        {"@type":"Offer","name":"Cupom Amazon — Monitor Gamer — R$ 656",
         "couponCode":"10OFFAGORAOU",
         "description":"Preço atual do produto, sem desconto explícito"}
        </script>
        """
        source = CupomSpotCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["amazon"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "invalid_code_or_discount": 1,
        })


class PrimaRycaCouponsTests(SimpleTestCase):
    def _page(self, amazon_rows=(), shopee_rows=()):
        def section(name, rows):
            cards = ",".join(
                '[\\"$\\",\\"card\\",null,{\\"coupon\\":'
                + json.dumps(row, separators=(",", ":")).replace('"', '\\"')
                + "}]" for row in rows
            )
            return f'[\\"$\\",\\"section\\",\\"{name}\\",{{\\"children\\":[{cards}]}}]'
        return section("amazon", amazon_rows) + section("shopee", shopee_rows)

    def test_accepts_typed_current_card_and_never_keeps_affiliate_redirect(self):
        body = self._page(shopee_rows=({
            "id": "sh-1", "code": "SHOPEE15", "description": "Mín. R$79",
            "discountType": "fixed", "discountValue": "15",
            "scopeType": "marketplace", "expiresAt": "2099-09-30T23:59:00Z",
            "eligibleProductsUrl": None,
            "redeemUrl": "https://s.shopee.com.br/abc123",
        },))
        source = PrimaRycaCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "SHOPEE15")
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 79.0)
        self.assertEqual(coupon.canonical_url, "")
        self.assertTrue(coupon.evidence["has_public_marketplace_link"])

    def test_rejects_explicitly_expired_and_cross_marketplace_cards(self):
        body = self._page(shopee_rows=(
            {"id": "old", "code": "OLD20",
             "description": "válido até 31/08/2020", "discountType": "fixed",
             "discountValue": "20", "expiresAt": None,
             "redeemUrl": "https://s.shopee.com.br/old"},
            {"id": "wrong", "code": "WRONG20", "description": "R$20 OFF",
             "discountType": "fixed", "discountValue": "20", "expiresAt": None,
             "redeemUrl": "https://produto.mercadolivre.com.br/MLB-123"},
        ))
        source = PrimaRycaCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "expired_in_description": 1, "wrong_marketplace_url": 1,
        })


class DiscoupShopeeCouponsTests(SimpleTestCase):
    ID = "019fa60c-df67-7f72-91cb-0a7efb3dfc49"

    def _page(self, *, name="Cupom Shopee R$20 OFF",
              description="Em compras acima de R$60"):
        return f"""
        <script type="application/ld+json">
        {{"@context":"https://schema.org","@type":"ItemList",
          "itemListElement":[{{"@type":"ListItem","item":{{
            "@type":"Offer","@id":"https://discoup.test/p#{self.ID}",
            "name":{json.dumps(name)},"description":{json.dumps(description)},
            "validThrough":"2099-09-24T02:59:00Z"}}}}]}}
        </script>
        """

    def test_reveals_public_code_and_keeps_no_outbound_affiliate_link(self):
        source = DiscoupShopeeCouponsSource()
        popup = """
        <h3>Cupom Shopee R$20 OFF</h3>
        <a onclick="Out.offer(this,{copy:&#39;SHOPEE20&#39;,attrs:{}})">Copiar</a>
        """
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=self._page()), patch.object(
                source, "_download_popup", return_value=(popup, False)):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "SHOPEE20")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 20.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 60.0)
        self.assertEqual(coupon.canonical_url, "")
        self.assertEqual(coupon.evidence["transport"], "discoup-schema-popup")

    def test_rejects_cashback_even_when_popup_has_a_code(self):
        source = DiscoupShopeeCouponsSource()
        popup = "<a onclick=\"x({copy:&#39;MOEDAS20&#39;})\">Copiar</a>"
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=self._page(
                    name="20% cashback em moedas", description="limitado a R$20",
                )), patch.object(
                source, "_download_popup", return_value=(popup, False)):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {"cashback": 1})


class PromomiaShopeeCouponsTests(SimpleTestCase):
    def _page(self, *, discount="R$20", code="PROMO20", expired="false"):
        return (
            '<script>self.__next_f.push([1,"27:[\\"$\\",\\"card\\",null,'
            '{\\"couponId\\":\\"coupon-1\\",\\"storeSlug\\":\\"shopee\\",'
            f'\\"discountLabel\\":\\"{discount}\\",'
            '\\"children\\":\\"ate 30/09/2099\\",'
            '\\"children\\":\\"Cupom Shopee R$20 OFF em R$60\\",'
            f'\\"couponCode\\":\\"{code}\\",\\"isExpired\\":{expired}'
            '}]"])</script>'
        )

    def test_accepts_only_structured_numeric_coupon_card(self):
        source = PromomiaShopeeCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=self._page()):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "PROMO20")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 20.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 60.0)
        self.assertEqual(coupon.canonical_url, "")

    def test_rejects_card_without_numeric_benefit(self):
        source = PromomiaShopeeCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=self._page(discount="$undefined")):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "invalid_code_or_discount": 1,
        })


class CuponationShopeeCouponsTests(SimpleTestCase):
    @staticmethod
    def _page(*rows):
        flight = "4a:" + ",".join(json.dumps(row) for row in rows)
        return "<script>self.__next_f.push(" + json.dumps([1, flight]) + ")</script>"

    @staticmethod
    def _row(**overrides):
        row = {
            "idPool": "voucher-1", "title": "R$20 OFF acima de R$60",
            "caption1": "R$20", "caption2": "OFF", "voucherType": 0,
            "endTime": "2099-09-24T02:59:00Z",
            "startTime": "2099-09-01T10:00:00Z",
            "verified": "2099-09-01T11:00:00Z", "published": True,
            "code": "SHOPEE20", "termsAndConditions": "Compras acima de R$60",
            "encryptedAffiliateUrl": "never-persist-this", "__typename": "Voucher",
        }
        row.update(overrides)
        return row

    def test_reads_complete_voucher_object_and_drops_affiliate_redirect(self):
        source = CuponationShopeeCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=self._page(self._row())):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 1)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "SHOPEE20")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 20.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 60.0)
        self.assertEqual(coupon.canonical_url, "")
        self.assertNotIn("encryptedAffiliateUrl", coupon.evidence)
        self.assertEqual(coupon.evidence["transport"], "cuponation-next-rsc")

    def test_cards_do_not_share_fields_and_deals_are_rejected(self):
        source = CuponationShopeeCouponsSource()
        body = self._page(
            self._row(idPool="deal", voucherType=1, code="DEAL20"),
            self._row(idPool="missing", code=None),
        )
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "invalid_code_or_discount": 1, "not_coupon": 1,
        })


class CashbeShopeeCouponsTests(SimpleTestCase):
    @staticmethod
    def _card(title, code, expiry):
        return f"""
        <div class="card coupons__card">
          <h3 class="card__name">{title}</h3>
          <div class="card__info">{expiry}</div>
          <div class="card__button-code">{code}</div>
        </div>
        """

    def test_reads_public_card_rules_and_relative_expiry(self):
        body = "".join((
            self._card(
                "Resgate o cupom de R$15 OFF em compras acima de R$69",
                "T4FALXAND0AF", "Expira: em 23 dias",
            ),
            self._card(
                "Economize R$35 em compras a partir de R$199",
                "P4R4V0C3", "Expira: em 23 dias",
            ),
        ))
        source = CashbeShopeeCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual(len(rows), 2)
        coupon = rows[0]
        self.assertEqual(coupon.coupon_code, "T4FALXAND0AF")
        self.assertEqual(coupon.coupon_rules["valor_desconto"], 15.0)
        self.assertEqual(coupon.coupon_rules["valor_minimo"], 69.0)
        self.assertGreater(coupon.valid_until, coupon.observed_at)
        self.assertEqual(coupon.canonical_url, "")
        self.assertEqual(coupon.evidence["transport"], "cashbe-public-card")
        self.assertEqual(rows[1].coupon_rules["valor_desconto"], 35.0)
        self.assertEqual(rows[1].coupon_rules["valor_minimo"], 199.0)

    def test_rejects_expired_missing_expiry_and_duplicate_cards(self):
        body = "".join((
            self._card("R$20 OFF acima de R$60", "OLD20", "Expira: há 8 horas"),
            self._card("R$10 OFF", "NOEXP10", "Confira as regras"),
            self._card("10% OFF", "SAME10", "Expira: em 3 horas"),
            self._card("10% OFF", "SAME10", "Expira: em 3 horas"),
        ))
        source = CashbeShopeeCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=body):
            rows = list(source.discover_coupons(marketplaces=["shopee"]))

        self.assertEqual([row.coupon_code for row in rows], ["SAME10"])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "duplicate_code": 1, "expired": 1, "missing_expiry": 1,
        })


class PublicCouponAggregatorRegistrationTests(SimpleTestCase):
    def test_sources_are_registered_as_weak_independent_aggregators(self):
        for slug in (
            "bia-garimpa-cupons", "cupomspot-cupons", "prima-ryca-cupons",
            "discoup-cupons", "promomia-cupons", "cuponation-cupons",
            "cashbe-cupons", "linkerhub-cupons", "peguei-barato-cupons",
        ):
            self.assertIn(slug, SOURCES)
            self.assertIn(slug, FONTES_COMUNIDADE)
            self.assertIn(slug, FONTES_COMUNIDADE_SEM_LISTAGEM)
            self.assertEqual(classe_da_fonte(slug), "agregador")
            self.assertGreater(_SOURCE_PRECEDENCE[slug], 10)
            self.assertFalse(SOURCES[slug].inventario_completo)


class PegueiBaratoAmazonCouponsTests(SimpleTestCase):
    @staticmethod
    def _card(card_id, title, code, *, destination="https://www.amazon.com.br/deals"):
        return f"""
        <div id="{card_id}" class="coupon__item" data-coupon-id="{card_id}">
          <h3 data-field="coupon-title">{title}</h3>
          <div data-field="description">Regra pública do card</div>
          <span class="coupon__code">{code}</span>
          <div class="coupon__action coupon__action--coupon">
            <a href="{destination}?tag=terceiro-20">Ver cupom</a>
          </div>
        </div>
        """

    def test_accepts_only_numeric_consistent_amazon_cards_without_third_party_link(self):
        ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 10)]
        body = "".join((
            self._card(ids[0], "10% OFF acima de R$150 em itens Growth", "GROWTH"),
            self._card(ids[1], "R$350 OFF em smartphones selecionados", "350SMART"),
            self._card(ids[2], "Até R$200 OFF no PlayStation 5", "APP200OFF"),
            self._card(ids[3], "R$20 OFF na sua primeira compra no App Amazon",
                       "COMPRANOAPP"),
            # O índice mistura a porcentagem da vitrine com o código LEIA20. Não
            # podemos anunciar 70% como benefício desse cupom.
            self._card(ids[4], "Até 70% OFF nos livros mais vendidos", "LEIA20"),
            self._card(ids[5], "Até 60% OFF nas ofertas 9.9", "COMPRANOAPP"),
            self._card(ids[6], "Oferta especial no Kindle", "KINDLE10"),
            self._card(ids[7], "R$40 OFF", "OUTRA40",
                       destination="https://loja.example/cupom"),
            self._card(ids[8], "R$350 OFF em smartphones selecionados", "350SMART"),
        ))
        source = PegueiBaratoAmazonCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value=body):
            rows = list(source.discover_coupons(marketplaces=["amazon"]))

        self.assertEqual(
            [row.coupon_code for row in rows],
            ["350SMART", "APP200OFF", "COMPRANOAPP"],
        )
        self.assertTrue(all(row.canonical_url == "" for row in rows))
        self.assertTrue(all(
            row.evidence["transport"] == "peguei-barato-public-card"
            for row in rows
        ))
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "code_discount_mismatch": 1,
            "duplicate_code": 1,
            "generic_code_on_offer": 1,
            "invalid_code_or_discount": 1,
            "missing_explicit_discount": 1,
            "wrong_marketplace_url": 1,
        })

    def test_schema_without_cards_fails_health_closed(self):
        source = PegueiBaratoAmazonCouponsSource()
        with patch(
                "apps.scrapers.sources.public_coupon_aggregators._download",
                return_value="<html>layout mudou</html>"):
            self.assertEqual(list(source.discover_coupons()), [])

        self.assertEqual(source.last_health_status, "degraded")
        self.assertEqual(source.last_metrics["rows_seen"], 0)
