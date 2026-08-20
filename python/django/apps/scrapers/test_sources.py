from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
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

    def test_fonte_publica_nao_apaga_a_categoria_que_a_creators_api_classificou(self):
        """O caminho público desfazia a classificação do browseNodeInfo.

        As duas coletas caem na MESMA linha (marketplace + owner + asin). A pública
        não conhece a categoria e gravava 'DESCONHECIDO' como constante, então bastava
        um ciclo público depois da Creators API para o produto voltar a ficar invisível
        no filtro de subcategoria da vitrine, que exclui esse valor.
        """
        item = list(FakeSource().discover_offers())[0]
        Produto.objects.create(
            marketplace="amazon", owner=self.user, asin=item.external_id,
            nome="Fone", categoria="Fones de Ouvido",
            macro_categoria="Áudio, Vídeo e Fotografia",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto=item.canonical_url,
        )

        persist_items([item], owner=self.user)

        produto = Produto.objects.get(owner=self.user, asin=item.external_id)
        self.assertEqual(produto.categoria, "Fones de Ouvido")
        self.assertEqual(produto.macro_categoria, "Áudio, Vídeo e Fotografia")

    def test_fonte_que_conhece_a_categoria_grava_e_atualiza(self):
        """Quem tem o sinal manda: `category` vence o que já estava na linha."""
        item = list(FakeSource().discover_offers())[0]
        persist_items([item], owner=self.user)

        classificado = IngestedItem(**{**item.__dict__, "category": "Fones de Ouvido"})
        persist_items([classificado], owner=self.user)

        produto = Produto.objects.get(owner=self.user, asin=item.external_id)
        self.assertEqual(produto.categoria, "Fones de Ouvido")

    def test_produto_novo_sem_sinal_nasce_como_desconhecido(self):
        """'Ninguém classificou ainda' continua sendo DESCONHECIDO, não vazio."""
        item = list(FakeSource().discover_offers())[0]
        persist_items([item], owner=self.user)

        produto = Produto.objects.get(owner=self.user, asin=item.external_id)
        self.assertEqual(produto.categoria, "DESCONHECIDO")

    def test_empty_source_preserves_existing_catalog(self):
        item = list(FakeSource().discover_offers())[0]
        persist_items([item], owner=self.user)
        with patch.dict(registry.SOURCES, {"empty-source": EmptySource()}):
            result = registry.run_source("empty-source")
        self.assertEqual(result["status"], "empty")
        self.assertTrue(Produto.objects.filter(owner=self.user).exists())

    def test_source_failure_is_isolated_and_sanitized(self):
        broken = BrokenSource()
        broken.last_health_status = "blocked"
        broken.last_metrics = {
            "duration_ms": 17,
            "duration_by_stage_ms": {"navigation": 11, "parsing": 6},
            "schema_fingerprint": "a" * 64,
        }
        with patch.dict(registry.SOURCES, {"broken-source": broken}):
            result = registry.run_source("broken-source")
        self.assertEqual(result["status"], "error")
        state = FonteIngestao.objects.get(slug="broken-source")
        self.assertEqual(state.status, "degraded")
        self.assertNotIn("Traceback", state.erro_publico)
        run = state.execucoes.latest("pk")
        self.assertEqual(run.health_status, "blocked")
        self.assertEqual(run.duracoes, {
            "navigation": 11, "parsing": 6, "total_ms": 17,
        })
        self.assertEqual(run.schema_fingerprint, "a" * 64)

    def test_source_com_itens_mas_inventario_parcial_fica_degradada(self):
        partial = FakeSource()
        partial.slug = "partial-source"
        partial.name = "Partial"
        partial.last_health_status = "partial"
        partial.last_metrics = {
            "complete": False, "stop_reason": "max_pages", "duration_ms": 9,
        }
        with patch.dict(registry.SOURCES, {"partial-source": partial}):
            result = registry.run_source("partial-source")

        self.assertEqual(result["status"], "degraded")
        state = FonteIngestao.objects.get(slug="partial-source")
        self.assertEqual(state.status, "degraded")
        self.assertIsNone(state.ultimo_sucesso)
        self.assertIn("parcial", state.erro_publico.lower())

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
                                   valor_desconto=10, ativo=True, automatico=True)
        self.assertIsNone(_melhor_codigo(product))

    def test_regex_discovered_coupon_stays_blocked_after_manual_edit(self):
        """Renomear a descrição não pode reabilitar um código sem vínculo provado."""
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto", preco_sem_desconto=100,
            preco_com_cupom=80, link_produto="https://produto.example/outro")
        CupomCodigo.objects.create(codigo="TESTE20", descricao="Meu cupom favorito",
                                   valor_desconto=20, ativo=True, automatico=True)
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
        # A reescrita agora devolve também os pares (origem, afiliada) que o portão de
        # verificação consome. O portão é liberado de propósito aqui: o que este teste
        # mede é o cursor diante de uma FALHA DE ENVIO, não a conferência do destino.
        alvo = "https://www.amazon.com.br/dp/B012345678"
        with patch("apps.scrapers.canais.validacao.mensagem_liberada",
                   return_value=(True, "aprovado", "")), \
                self.assertRaises(RuntimeError):
            Command()._processar_canal(
                Client(), channel,
                __import__("apps.scrapers.models", fromlist=["EnvioCanal"]).EnvioCanal,
                lambda text, user: (text, ["hash"], [(alvo, f"{alvo}?tag=x")]),
                lambda name: Sender(),
                lambda text: [("url", "amazon")])
        channel.refresh_from_db()
        self.assertEqual(channel.ultimo_id, 0)

    def test_curated_channel_does_not_send_when_destination_is_rejected(self):
        """Oferta reprovada no destino não sai — é a reputação de quem assina."""
        channel = CanalMonitorado.objects.create(
            owner=self.user, handle="@fonte", destino_grupo_id="destino", ultimo_id=0)

        class Message:
            id = 11
            message = "Oferta https://www.amazon.com.br/dp/B012345678"

        class Client:
            def iter_messages(self, *args, **kwargs):
                return [Message()]

        enviadas = []

        class Sender:
            def enviar_oferta(self, *args, **kwargs):
                enviadas.append(kwargs)
                return {"sucesso": True}

        from apps.scrapers.management.commands.monitorar_canais import Command
        alvo = "https://www.amazon.com.br/dp/B012345678"
        with patch("apps.scrapers.canais.validacao.mensagem_liberada",
                   return_value=(False, "reprovado", "Produto indisponível")):
            Command()._processar_canal(
                Client(), channel,
                __import__("apps.scrapers.models", fromlist=["EnvioCanal"]).EnvioCanal,
                lambda text, user: (text, ["hash"], [(alvo, f"{alvo}?tag=x")]),
                lambda name: Sender(),
                lambda text: [("url", "amazon")])
        self.assertEqual(enviadas, [], "Mensagem reprovada não pode ser enviada.")
        channel.refresh_from_db()
        # Reprovada é definitiva: o cursor avança para não reprocessar para sempre.
        self.assertEqual(channel.ultimo_id, 11)

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
        self.assertEqual(
            result, {"sucessos": 1, "falhas": ["amazon"], "adiadas": []})
        expire.assert_called_once()

    @override_settings(AFFILIATE_FEED_URL="")
    @patch("apps.scrapers.coupon_products.preparar_lote",
           return_value={"processados": 0, "prontos": 0})
    @patch("apps.scrapers.scraper_mercadolivre.cupons_container.casar_cupons_container",
           return_value=0)
    @patch("apps.scrapers.maintenance.expire_stale")
    @patch("apps.scrapers.management.commands.automacao.st.write_state")
    def test_loja_adiada_por_navegador_ocupado_nao_e_fonte_quebrada(
        self, _state, expire, _containers, _preparo
    ):
        """Disputa de capacidade não pode virar incidente de fonte.

        O ciclo deixou de reservar o Chromium durante as lojas que só falam HTTP,
        então a loja que ABRE navegador pode agora encontrá-lo ocupado — situação
        impossível enquanto o lease do ciclo inteiro era o próprio `django_chromium`.
        Se isso caísse no `except Exception` genérico, cada disputa marcaria as
        fontes do Mercado Livre como `degraded` e gravaria um evento `error`: alarme
        falso sobre uma fila que se resolve sozinha no ciclo seguinte.
        """
        from apps.scrapers.carga import BrowserResourceUnavailable
        from apps.scrapers.management.commands.automacao import _rodar_scrape
        from apps.scrapers.marketplaces import registry as marketplaces
        from apps.scrapers.models import EventoOperacional, FonteIngestao

        class Http:
            def scrape_all(self, **kwargs):
                return None

        class SemNavegador:
            def scrape_all(self, **kwargs):
                raise BrowserResourceUnavailable("capacidade ocupada")

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"},
        )
        FonteIngestao.objects.filter(pk=fonte.pk).update(
            marketplace="mercadolivre", habilitada=True, status="ok")
        with patch.object(marketplaces, "MARKETPLACES", {
                "mercadolivre": SemNavegador(), "amazon": Http()}):
            result = _rodar_scrape()

        self.assertEqual(result["adiadas"], ["mercadolivre"])
        self.assertEqual(result["falhas"], [])
        # Adiada não conta como sucesso: quem só foi adiado não coletou nada.
        self.assertEqual(result["sucessos"], 1)
        self.assertEqual(
            FonteIngestao.objects.get(slug="mercadolivre-web").status, "ok")
        self.assertFalse(
            EventoOperacional.objects.filter(evento="fonte_falhou").exists())

    @override_settings(AFFILIATE_FEED_URL="")
    @patch("apps.scrapers.coupon_products.preparar_lote",
           return_value={"processados": 0, "prontos": 0})
    @patch("apps.scrapers.scraper_mercadolivre.cupons_container.casar_cupons_container",
           return_value=0)
    @patch("apps.scrapers.maintenance.expire_stale")
    @patch("apps.scrapers.management.commands.automacao.st.write_state")
    def test_ciclo_inteiro_adiado_nao_grita_que_tudo_falhou(
        self, _state, expire, _containers, _preparo
    ):
        from apps.scrapers.carga import BrowserResourceUnavailable
        from apps.scrapers.management.commands.automacao import _rodar_scrape
        from apps.scrapers.marketplaces import registry as marketplaces

        class SemNavegador:
            def scrape_all(self, **kwargs):
                raise BrowserResourceUnavailable("capacidade ocupada")

        with patch.object(marketplaces, "MARKETPLACES", {
                "mercadolivre": SemNavegador(), "amazon": SemNavegador()}):
            result = _rodar_scrape()  # não levanta

        self.assertEqual(result["sucessos"], 0)
        self.assertEqual(sorted(result["adiadas"]), ["amazon", "mercadolivre"])
        # Nada foi coletado: não há o que expirar por idade neste ciclo.
        expire.assert_not_called()


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

    def test_condicao_de_publico_e_persistida(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
            _normalizar_cupom, _salvar_cupons_smart,
        )

        bruto = {
            **self.CUPOM,
            "campaign_id": "restrito-app",
            "title": {"text": "20% OFF somente no app para novos clientes"},
        }
        normalizado = _normalizar_cupom(bruto)
        self.assertTrue(normalizado["restrito"])
        _salvar_cupons_smart([normalizado])
        self.assertTrue(
            CupomNormalizado.objects.get(external_id="campanha:restrito-app").restrito,
        )

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

    def setUp(self):
        # A varredura é retomável e guarda no estado do worker a página em que
        # parou. Estes testes contam páginas a partir da PRIMEIRA, então precisam de
        # um estado próprio: sem isso, um teste anterior que cedeu o navegador na
        # página 3 fazia a contagem começar de lá (`3 != 5`).
        import tempfile

        from apps.scrapers import automacao_state

        estado = tempfile.TemporaryDirectory()
        self.addCleanup(estado.cleanup)
        remendo = patch.object(automacao_state, "_DIR", estado.name)
        remendo.start()
        self.addCleanup(remendo.stop)

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

    def test_rejects_saving_basis_at_ten_times_price_without_percentage(self):
        """Razão 10x é 90% falso no topo; a guarda antiga usava `>` e deixava
        passar exatamente os exemplos corrompidos encontrados em produção."""
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        m = az._mapear_item(self._item({
            "money": {"amount": 63.99},
            "savingBasis": {"money": {"amount": 639.90}},
        }))

        self.assertIsNone(m)

    def test_rejects_saving_basis_that_disagrees_with_api_percentage(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        m = az._mapear_item(self._item({
            "money": {"amount": 63.99},
            "savingBasis": {"money": {"amount": 63990}},
            "savings": {"percentage": 20},
        }))

        self.assertIsNone(m)


class AmazonPublicPriceSanityTests(TestCase):
    def test_accepts_plausible_discount(self):
        from apps.scrapers.sources.amazon_public import _precos_publicaveis

        self.assertTrue(_precos_publicaveis(80, 100))

    def test_rejects_exactly_ninety_percent(self):
        from apps.scrapers.sources.amazon_public import _precos_publicaveis

        self.assertFalse(_precos_publicaveis(63.99, 639.90))

    def test_rejects_reference_price_in_wrong_scale(self):
        from apps.scrapers.sources.amazon_public import _precos_publicaveis

        self.assertFalse(_precos_publicaveis(63.99, 63990))


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


class GerarLinksNaoSeguraTransacaoTests(TestCase):
    """O job de gerar links passa MINUTOS no Link Builder sem tocar no banco.

    Se o tenant for instalado com `transaction.atomic()` aberto (o padrão de
    organization_context), essa transação fica `idle in transaction` o tempo todo e
    o proxy da Fly derruba o socket. E o pior: dentro de uma transação o Django NÃO
    fecha a conexão — só marca `closed_in_transaction` — então nenhuma tentativa de
    renovar tem efeito, e toda query seguinte estoura "the connection is closed".

    A saída é a mesma que o live view do ML já usava: escopo apenas anotado, e cada
    ida ao banco reinstalando o tenant numa transação de milissegundos.
    """

    def test_job_de_links_nao_segura_transacao(self):
        import inspect
        from apps.scrapers import views
        fonte = inspect.getsource(views.gerar_links_stream)
        self.assertIn("segurar_transacao=False", fonte)

    def test_idas_ao_banco_passam_por_executar_no_tenant(self):
        """Sem transação não há GUC de tenant: leitura direta seria filtrada pela
        RLS e devolveria zero produto."""
        import inspect
        from apps.scrapers import views
        fonte = inspect.getsource(views.gerar_links_stream)
        codigo = " ".join(l for l in fonte.split("\n")
                          if not l.strip().startswith("#")).split()
        codigo = " ".join(codigo)   # normaliza quebras de linha da chamada
        self.assertIn("executar_no_tenant( _produtos_sem_link", codigo)
        self.assertIn("executar_no_tenant(frase_resumo_afiliacao", codigo)

    def test_modo_sem_transacao_apenas_anota_o_escopo(self):
        """No modo sem transação o tenant fica SUSPENSO, não instalado: nenhum GUC
        e nenhuma transação presos durante os minutos de browser. Quem grava o
        reinstala via executar_no_tenant.

        (Não dá para medir `in_atomic_block` aqui: o próprio TestCase envolve tudo
        numa transação. O que se observa é o modo do escopo.)"""
        from apps.accounts.tenant import (
            current_organization_id, organization_callable, _tenant_suspenso,
        )
        from apps.accounts.models import organization_for_user

        user = get_user_model().objects.create_user("semtrans", password="test")
        org = organization_for_user(user)
        visto = {}

        def _corpo():
            visto["instalado"] = current_organization_id()
            visto["suspenso"] = (_tenant_suspenso.get() or (None, None))[0]

        organization_callable(org.pk, _corpo, segurar_transacao=False)()
        self.assertIsNone(visto["instalado"])
        self.assertEqual(visto["suspenso"], str(org.pk))

        organization_callable(org.pk, _corpo, segurar_transacao=True)()
        self.assertEqual(visto["instalado"], str(org.pk))

    def test_alvo_de_thread_devolve_a_conexao_ao_terminar(self):
        """Thread que morre não fecha a conexão dela, e `close_old_connections`
        também não: com CONN_MAX_AGE=600 ela não está velha, só órfã. Sem isto,
        cada requisição SSE deixava uma conexão pendurada no Postgres.

        (O fechamento em si não é observável aqui: o sqlite em memória dos testes
        ignora `close()` de propósito, para não destruir o banco. O que se verifica
        é que o alvo de thread pede o fechamento e o `organization_callable` cru,
        usado em linha dentro da transação do chamador, não pede.)
        """
        import threading
        from django.db import connections
        from apps.accounts.models import organization_for_user
        from apps.accounts.tenant import (
            organization_callable, organization_thread_target,
        )

        user = get_user_model().objects.create_user("threadconn", password="test")
        org = organization_for_user(user)

        def _corpo():
            connections["default"].ensure_connection()

        with patch.object(connections, "close_all") as fechar:
            thread = threading.Thread(
                target=organization_thread_target(org.pk, _corpo), daemon=True)
            thread.start()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        fechar.assert_called_once_with()

        with patch.object(connections, "close_all") as fechar_em_linha:
            organization_callable(org.pk, _corpo)()
        fechar_em_linha.assert_not_called()


class AvisoCuponsNaoSeguraTransacaoTests(TestCase):
    """O aviso de cupons gera o link da mensagem no Link Builder (Chromium),
    minutos sem tocar no banco — o mesmo veneno do job de gerar links: tenant
    instalado com transação aberta vira `idle in transaction`, o proxy da Fly
    derruba o socket e a query seguinte estoura "the connection is closed"
    (foi o OperationalError do disparo manual em produção). A saída é a mesma:
    escopo anotado (segurar_transacao=False) e ORM sempre via executar_no_tenant.
    """

    def test_view_do_aviso_nao_segura_transacao(self):
        import inspect
        from apps.scrapers import views
        fonte = inspect.getsource(views.enviar_aviso_cupons_stream)
        self.assertIn("segurar_transacao=False", fonte)

    def test_leituras_da_view_passam_por_executar_no_tenant(self):
        """Sem transação não há GUC de tenant: leitura direta seria filtrada pela
        RLS e o aviso diria "nenhum cupom novo" havendo cupons."""
        import inspect
        from apps.scrapers import views
        fonte = inspect.getsource(views.enviar_aviso_cupons_stream)
        codigo = " ".join(l for l in fonte.split("\n")
                          if not l.strip().startswith("#")).split()
        codigo = " ".join(codigo)   # normaliza quebras de linha da chamada
        self.assertIn("executar_no_tenant( selecionar_cupons_para_aviso", codigo)

    def test_nucleo_do_aviso_isola_o_orm_da_transacao_longa(self):
        """O núcleo é chamado também pelo worker (role de sistema, sem escopo):
        por isso o ORM dele passa por _executar_orm, que instala o tenant quando
        há escopo anotado e cai no caminho direto quando a role dispensa GUC."""
        import inspect
        from apps.scrapers import ofertas
        fonte = inspect.getsource(ofertas.enviar_aviso_cupons)
        self.assertIn("_executar_orm(_reservar)", fonte)
        self.assertIn("_executar_orm(wa_session_de, usuario)", fonte)
        fonte_resolver = inspect.getsource(ofertas.resolver_link_afiliado_cupom)
        self.assertIn("_executar_orm(_produto_para_cupom, cupom)", fonte_resolver)


class EnviosManuaisNaoSeguramTransacaoTests(TestCase):
    """Os botões "enviar produto" e "enviar cupom" têm o mesmo veneno do aviso:
    Chromium do Link Builder no meio do job. Com o tenant instalado do jeito
    antigo (transação aberta do início ao fim), a conexão morre no meio e o
    envio inteiro falha com "the connection is closed"."""

    def test_views_de_envio_nao_seguram_transacao(self):
        import inspect
        from apps.scrapers import views
        for view in (views.enviar_produto_stream, views.enviar_cupom_stream):
            with self.subTest(view=view.__name__):
                self.assertIn("segurar_transacao=False", inspect.getsource(view))

    def test_nucleos_de_envio_isolam_o_orm_da_transacao_longa(self):
        import inspect
        from apps.scrapers import ofertas
        for nucleo in (ofertas.enviar_cupom, ofertas.enviar_oferta_de_produto):
            with self.subTest(nucleo=nucleo.__name__):
                fonte = inspect.getsource(nucleo)
                self.assertIn("_executar_orm(_reservar)", fonte)
                self.assertIn("_executar_orm(wa_session_de, usuario)", fonte)

    def test_banco_nao_tolera_lock_nem_transacao_ociosa_eternos(self):
        """Sem lock_timeout, uma transação abandonada derrubava o site inteiro
        (fila de lock prendendo todas as threads do gunicorn). O release command
        (migrate) fica de fora: DDL espera lock legítimo do tráfego ao vivo."""
        import inspect
        from core import settings as core_settings
        fonte = inspect.getsource(core_settings)
        self.assertIn("lock_timeout=15000", fonte)
        self.assertIn("statement_timeout=120000", fonte)
        self.assertIn("idle_in_transaction_session_timeout=300000", fonte)
        self.assertIn("if not RELEASE_COMMAND_PROCESS", fonte)


class ChecagensDeConexaoTemEscopoDeTenantTests(TestCase):
    """As checagens de conexão também vão ao banco — e as delas ficaram de fora.

    Quando os envios manuais passaram a `segurar_transacao=False`, as queries
    VISÍVEIS foram convertidas, mas as escondidas dentro das próprias checagens
    (capability do WhatsApp, flags de piloto, sessão do ML, cache de link) seguiram
    nuas. Sob RLS elas voltam zero linhas, e o resultado é o pior possível: a tela de
    conexão verde e o envio recusando por "desconectado".
    """

    def test_capability_do_whatsapp_le_com_escopo(self):
        import inspect
        from apps.accounts import wa_capabilities
        fonte = inspect.getsource(wa_capabilities.connection_for_session)
        self.assertIn("executar_orm_ou_direto", fonte)

    def test_flags_de_piloto_leem_com_escopo(self):
        import inspect
        from apps.accounts import feature_flags
        # `_decisao_memorizada` é onde a resolução por usuário toca o banco:
        # `enabled_for_user` e `feature_decision` delegam a ela (o memo por
        # instância de usuário evitava mil consultas por GET na tela de Promoções).
        # A garantia continua a mesma — nenhuma leitura de flag sai sem escopo.
        for flag in (feature_flags._decisao_memorizada,
                     feature_flags.enabled_for_whatsapp_session):
            with self.subTest(flag=flag.__name__):
                self.assertIn("_no_tenant(", inspect.getsource(flag))
        for atalho in (feature_flags.enabled_for_user,
                       feature_flags.feature_decision):
            with self.subTest(flag=atalho.__name__):
                self.assertIn("_decisao_memorizada(", inspect.getsource(atalho))

    def test_link_afiliado_do_ml_le_e_grava_com_escopo(self):
        """Sem escopo, `link_cacheado` some e `has_storage_state` diz "sem sessão":
        o envio pede reconexão de uma conta que nunca caiu."""
        import inspect
        from apps.scrapers.scraper_mercadolivre import link as ml
        fonte = inspect.getsource(ml.gerar_link_afiliado_para_produto)
        for chamada in ("_no_tenant(link_cacheado", "_no_tenant(has_storage_state",
                        "_no_tenant(salvar_cache", "_no_tenant(registrar_falha"):
            with self.subTest(chamada=chamada):
                self.assertIn(chamada, fonte)


class MensagensDoGerarLinksTests(TransactionTestCase):
    """A request web só registra a pendência; o worker diagnostica o browser.

    TransactionTestCase (e não TestCase) porque o job SSE roda em thread própria:
    dentro da transação do TestCase o SQLite trava a tabela para a outra thread.
    Mesmo motivo de EndpointsEnvioPostTests.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("msg-links", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Item", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://produto.mercadolivre.com.br/MLB-999999")

    def _corpo(self, excecao):
        from apps.scrapers.marketplaces.mercadolivre import MercadoLivre
        with patch.object(MercadoLivre, "prefetch_links", side_effect=excecao) as prefetch:
            resp = self.client.get("/scrapers/gerar-links/")
            return b"".join(resp.streaming_content).decode(), prefetch

    def test_antibot_nao_manda_reconectar(self):
        from apps.scrapers.scraper_mercadolivre.link import AntiBotError
        corpo, prefetch = self._corpo(AntiBotError("verificação"))
        prefetch.assert_not_called()
        self.assertIn("fila segura de links", corpo)
        self.assertNotIn("__ML_LOGIN__", corpo)
        self.assertNotIn("expirou", corpo)
        self.assertIn("__LINKS_ENFILEIRADOS__", corpo)

    def test_sessao_morta_continua_mandando_reconectar(self):
        """O caminho legítimo não pode regredir."""
        from apps.scrapers.scraper_mercadolivre.link import LoginError
        corpo, prefetch = self._corpo(LoginError("Sua sessão do Mercado Livre expirou."))
        prefetch.assert_not_called()
        self.assertNotIn("__ML_LOGIN__", corpo)
        self.assertIn("__LINKS_ENFILEIRADOS__", corpo)

    def test_ml_fora_do_ar_nao_manda_reconectar(self):
        from apps.scrapers.scraper_mercadolivre.link import AuthError
        corpo, prefetch = self._corpo(AuthError("sem resposta"))
        prefetch.assert_not_called()
        self.assertNotIn("__ML_LOGIN__", corpo)
        self.assertIn("__LINKS_ENFILEIRADOS__", corpo)

    def test_progresso_do_lote_chega_na_tela(self):
        """O emitir_fase já existia em gerar_links_em_lote, mas ia só para o logger:
        o usuário via a primeira linha e nada mais por ~4 minutos."""
        from apps.scrapers.marketplaces.mercadolivre import MercadoLivre
        from apps.scrapers.progresso import emitir_fase

        def _fingir(produtos, usuario=None, faixa=None):
            emitir_fase("Link 1/2", 0.5, (0, 100))
            return (1, 0)

        with patch.object(MercadoLivre, "prefetch_links", side_effect=_fingir) as prefetch:
            resp = self.client.get("/scrapers/gerar-links/")
            corpo = b"".join(resp.streaming_content).decode()
        prefetch.assert_not_called()
        self.assertNotIn("Link 1/2", corpo)
        self.assertIn("__LINKS_ENFILEIRADOS__", corpo)


class BotaoEnfileiraNoWorkerTests(TransactionTestCase):
    """O botão web não abre Chromium nem acessa o lease global system-only."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("lock-links", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        Produto.objects.create(
            marketplace="mercadolivre", nome="Item ML", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://produto.mercadolivre.com.br/MLB-111111")

    def _corpo(self):
        from apps.scrapers.marketplaces.mercadolivre import MercadoLivre

        with patch.object(MercadoLivre, "prefetch_links",
                          return_value=(1, 0)) as prefetch:
            resp = self.client.get("/scrapers/gerar-links/")
            corpo = b"".join(resp.streaming_content).decode()
        return corpo, prefetch

    def test_nao_abre_navegador_e_avisa_a_fila(self):
        corpo, prefetch = self._corpo()
        prefetch.assert_not_called()          # nada de segundo Chromium
        self.assertIn("__LINKS_ENFILEIRADOS__", corpo)
        self.assertIn("fila segura de links", corpo)
        # Fila não é problema de conta: não pode oferecer "Reconectar".
        self.assertNotIn("__ML_LOGIN__", corpo)
        self.assertNotIn("expirada", corpo)

    def test_amazon_nao_espera_o_lock(self):
        """A Amazon é Python puro e não abre navegador — fazer um usuário
        só-Amazon esperar o worker seria dano gratuito."""
        from apps.scrapers.marketplaces.amazon import Amazon

        Produto.objects.filter(marketplace="mercadolivre").delete()
        Produto.objects.create(
            marketplace="amazon", owner=self.user, asin="B0LOCK0001",
            nome="Item Amazon", origem="oferta", preco_sem_desconto=100,
            preco_com_cupom=70, link_produto="https://www.amazon.com.br/dp/B0LOCK0001")

        with patch.object(Amazon, "prefetch_links", return_value=(1, 0)) as prefetch:
            resp = self.client.get("/scrapers/gerar-links/")
            b"".join(resp.streaming_content)

        prefetch.assert_called_once()


@override_settings(AMAZON_GENERAL_COUPONS_URL="https://feed.example/amazon-coupons.json")
class AmazonGeneralCouponsSourceTests(TestCase):
    def test_feed_oficial_valido_persiste_codigo_sitewide_sem_produto(self):
        from unittest.mock import Mock
        from apps.scrapers.sources.amazon_general_coupons import (
            AmazonGeneralCouponsSource,
        )

        response = Mock()
        response.json.return_value = {"coupons": [{
            "id": "prime-week", "code": "PRIME20",
            "title": "20% OFF em toda a Amazon",
            "url": "https://www.amazon.com.br/",
            "discount_type": "porcentagem", "discount": 20,
            "minimum_purchase": 19, "valid_until": "2099-08-20",
            "sitewide": True,
        }]}
        response.raise_for_status.return_value = None
        source = AmazonGeneralCouponsSource()
        with patch(
            "apps.scrapers.sources.amazon_general_coupons.requests.get",
            return_value=response,
        ):
            rows = list(source.discover_coupons())

        self.assertEqual(len(rows), 1)
        persist_items(rows)
        coupon = CupomNormalizado.objects.get(external_id="prime-week")
        self.assertEqual(
            (coupon.redemption_mode, coupon.scope_type, coupon.audience_scope),
            ("code", "sitewide", "public"),
        )
        self.assertFalse(ProdutoCupom.objects.filter(cupom=coupon).exists())

    def test_feed_rejeita_destino_fora_da_amazon_e_codigo_invalido(self):
        from unittest.mock import Mock
        from apps.scrapers.sources.amazon_general_coupons import (
            AmazonGeneralCouponsSource,
        )

        response = Mock()
        response.json.return_value = {"coupons": [
            {"code": "X", "url": "https://www.amazon.com.br/", "discount": 10,
             "valid_until": "2099-08-20"},
            {"code": "VALID10", "url": "https://evilamazon.com.br/", "discount": 10,
             "valid_until": "2099-08-20"},
        ]}
        response.raise_for_status.return_value = None
        source = AmazonGeneralCouponsSource()
        with patch(
            "apps.scrapers.sources.amazon_general_coupons.requests.get",
            return_value=response,
        ):
            self.assertEqual(list(source.discover_coupons()), [])
        self.assertEqual(source.last_metrics["rejections"], {"invalid_identity": 2})


class OfficialCodeEnrichmentTests(TestCase):
    def test_fonte_oficial_enriquece_mesmo_codigo_heuristico_sem_mudar_precedencia(self):
        heuristic_source, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"},
        )
        heuristic = CupomNormalizado.objects.create(
            fonte=heuristic_source, external_id="checkout:SEMTERMOS20",
            marketplace="mercadolivre", titulo="Cupom SEMTERMOS20",
            codigo="SEMTERMOS20", link="https://www.mercadolivre.com.br/",
            regras={"modo_resgate": "codigo", "tipo_desconto": "",
                    "valor_desconto": None},
            evidencia={"association": "ssr_code"},
        )
        official = IngestedItem(
            external_id="official:SEMTERMOS20", marketplace="mercadolivre",
            source="ml-cupons-afiliados", kind="coupon",
            canonical_url="https://www.mercadolivre.com.br/",
            title="SEMTERMOS20 — 20% OFF", coupon_code="SEMTERMOS20",
            coupon_rules={"modo_resgate": "codigo",
                          "tipo_desconto": "porcentagem",
                          "valor_desconto": 20, "valor_minimo": 19,
                          "is_mar_aberto": True},
            valid_until=timezone.now() + timedelta(days=2),
            observed_at=timezone.now(), evidence={"public": True},
        )

        persist_items([official])
        heuristic.refresh_from_db()
        self.assertEqual(heuristic.regras["valor_desconto"], 20.0)
        self.assertEqual(heuristic.evidencia["enriched_by"]["source"],
                         "ml-cupons-afiliados")
        # A observação oficial continua com precedência superior; enriquecer não
        # converte o registro heurístico na fonte vencedora.
        self.assertEqual(
            CupomNormalizado.objects.get(external_id="official:SEMTERMOS20")
            .fonte.slug,
            "ml-cupons-afiliados",
        )
