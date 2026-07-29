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

    def test_amazon_coupon_source_groups_asins_and_preserves_final_price(self):
        from apps.scrapers.sources.amazon_coupons import AmazonCouponsSource

        source = AmazonCouponsSource()
        source._cache_at = __import__("time").monotonic()
        source._cache = [
            {
                "asin": "B012345678", "promo_id": "PROMO1", "title": "Cafeteira",
                "url": "https://www.amazon.com.br/dp/B012345678",
                "image_url": "https://m.media-amazon.com/a.jpg",
                "current": 100.0, "reference": 120.0, "final": 90.0,
                "discount": 10.0,
            },
            {
                "asin": "B087654321", "promo_id": "PROMO1", "title": "Coifa",
                "url": "https://www.amazon.com.br/dp/B087654321",
                "image_url": "https://m.media-amazon.com/b.jpg",
                "current": 200.0, "reference": 200.0, "final": 180.0,
                "discount": 10.0,
            },
        ]

        offers = list(source.discover_offers())
        coupons = list(source.discover_coupons())

        self.assertEqual(len(offers), 2)
        self.assertEqual(offers[0].image_url, "https://m.media-amazon.com/a.jpg")
        self.assertEqual(offers[0].evidence["coupon_final_price"], 90.0)
        # A vitrine e o preço pago viajam em campos distintos: 'current' alimenta
        # os gates de desconto, 'effective' é o que a mensagem publica.
        self.assertEqual(offers[0].current_price, 100.0)
        self.assertEqual(offers[0].effective_price, 90.0)
        persist_items([offers[0]], owner=self.user)
        produto = Produto.objects.get(owner=self.user, asin="B012345678")
        self.assertEqual(produto.preco_com_cupom, 100.0)
        self.assertEqual(produto.preco_efetivo, 90.0)
        self.assertEqual(len(coupons), 1)
        self.assertEqual(coupons[0].evidence["asins"], ["B012345678", "B087654321"])
        self.assertEqual(coupons[0].coupon_rules["modo_resgate"], "ativacao")

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
    @patch("apps.scrapers.coupon_products.preparar_lote",
           return_value={"processados": 0, "prontos": 0})
    @patch("apps.scrapers.scraper_mercadolivre.cupons_container.casar_cupons_container",
           return_value=0)
    @patch("apps.scrapers.maintenance.expire_stale")
    @patch("apps.scrapers.management.commands.automacao.st.write_state")
    def test_full_cycle_degrades_gracefully_when_one_marketplace_fails(
        self, _state, expire, _containers, _preparo
    ):
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


class CouponPagePayloadTests(TestCase):
    """A página /ofertas/cupons virou 'smart-coupon' renderizado no cliente: o DOM
    hidratado não tem mais `.poly-card`, então a coleta tem de vir do payload SSR."""

    # Recorte fiel de um cupom do carrossel oficial (campos e grafia reais).
    CUPOM = {
        "campaign_id": "13975432",
        "title": {"text": "60% OFF"},
        "category": "Itens para Casa",
        "benefit_mode": "PERCENT",
        "status": {"id": "ACTIVE"},
        "expiration_date": "2026-08-31T02:59:00Z",
        "amount": {"min_amount": "Compra mínima R$ 1.099",
                   "cap_amount": "Limite de desconto R$ 63"},
        "action": {"type": "link", "value": (
            "https://lista.mercadolivre.com.br/_Container_13975432"
            "?coupon_campaign_id=13975432#navigation_id=coupons-carousel-1_x")},
        "segmentations": {"container": {"id": "1708749", "name": "13975432"},
                          "total_items": 30},
    }

    def _html(self, *cupons):
        import json
        payload = {"appProps": {"pageProps": {"floxPreloadedState": {
            "@meli/web/flox/FLOX_STATE": {"brickStack": {
                # A chave carrega um timestamp que muda a cada render — o parser
                # não pode depender dela.
                "coupons-carousel-1780432282667": {"data": {"coupons": list(cupons)}},
            }}}}}}
        return ('<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r='
                + json.dumps(payload)
                + ';_n.ctx.r.assets.manifest=new Map([]);</script>')

    def test_payload_yields_coupon_with_container_url(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
            _cupons_do_payload, _normalizar_cupom, _payload_nordic,
        )
        dados = _payload_nordic(self._html(self.CUPOM))
        brutos = _cupons_do_payload(dados)
        self.assertEqual(len(brutos), 1)

        cupom = _normalizar_cupom(brutos[0])
        self.assertEqual(cupom["campanha_id"], "13975432")
        self.assertEqual(cupom["tipo_desconto"], "porcentagem")
        self.assertEqual(cupom["valor_desconto"], 60.0)
        self.assertEqual(cupom["valor_minimo"], 1099.0)   # "R$ 1.099" é milhar, não 1,099
        self.assertEqual(cupom["desconto_maximo"], 63.0)
        # O fragmento de telemetria do carrossel não pode entrar na URL do container:
        # é ele que casar_cupons_container abre para descobrir os produtos.
        self.assertEqual(
            cupom["container_url"],
            "https://lista.mercadolivre.com.br/_Container_13975432?coupon_campaign_id=13975432")

    def test_finished_or_non_listing_coupons_are_dropped(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import _normalizar_cupom
        self.assertIsNone(_normalizar_cupom({**self.CUPOM, "status": {"id": "FINISHED"}}))
        # Vitrine social não é lista de produtos: publicar levaria a clique sem desconto.
        self.assertIsNone(_normalizar_cupom({**self.CUPOM, "action": {
            "type": "link", "value": "https://www.mercadolivre.com.br/social/loja"}}))

    def test_malformed_page_never_raises(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import _payload_nordic
        self.assertIsNone(_payload_nordic("<html>sem script</html>"))
        self.assertIsNone(_payload_nordic(object()))
        self.assertIsNone(_payload_nordic(
            '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={quebrado;_n.ctx.r.assets</script>'))

    def test_coupons_are_persisted_with_container_rules(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
            _cupons_do_payload, _normalizar_cupom, _payload_nordic, _salvar_cupons_smart,
        )
        brutos = _cupons_do_payload(_payload_nordic(self._html(self.CUPOM)))
        total = _salvar_cupons_smart([_normalizar_cupom(b) for b in brutos])

        self.assertEqual(total, 1)
        cupom = CupomNormalizado.objects.get(external_id="campanha:13975432")
        self.assertEqual(cupom.estado, "ativo")
        # Token opaco de ativação nunca vira "código para digitar no checkout".
        self.assertEqual(cupom.codigo, "")
        self.assertEqual(cupom.regras["modo_resgate"], "ativacao")
        self.assertFalse(cupom.regras["is_mar_aberto"])
        self.assertTrue(cupom.regras["container_url"].endswith("coupon_campaign_id=13975432"))


class OfferFeedPaginationTests(TestCase):
    """O feed /ofertas tem ~40 páginas cheias; uma página em branco é quase sempre
    challenge do anti-bot, não fim do catálogo."""

    def _rodar(self, paginas_de_cards, max_paginas=6):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        from apps.scrapers.scraper_mercadolivre import ofertas_scraper as mod

        @contextmanager
        def fake_browser(*a, **kw):
            yield MagicMock(), MagicMock()

        with patch.object(mod, "iniciar_browser", fake_browser), \
             patch.object(mod, "pausa_humana"), \
             patch.object(mod, "storage_state", return_value=None), \
             patch.object(mod, "_coletar_cards", side_effect=paginas_de_cards), \
             patch.object(mod, "_salvar", side_effect=lambda c, **kw: len(c)) as salvar:
            total = mod.mapear_ofertas(max_paginas=max_paginas)
        return total, salvar

    def _card(self, i):
        return {"link_produto": f"https://ml/{i}", "nome": f"Item {i}"}

    def test_single_blank_page_does_not_truncate_the_feed(self):
        # Antes: a página 2 vazia encerrava tudo e o feed vinha com 1 item.
        paginas = [[self._card(1)], [], [self._card(2)], [self._card(3)],
                   [self._card(4)], [self._card(5)]]
        total, _ = self._rodar(paginas)
        self.assertEqual(total, 5)

    def test_three_blank_pages_in_a_row_end_the_scan(self):
        paginas = [[self._card(1)], [], [], [], [self._card(9)], [self._card(10)]]
        total, salvar = self._rodar(paginas)
        self.assertEqual(total, 1)
        # Parou na 4ª página: não gastou as duas restantes.
        self.assertEqual(len(salvar.call_args[0][0]), 1)


class AmazonDiscountRecoveryTests(TestCase):
    """`savingBasis` é opcional no catalog/v1; sem reconstruir o 'De:' o feed
    descartava ofertas que a própria API já filtrou por minSavingPercent."""

    def _item(self, price):
        return {"asin": "B0RECOVER1",
                "itemInfo": {"title": {"displayValue": "Fone"}},
                "offersV2": {"listings": [{"price": price}]}}

    def test_percentage_only_item_keeps_its_discount(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        m = az._mapear_item(self._item({"money": {"amount": 80},
                                        "savings": {"percentage": 20}}))
        self.assertEqual(m["preco_com_cupom"], 80)
        self.assertEqual(round(m["preco_sem_desconto"], 2), 100.0)

    def test_absolute_savings_item_keeps_its_discount(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        m = az._mapear_item(self._item({"money": {"amount": 80},
                                        "savings": {"money": {"amount": 20}}}))
        self.assertEqual(m["preco_sem_desconto"], 100)

    def test_item_without_any_discount_signal_stays_flat(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        m = az._mapear_item(self._item({"money": {"amount": 80}}))
        self.assertEqual(m["preco_sem_desconto"], m["preco_com_cupom"])

    def test_saving_basis_still_wins_when_present(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        m = az._mapear_item(self._item({"money": {"amount": 80},
                                        "savingBasis": {"money": {"amount": 100}},
                                        "savings": {"percentage": 20}}))
        self.assertEqual(m["preco_sem_desconto"], 100)


class VerificacaoDeLinksEhLanePropriaTests(TestCase):
    """A verificação NÃO pode depender de haver link novo para gerar.

    Era um passageiro de _rodar_links: ficava depois do `if not pendentes: continue`,
    então bastava a fila de geração esvaziar (todo produto já com link) para a
    verificação nunca mais rodar. Em homologação isso deixou 287 links gerados com
    6 verificados — e a tela de Promoções só lista item com verificado_ok=True.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("verificador", password="test")
        self.user.perfil.marcar_verificado()

    @patch("apps.scrapers.scraper_mercadolivre.link.verificar_links_pendentes")
    def test_verifica_mesmo_sem_nenhum_produto_para_gerar(self, verificar):
        """O caso exato do bug: fila de geração vazia."""
        from apps.scrapers.management.commands.automacao import _rodar_verificacao_links
        verificar.return_value = {"aprovados": 3, "reprovados": 1, "transitorios": 0}

        total = _rodar_verificacao_links(limite=40)

        verificar.assert_called_once()
        self.assertEqual(total["aprovados"], 3)

    @patch("apps.scrapers.scraper_mercadolivre.link.verificar_links_pendentes")
    def test_nao_exige_sessao_do_mercado_livre(self, verificar):
        """Sem sessão ML o usuário ainda tem centenas de links esperando veredito.
        A verificação abre a página pública do destino — exigir sessão os manteria
        invisíveis sem motivo (a geração é que precisa do Link Builder logado)."""
        from apps.scrapers.management.commands.automacao import _rodar_verificacao_links
        verificar.return_value = {"aprovados": 1, "reprovados": 0, "transitorios": 0}

        with patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False):
            total = _rodar_verificacao_links(limite=40)

        verificar.assert_called_once()
        self.assertEqual(total["aprovados"], 1)

    @patch("apps.scrapers.scraper_mercadolivre.link.verificar_links_pendentes")
    def test_falha_de_um_usuario_nao_derruba_os_outros(self, verificar):
        from apps.scrapers.management.commands.automacao import _rodar_verificacao_links
        outro = get_user_model().objects.create_user("outro-verif", password="test")
        outro.perfil.marcar_verificado()
        verificar.side_effect = [
            RuntimeError("browser caiu"),
            {"aprovados": 2, "reprovados": 0, "transitorios": 0},
        ]

        total = _rodar_verificacao_links(limite=40)

        self.assertEqual(verificar.call_count, 2)
        self.assertEqual(total["aprovados"], 2)

    def test_geracao_nao_verifica_mais(self):
        """_rodar_links cuida só de gerar; quem verifica é a lane própria. Se alguém
        reintroduzir a chamada lá dentro, o acoplamento volta."""
        import inspect
        from apps.scrapers.management.commands import automacao
        fonte = inspect.getsource(automacao._rodar_links)
        self.assertNotIn("verificar_links_pendentes", fonte)
