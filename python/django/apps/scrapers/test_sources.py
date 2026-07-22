from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scrapers.models import (
    CanalMonitorado, CupomCodigo, CupomNormalizado, FonteIngestao, Produto,
    ProdutoCupom, Publicacao,
)
from apps.scrapers.ofertas import _melhor_codigo
from apps.scrapers.sources.base import IngestedItem, SourceAdapter
from apps.scrapers.sources.persistence import persist_items
from apps.scrapers.sources import registry


class FakeSource(SourceAdapter):
    slug, marketplace, name = "fake-source", "amazon", "Fake"

    def discover_offers(self, **kwargs):
        return [IngestedItem(
            external_id="B012345678", marketplace="amazon", source=self.slug,
            kind="offer", canonical_url="https://www.amazon.com.br/dp/B012345678",
            title="Fone", current_price=80, reference_price=100,
            observed_at=timezone.now(), evidence={"fixture": True})]


class EmptySource(FakeSource):
    slug, name = "empty-source", "Empty"
    def discover_offers(self, **kwargs):
        return []


class BrokenSource(FakeSource):
    slug, name = "broken-source", "Broken"
    def discover_offers(self, **kwargs):
        raise TimeoutError("timeout")


class SourcePipelineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("source-user")

    def test_normalized_upsert_is_idempotent_and_private(self):
        item = list(FakeSource().discover_offers())[0]
        persist_items([item], owner=self.user)
        persist_items([item], owner=self.user)
        self.assertEqual(Produto.objects.filter(owner=self.user, asin=item.external_id).count(), 1)

    def test_empty_source_preserves_existing_catalog(self):
        item = list(FakeSource().discover_offers())[0]
        persist_items([item], owner=self.user)
        with patch.dict(registry.SOURCES, {"empty-source": EmptySource()}):
            result = registry.run_source("empty-source")
        self.assertEqual(result["status"], "empty")
        self.assertTrue(Produto.objects.filter(owner=self.user).exists())

    def test_source_failure_is_isolated_and_sanitized(self):
        with patch.dict(registry.SOURCES, {"broken-source": BrokenSource()}):
            result = registry.run_source("broken-source")
        self.assertEqual(result["status"], "error")
        state = FonteIngestao.objects.get(slug="broken-source")
        self.assertEqual(state.status, "degraded")
        self.assertNotIn("Traceback", state.erro_publico)

    def test_lock_prevents_duplicate_cycle(self):
        from django.core.cache import cache
        cache.set("ingestion-lock:fake-source", "1", 60)
        with patch.dict(registry.SOURCES, {"fake-source": FakeSource()}):
            result = registry.run_source("fake-source")
        self.assertEqual(result["status"], "running")

    def test_regex_discovered_coupon_is_not_auto_attached(self):
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto", preco_sem_desconto=100,
            preco_com_cupom=80, link_produto="https://produto.example/item")
        CupomCodigo.objects.create(codigo="TESTE10", descricao="cupom ML (checkout)",
                                   valor_desconto=10, ativo=True)
        self.assertIsNone(_melhor_codigo(product))

    @patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper._salvar", return_value=1)
    @patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper.iniciar_browser")
    def test_coupon_page_without_codes_does_not_disable_previous_codes(self, browser, _save):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "Nenhum código visível"
        with patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper._coletar_cards",
                   side_effect=[[{"link_produto": "https://x", "nome": "Oferta"}], [], [], [], []]):
            @contextmanager
            def fake_browser(*args, **kwargs):
                yield page, MagicMock()
            browser.side_effect = fake_browser
            old = CupomCodigo.objects.create(
                codigo="ANTIGO10", descricao="cupom ML (checkout)", ativo=True)
            from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
            mapear_cupons_codigo()
        old.refresh_from_db()
        self.assertTrue(old.ativo)

    def test_only_confirmed_relation_represents_applicability(self):
        source = FonteIngestao.objects.create(slug="coupon-source", marketplace="mercadolivre", nome="Coupons")
        coupon = CupomNormalizado.objects.create(
            fonte=source, external_id="c1", marketplace="mercadolivre",
            titulo="Cupom", codigo="CUPOM10")
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto", preco_sem_desconto=100,
            preco_com_cupom=80, link_produto="https://produto.example/2")
        relation = ProdutoCupom.objects.create(produto=product, cupom=coupon, status="confirmado")
        self.assertEqual(relation.status, "confirmado")

    def test_expired_product_is_not_ranked(self):
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Velho", preco_sem_desconto=100,
            preco_com_cupom=70, link_produto="https://produto.example/old",
            valido_ate=timezone.now() - timedelta(minutes=1))
        from apps.scrapers.ofertas import selecionar_item_para_grupo
        self.assertNotIn(product, selecionar_item_para_grupo(usuario=self.user))

    @override_settings(AFFILIATE_FEED_URL="")
    def test_licensed_feed_is_disabled_without_configuration(self):
        from apps.scrapers.sources.external_feed import LicensedFeedSource
        self.assertEqual(list(LicensedFeedSource().discover_offers()), [])

    @override_settings(
        AFFILIATE_FEED_URL="https://feed.example/coupons.json",
        AFFILIATE_FEED_TOKEN="secret-token",
    )
    @patch("apps.scrapers.sources.external_feed.requests.get")
    def test_licensed_feed_ingests_only_ml_and_amazon_coupons(self, get):
        response = get.return_value
        response.json.return_value = {"items": [
            {
                "type": "coupon", "id": "ml-10", "store": "Mercado Livre",
                "title": "10% em eletrônicos", "code": "ML10",
                "deeplink": "https://afiliado.example/ml?ref=123",
                "discount_type": "percentual", "discount_percent": 10,
                "minimum_purchase": "R$ 100,00", "category": "Eletrônicos",
                "valid_until": "2099-12-31", "network": "Rede Teste",
            },
            {
                "kind": "voucher", "coupon_id": "az-20", "merchant": "Amazon.com.br",
                "description": "R$ 20 de desconto", "voucher_code": "AMAZON20",
                "affiliate_url": "https://afiliado.example/amazon?tag=partner",
                "discount_type": "fixo", "discount_value": "R$ 20",
                "expires_at": "2099-12-31T23:00:00Z",
            },
            {
                "type": "coupon", "id": "other-1", "store": "Outra Loja",
                "code": "OUTRA10", "url": "https://afiliado.example/outra",
            },
            {
                "type": "coupon", "id": "expired", "store": "Amazon",
                "code": "VELHO", "url": "https://afiliado.example/velho",
                "valid_until": "2020-01-01",
            },
        ]}

        from apps.scrapers.sources.external_feed import LicensedFeedSource
        coupons = list(LicensedFeedSource().discover_coupons())

        self.assertEqual([coupon.marketplace for coupon in coupons], ["mercadolivre", "amazon"])
        self.assertEqual(coupons[0].external_id, "licensed:mercadolivre:ml-10")
        self.assertEqual(coupons[0].coupon_rules["tipo_desconto"], "porcentagem")
        self.assertEqual(coupons[0].coupon_rules["valor_desconto"], 10.0)
        self.assertEqual(coupons[0].coupon_rules["valor_minimo"], 100.0)
        self.assertEqual(coupons[0].coupon_rules["escopo"], "Eletrônicos")
        self.assertEqual(coupons[1].canonical_url,
                         "https://afiliado.example/amazon?tag=partner")
        get.assert_called_once_with(
            "https://feed.example/coupons.json",
            headers={
                "Accept": "application/json",
                "User-Agent": "Spreading/1.0 (+affiliate-feed)",
                "Authorization": "Bearer secret-token",
            },
            timeout=20,
        )

    @override_settings(AFFILIATE_FEED_URL="https://feed.example/coupons.json")
    @patch("apps.scrapers.sources.external_feed.requests.get")
    def test_licensed_coupon_requires_code_and_http_deeplink(self, get):
        get.return_value.json.return_value = [
            {"type": "coupon", "store": "Amazon", "code": "", "url": "https://ok.example"},
            {"type": "coupon", "store": "Amazon", "code": "TESTE", "url": "javascript:alert(1)"},
        ]
        from apps.scrapers.sources.external_feed import LicensedFeedSource
        self.assertEqual(list(LicensedFeedSource().discover_coupons()), [])

    @override_settings(AFFILIATE_FEED_URL="https://feed.example/coupons.json")
    @patch("apps.scrapers.sources.persistence.persist_items")
    @patch("apps.scrapers.sources.run_source")
    def test_configured_feed_is_enabled_and_persists_coupons(self, run_source, persist):
        source = FonteIngestao.objects.get(slug="licensed-affiliate-feed")
        self.assertFalse(source.habilitada)
        coupon = IngestedItem(
            external_id="licensed:amazon:1", marketplace="amazon",
            source="licensed-affiliate-feed", kind="coupon",
            canonical_url="https://affiliate.example/amazon", title="Cupom Amazon",
            coupon_code="AMAZON10",
        )
        run_source.return_value = {"offers": [], "coupons": [coupon], "status": "ok"}
        persist.return_value = {"offers": 0, "coupons": 1}

        from apps.scrapers.management.commands.automacao import _rodar_feed_afiliados
        result = _rodar_feed_afiliados()

        source.refresh_from_db()
        self.assertTrue(source.habilitada)
        persist.assert_called_once_with([coupon])
        self.assertEqual(result, {"offers": 0, "coupons": 1})

    @override_settings(AMAZON_PARTNER_TAG="globaltag-20", AMAZON_PUBLIC_FALLBACK=True)
    @patch("apps.scrapers.sources.persistence.persist_items")
    @patch("apps.scrapers.sources.run_source")
    def test_global_amazon_tag_is_not_inherited_by_users(self, run_source, persist):
        run_source.return_value = {"offers": [], "coupons": [], "status": "empty"}
        from apps.scrapers.marketplaces.amazon import Amazon
        Amazon().scrape_all(termos=["fone"])
        run_source.assert_not_called()
        persist.assert_not_called()

    @override_settings(AMAZON_PARTNER_TAG="globaltag-20", AFILIADO_EXIGIR=True)
    @patch("apps.scrapers.sources.amazon_public.verify_product_url",
           return_value={"ok": True, "titulo": "Fone", "preco": 80})
    def test_amazon_public_offer_completes_dry_run_publication(self, _verify):
        self.user.perfil.afiliado_tag_amazon = "usertag-20"
        self.user.perfil.save(update_fields=["afiliado_tag_amazon"])
        product = Produto.objects.create(
            owner=self.user, marketplace="amazon", asin="B012345678",
            fonte="amazon-public-web", origem="oferta", nome="Fone",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://www.amazon.com.br/dp/B012345678")
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        result = enviar_oferta_de_produto(
            product, "dry-run", verificar=True, dry_run=True, usuario=self.user)
        self.assertTrue(result["sucesso"])
        self.assertIn("tag=usertag-20", result["link"])
        self.assertEqual(Publicacao.objects.get(produto=product).status, "ignorado")

    def test_curated_channel_does_not_advance_cursor_when_send_fails(self):
        channel = CanalMonitorado.objects.create(
            owner=self.user, handle="@fonte", destino_grupo_id="destino", ultimo_id=0)

        class Message:
            id = 10
            message = "Oferta https://www.amazon.com.br/dp/B012345678"

        class Client:
            def iter_messages(self, *args, **kwargs):
                return [Message()]

        class Sender:
            def enviar_oferta(self, *args, **kwargs):
                return {"sucesso": False, "erro": "offline"}

        from apps.scrapers.management.commands.monitorar_canais import Command
        with self.assertRaises(RuntimeError):
            Command()._processar_canal(
                Client(), channel,
                __import__("apps.scrapers.models", fromlist=["EnvioCanal"]).EnvioCanal,
                lambda text, user: (text, ["hash"]), lambda name: Sender(),
                lambda text: [("url", "amazon")])
        channel.refresh_from_db()
        self.assertEqual(channel.ultimo_id, 0)

    @override_settings(AFFILIATE_FEED_URL="")
    @patch("apps.scrapers.maintenance.expire_stale")
    @patch("apps.scrapers.management.commands.automacao.st.write_state")
    def test_full_cycle_degrades_gracefully_when_one_marketplace_fails(self, _state, expire):
        class Good:
            def scrape_all(self, **kwargs):
                return None
        class Bad:
            def scrape_all(self, **kwargs):
                raise RuntimeError("offline")
        from apps.scrapers.marketplaces import registry as marketplaces
        from apps.scrapers.management.commands.automacao import _rodar_scrape
        with patch.object(marketplaces, "MARKETPLACES", {
                "mercadolivre": Good(), "amazon": Bad()}):
            result = _rodar_scrape()
        self.assertEqual(result, {"sucessos": 1, "falhas": ["amazon"]})
        expire.assert_called_once()
