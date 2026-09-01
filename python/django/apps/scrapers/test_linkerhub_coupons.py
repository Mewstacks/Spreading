from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.sources.linkerhub_coupons import LinkerHubCouponsSource


def _card(store, validity, heading, description, code):
    return f"""
    <div class="glass-panel rounded-3xl">
      <div><span class="uppercase font-black">{store}</span>
      <span class="text-zinc-500 font-bold">{validity}</span></div>
      <div><h3>{heading}</h3>
      <p class="text-zinc-300 line-clamp-2">{description}</p></div>
      <div><div class="font-mono font-black">{code}</div></div>
    </div>
    """


class LinkerHubCouponsTests(SimpleTestCase):
    def _discover(self, body, marketplaces=None):
        source = LinkerHubCouponsSource()
        with patch.object(source, "_download", return_value=body):
            rows = list(source.discover_coupons(marketplaces=marketplaces))
        return source, rows

    def test_accepts_all_three_marketplaces_and_never_keeps_redirect(self):
        body = "".join((
            _card("Amazon", "Válido até 16/09/2099", "R$ 20 OFF",
                  "Cupom: PRIME20 em compras acima de R$ 60", "PRIME20"),
            _card("Mercado Livre", "Expira amanhã", "15% OFF",
                  "Cupom para tecnologia; limite de R$ 50", "TECH15ML"),
            _card("Shopee", "Expira em 2 dias!", "OFERTA",
                  "🎟 SHOPEE10 R$10 OFF em R$69", "SHOPEE10"),
        ))
        source, rows = self._discover(body)

        self.assertEqual(len(rows), 3)
        self.assertEqual({row.marketplace for row in rows}, {
            "amazon", "mercadolivre", "shopee",
        })
        self.assertTrue(all(row.canonical_url == "" for row in rows))
        shopee = next(row for row in rows if row.marketplace == "shopee")
        self.assertEqual(shopee.coupon_rules["valor_desconto"], 10.0)
        self.assertEqual(shopee.coupon_rules["valor_minimo"], 69.0)
        self.assertEqual(source.last_metrics["accepted_by_marketplace"], {
            "amazon": 1, "mercadolivre": 1, "shopee": 1,
        })

    def test_rejects_wrong_store_code_mismatch_placeholder_and_product_price(self):
        body = "".join((
            _card("Amazon", "Expira em 2 dias", "OFERTA",
                  "R$20 OFF Cupom: PRAVCNOML https://meli.la/abc", "PRAVCNOML"),
            _card("Shopee", "Válido até 09/09/2099", "OFERTA",
                  "🎟 C0RR1D499 R$10 OFF em R$69", "ANTES"),
            _card("Amazon", "Válido por tempo limitado", "OFERTA",
                  "Produto por R$ 232,76", "AMAZONPROD"),
        ))
        source, rows = self._discover(body)

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "invalid_or_placeholder_code": 1,
            "no_explicit_coupon_discount": 1,
            "wrong_marketplace_url": 1,
        })

    def test_rejects_mismatching_real_display_code_and_expired_card(self):
        body = "".join((
            _card("Shopee", "Válido até 09/09/2099", "R$10 OFF",
                  "Cupom: C0RR1D499", "WRONG999"),
            _card("Amazon", "Válido até 01/01/2020", "20% OFF",
                  "Cupom: OLDAMZ20", "OLDAMZ20"),
        ))
        source, rows = self._discover(body)

        self.assertEqual(rows, [])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "displayed_code_mismatch": 1, "expired": 1,
        })

    def test_deduplicates_cards_and_respects_marketplace_filter(self):
        body = "".join((
            _card("Amazon", "Válido por tempo limitado", "10% OFF", "", "AMZCODE10"),
            _card("Amazon", "Válido por tempo limitado", "10% OFF", "", "AMZCODE10"),
            _card("Shopee", "Válido por tempo limitado", "R$10 OFF", "", "SHOPEE10"),
        ))
        source, rows = self._discover(body, marketplaces=["amazon"])

        self.assertEqual([row.coupon_code for row in rows], ["AMZCODE10"])
        self.assertEqual(source.last_metrics["rejected_by_reason"], {
            "duplicate_code": 1, "marketplace_not_requested": 1,
        })

    def test_assigns_each_code_its_own_discount_in_a_shared_list(self):
        description = (
            "Cupons ativos: CODE010: R$10 OFF em R$69; "
            "CODE070: R$70 OFF em R$499"
        )
        body = "".join((
            _card("Shopee", "Expira hoje", "OFERTA", description, "CODE010"),
            _card("Shopee", "Expira hoje", "OFERTA", description, "CODE070"),
        ))
        _source, rows = self._discover(body)

        self.assertEqual({
            row.coupon_code: row.coupon_rules["valor_desconto"] for row in rows
        }, {"CODE010": 10.0, "CODE070": 70.0})
        self.assertEqual({
            row.coupon_code: row.coupon_rules["valor_minimo"] for row in rows
        }, {"CODE010": 69.0, "CODE070": 499.0})

    def test_empty_or_failed_download_is_degraded(self):
        source, rows = self._discover("")
        self.assertEqual(rows, [])
        self.assertEqual(source.last_health_status, "degraded")
        self.assertEqual(source.last_metrics["pages_read"], 0)
