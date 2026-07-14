import os
import tempfile
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.scrapers import whatsapp_client
from apps.scrapers.afiliado import tag_ml
from apps.scrapers.monitor_conexao import wa_conectado
from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, HistoricoEnvio,
    LinkAfiliadoUsuario, Produto, EventoOperacional, Publicacao,
    ReceitaAfiliado, RelatorioSync,
)
from apps.scrapers.precos import registrar as registrar_preco
from apps.scrapers.scraper_amazon import link as amazon_link
from apps.scrapers.scraper_amazon import ofertas_scraper as amazon_ofertas
from apps.scrapers.scraper_mercadolivre.scraper import _sincronizar_produtos_no_banco
from apps.scrapers.scraper_mercadolivre import link as ml_link


class AutomationStatusSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("status-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    @patch("apps.scrapers.automacao_state.is_running", return_value=True)
    @patch("apps.scrapers.automacao_state.read_state")
    def test_status_never_exposes_worker_traceback(self, read_state, _is_running):
        read_state.return_value = {
            "fase": "aguardando",
            "erro": 'File "/usr/local/lib/python3.12/site-packages/psycopg/connection.py"\nOperationalError: the connection is closed',
        }

        response = self.client.get(reverse("scraper-automacao"), {"tipo": "scrape"})

        self.assertEqual(response.status_code, 200)
        error = response.json()["estado"]["erro"]
        self.assertIn("Falha temporária", error)
        self.assertNotIn("psycopg", error)
        self.assertNotIn("/usr/local", error)


class AffiliateIdentityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("affiliate", password="test")
        self.product = Produto.objects.create(
            nome="Produto teste",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
            origem="oferta",
        )

    @override_settings(AFILIADO_TAG="global-que-nao-deve-ser-usada")
    def test_ml_does_not_use_manual_or_global_tag(self):
        self.user.perfil.afiliado_tag_ml = "manual-que-nao-deve-ser-usada"
        self.assertEqual(tag_ml(self.user), "")

    def test_ml_link_uses_only_the_users_auth_file(self):
        with tempfile.TemporaryDirectory() as auth_dir:
            user_auth = os.path.join(auth_dir, f"auth_{self.user.id}.json")
            with open(user_auth, "w", encoding="utf-8") as auth_file:
                auth_file.write("{}")

            with (
                patch.object(ml_link, "_auth_dir", return_value=auth_dir),
                patch.object(
                    ml_link,
                    "afiliate_link_builder",
                    return_value="https://meli.la/user-link",
                ) as builder,
                patch("apps.scrapers.afiliado.salvar_cache") as save_cache,
            ):
                result = ml_link.gerar_link_afiliado_para_produto(
                    self.product, usuario=self.user
                )

            self.assertEqual(result["link_afiliado"], "https://meli.la/user-link")
            self.assertEqual(builder.call_args.kwargs["auth_path"], user_auth)
            save_cache.assert_called_once()

    def test_ml_link_never_falls_back_to_global_auth_for_a_user(self):
        with tempfile.TemporaryDirectory() as auth_dir:
            with open(os.path.join(auth_dir, "auth.json"), "w", encoding="utf-8") as auth:
                auth.write("{}")
            with patch.object(ml_link, "_auth_dir", return_value=auth_dir):
                with self.assertRaises(ml_link.LoginError):
                    ml_link.gerar_link_afiliado_para_produto(
                        self.product, usuario=self.user
                    )


class WhatsAppIsolationTests(SimpleTestCase):
    @patch("apps.scrapers.whatsapp_client.status")
    def test_connection_monitor_checks_the_requested_session(self, status):
        status.return_value = {"conectado": True}
        self.assertTrue(wa_conectado("user-42"))
        status.assert_called_once_with("user-42")

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_session_is_started_only_by_explicit_command(self, request):
        response = Mock()
        response.json.return_value = {"sucesso": True, "instancia": "user-42"}
        request.return_value = response

        result = whatsapp_client.iniciar_sessao("user-42")

        self.assertTrue(result["sucesso"])
        request.assert_called_once_with(
            "POST", "http://whatsapp.internal:3000/api/sessoes",
            headers={"x-api-key": "secret", "Content-Type": "application/json"},
            params=None, json={"session": "user-42"}, timeout=10,
        )

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
        WHATSAPP_GRUPO_ID="",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_routes_to_the_users_session(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True, "mensagem_id": "abc123"}
        post.return_value = response

        result = whatsapp_client.enviar_oferta(
            "123@g.us", "mensagem", session="user-42"
        )

        self.assertTrue(result["sucesso"])
        self.assertEqual(post.call_args.kwargs["json"]["session"], "user-42")
        self.assertEqual(post.call_args.kwargs["json"]["grupoid"], "123@g.us")
        self.assertEqual(post.call_args.kwargs["timeout"], 75)

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
        WHATSAPP_GRUPO_ID="",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_rejects_success_without_message_confirmation(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True}
        post.return_value = response

        result = whatsapp_client.enviar_oferta(
            "123@g.us", "mensagem", session="user-42"
        )

        self.assertFalse(result["sucesso"])
        self.assertIn("ID de confirmação", result["erro"])


class ConfiguracaoValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("config-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-configuracoes")

    def test_rejects_malformed_numeric_values_without_server_error(self):
        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "intervalo_minutos": "nao-e-numero",
        })

        self.assertRedirects(response, self.url)
        self.assertFalse(self.user.configuracoes.exists())
        self.assertTrue(any(
            "valor inválido" in str(message)
            for message in get_messages(response.wsgi_request)
        ))

    def test_rejects_invalid_schedule_range(self):
        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "intervalo_minutos": "60",
            "janela_inicio": "24",
            "janela_fim": "8",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        self.assertFalse(self.user.configuracoes.exists())


class TopPromocoesFilterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("deals-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-top")
        Produto.objects.create(
            marketplace="mercadolivre",
            nome="Fone Bluetooth",
            categoria="Áudio",
            macro_categoria="Eletrônicos",
            preco_sem_desconto=100,
            preco_com_cupom=50,
            link_produto="https://example.com/fone",
            origem="oferta",
        )
        Produto.objects.create(
            marketplace="amazon",
            owner=self.user,
            nome="Cafeteira",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=100,
            preco_com_cupom=90,
            link_produto="https://example.com/cafeteira",
            origem="oferta",
        )

    def test_search_and_minimum_discount_are_applied(self):
        response = self.client.get(self.url, {"q": "fone", "min_desconto": "40"})

        self.assertEqual([p.nome for p in response.context["produtos"]], ["Fone Bluetooth"])

    def test_filters_are_restored_on_next_visit_and_can_be_cleared(self):
        self.client.get(self.url, {"loja": "amazon", "ordenar": "valor"})

        response = self.client.get(self.url)
        self.assertEqual(response.context["loja_selecionada"], "amazon")
        self.assertEqual([p.nome for p in response.context["produtos"]], ["Cafeteira"])

        self.client.get(self.url, {"reset": "1"})
        response = self.client.get(self.url)
        self.assertEqual(response.context["loja_selecionada"], "")
        self.assertEqual(len(response.context["produtos"]), 2)

    def test_expired_coupon_is_not_attached_to_top_promotion(self):
        product = Produto.objects.create(
            marketplace="mercadolivre",
            nome="Panela com cupom vencido",
            categoria="Cozinha",
            macro_categoria="Casa",
            campanha_id="expired-coupon",
            preco_sem_desconto=200,
            preco_com_cupom=120,
            link_produto="https://example.com/panela",
            origem="oferta",
        )
        Cupom.objects.create(
            campanha_id="expired-coupon", titulo="Cupom vencido",
            tipo_desconto="fixo", valor_desconto=80, valor_minimo=0,
            link_original="https://example.com/coupon", estado="ativo",
            validade=timezone.now() - timedelta(days=1),
        )

        response = self.client.get(self.url, {"q": "Panela com cupom vencido"})

        [rendered] = [p for p in response.context["produtos"] if p.id == product.id]
        self.assertIsNone(rendered.cupom)

    def test_stale_products_are_hidden_from_top_promotions(self):
        stale = Produto.objects.create(
            marketplace="mercadolivre",
            nome="Oferta velha",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=100,
            preco_com_cupom=50,
            link_produto="https://example.com/stale",
            origem="oferta",
            estado="stale",
        )

        response = self.client.get(self.url, {"q": "Oferta velha"})

        self.assertNotIn(stale.id, [p.id for p in response.context["produtos"]])


class AttributionWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.product = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta rastreável", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=60,
            link_produto="https://example.com/product",
        )

    def test_signed_redirect_records_anonymous_click(self):
        from django.core import signing
        publication = Publicacao.objects.create(
            usuario=self.user, produto=self.product, canal="whatsapp",
            destino_id="group@g.us", status="enviado",
            link_afiliado="https://example.com/affiliate",
        )
        token = signing.dumps({"p": str(publication.id_publico)}, salt="click")

        response = self.client.get(reverse("scraper-redirect", args=[token]))

        self.assertRedirects(
            response, "https://example.com/affiliate", fetch_redirect_response=False)
        self.assertEqual(CliquePublicacao.objects.filter(publicacao=publication).count(), 1)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_invalid_redirect_token_is_not_open_redirect(self):
        response = self.client.get(reverse("scraper-redirect", args=["not-a-real-token"]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(CliquePublicacao.objects.exists())

    def test_operational_log_sanitizes_sensitive_context(self):
        from apps.scrapers.eventos import log_event

        log_event("sistema", "secret_test", "testing", usuario=self.user,
                  contexto={"api_key": "super-secret", "safe": "ok"})

        event = EventoOperacional.objects.get(evento="secret_test")
        self.assertEqual(event.contexto["api_key"], "***")
        self.assertEqual(event.contexto["safe"], "ok")

    @patch("apps.scrapers.views.wa_conectado", create=True)
    def test_dashboard_is_the_authenticated_home(self, _wa):
        with (
            patch("apps.scrapers.monitor_conexao.wa_conectado", return_value=False),
            patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False),
        ):
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sua operação")

    @patch("apps.scrapers.relatorios.ADAPTERS")
    def test_automatic_report_sync_is_idempotent(self, adapters):
        from datetime import date
        from apps.scrapers.relatorios import ReportRow, sync_marketplace

        adapter = Mock()
        adapter.fetch.return_value = [ReportRow(
            marketplace="mercadolivre", data=date(2026, 7, 9),
            etiqueta="grupo-casa", produto_nome="Fone", cliques=10,
            pedidos=2, receita=199.90, comissao=20.00,
        )]
        adapters.__contains__.side_effect = lambda key: key == "mercadolivre"
        adapters.__getitem__.side_effect = lambda key: adapter

        sync_marketplace(self.user, "mercadolivre")
        sync_marketplace(self.user, "mercadolivre")

        self.assertEqual(ReceitaAfiliado.objects.filter(usuario=self.user).count(), 1)
        receita = ReceitaAfiliado.objects.get(usuario=self.user)
        self.assertEqual(receita.cliques, 10)
        self.assertEqual(receita.origem, "auto")
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="relatorios", evento="sync_ok", usuario=self.user).exists())

    @patch("apps.scrapers.relatorios.sync_marketplace")
    def test_dashboard_sync_now_uses_automatic_sync(self, sync_marketplace):
        sync_marketplace.return_value = RelatorioSync.objects.create(
            usuario=self.user, marketplace="mercadolivre", status="ok",
            registros_criados=1, registros_atualizados=0,
        )

        response = self.client.post(reverse("scraper-sincronizar-receitas"), {
            "marketplace": "mercadolivre",
        })

        self.assertRedirects(response, reverse("home"))
        sync_marketplace.assert_called_once_with(self.user, "mercadolivre")

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_link")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_failed_publication_writes_operational_event(
        self, build_link, verify_link, send, _img
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        build_link.return_value = {
            "link_afiliado": "https://example.com/a?tracking_id=ok",
            "afiliado_ok": True,
        }
        verify_link.return_value = {"ok": True}
        send.return_value = {"sucesso": False, "erro": "WhatsApp desconectado"}

        result = enviar_oferta_de_produto(
            self.product, "group@g.us", usuario=self.user, destino_nome="Grupo")

        self.assertFalse(result["sucesso"])
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="publicacao", evento="send_failed", usuario=self.user).exists())

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.registry.get_sender")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_successful_delivery_records_history_without_legacy_key(
        self, get_marketplace, get_sender, _image
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        from apps.scrapers.senders.base import WhatsAppMarkup

        marketplace = Mock()
        marketplace.build_affiliate_link.return_value = {
            "link_afiliado": "https://example.com/a?tracking_id=ok",
            "afiliado_ok": True,
        }
        get_marketplace.return_value = marketplace
        sender = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        sender.enviar_oferta.return_value = {"sucesso": True, "via": "test"}
        get_sender.return_value = sender

        result = enviar_oferta_de_produto(
            self.product, "group@g.us", verificar=False,
            usuario=self.user, destino_nome="Grupo",
        )

        self.assertTrue(result["sucesso"])
        self.assertTrue(HistoricoEnvio.objects.filter(
            produto=self.product, usuario=self.user,
        ).exists())
        self.assertEqual(
            Publicacao.objects.get(produto=self.product).status, "enviado"
        )

    def test_group_specific_branding_overrides_account_default(self):
        from apps.scrapers.ofertas import montar_mensagem
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="group@g.us", nome_marca="Tech do Dia",
            chamada_acao="Ver a oferta",
        )
        message = montar_mensagem(
            self.product, "https://example.com/a", None,
            usuario=self.user, configuracao=config,
        )
        self.assertIn("Tech do Dia", message)
        self.assertIn("Ver a oferta", message)


class RankingAndCooldownTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ranker", password="test")
        self.group_a = "casa@g.us"
        self.group_b = "tech@g.us"

    def _product(self, nome, preco_final, macro="Casa"):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            macro_categoria=macro, categoria=macro,
            preco_sem_desconto=100, preco_com_cupom=preco_final,
            link_produto=f"https://example.com/{nome.replace(' ', '-')}",
        )

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_cooldown_is_per_destination_and_allows_other_groups(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Air fryer", 70)
        Publicacao.objects.create(
            usuario=self.user, produto=product, canal="whatsapp",
            destino_id=self.group_a, status="enviado", enviada_em=timezone.now(),
            preco_final=70,
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        same_group = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)
        other_group = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_b, min_desconto_percent=10)

        self.assertEqual(same_group, [])
        self.assertEqual(other_group, [product])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_cooldown_allows_evergreen_product_after_meaningful_price_drop(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Cafeteira", 70)
        Publicacao.objects.create(
            usuario=self.user, produto=product, canal="whatsapp",
            destino_id=self.group_a, status="enviado", enviada_em=timezone.now(),
            preco_final=80,
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertEqual(selected, [product])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_ranking_explains_real_30_day_low(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Monitor", 70, macro="Eletrônicos")
        for price in [100, 95, 70]:
            registrar_preco("mercadolivre", "", product.link_produto, price)

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertEqual(selected, [product])
        self.assertIn("mínima de 30 dias", selected[0].motivos_score)

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_active_coupon_minimum_spend_blocks_ineligible_offer(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Panela", origem="oferta",
            campanha_id="coupon-1", macro_categoria="Casa", categoria="Casa",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://example.com/panela",
        )
        Cupom.objects.create(
            campanha_id="coupon-1", titulo="Cupom acima do mínimo",
            tipo_desconto="fixo", valor_desconto=30, valor_minimo=150,
            link_original="https://example.com/coupon", estado="ativo",
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertNotIn(product, selected)


class AmazonPipelineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("amazon-user", password="test")
        self.user.perfil.afiliado_tag_amazon = "tagusuario-20"
        self.user.perfil.save(update_fields=["afiliado_tag_amazon"])

    def test_amazon_affiliate_link_uses_user_tag_and_private_cache(self):
        product = Produto.objects.create(
            marketplace="amazon", owner=self.user, asin="B012345678",
            nome="Echo", origem="oferta", preco_sem_desconto=300,
            preco_com_cupom=250,
            link_produto="https://www.amazon.com.br/dp/B012345678?ref=x",
        )

        result = amazon_link.gerar_link_afiliado_para_produto(product, usuario=self.user)

        self.assertEqual(
            result["link_afiliado"],
            "https://www.amazon.com.br/dp/B012345678?tag=tagusuario-20",
        )
        self.assertTrue(amazon_link.link_tem_tag_afiliado(result["link_afiliado"], self.user))
        self.assertTrue(LinkAfiliadoUsuario.objects.filter(
            usuario=self.user, produto=product, afiliado_ok=True).exists())

    def test_amazon_item_mapping_requires_permitted_api_price_fields(self):
        mapped = amazon_ofertas._mapear_item({
            "asin": "B000API123",
            "itemInfo": {"title": {"displayValue": "Produto API"}},
            "offersV2": {"listings": [{
                "price": {
                    "money": {"amount": 80},
                    "savingBasis": {"money": {"amount": 100}},
                },
                "merchantInfo": {"name": "Amazon.com.br"},
                "dealDetails": {"displayName": "Oferta relâmpago"},
            }]},
            "images": {"primary": {"large": {"url": "https://example.com/i.jpg"}}},
        })

        self.assertEqual(mapped["asin"], "B000API123")
        self.assertEqual(mapped["preco_sem_desconto"], 100)
        self.assertEqual(mapped["preco_com_cupom"], 80)
        self.assertTrue(mapped["tem_promocao"])

    @patch("apps.scrapers.scraper_amazon.ofertas_scraper.creators_api.search_items")
    def test_amazon_upsert_keeps_products_private_to_user(self, search_items):
        search_items.side_effect = [[{
            "asin": "BPRIVATE123",
            "itemInfo": {"title": {"displayValue": "Produto privado"}},
            "offersV2": {"listings": [{
                "price": {
                    "money": {"amount": 50},
                    "savingBasis": {"money": {"amount": 100}},
                },
            }]},
        }], []]

        with override_settings(AMAZON_FEED_KEYWORDS=["fone"], AMAZON_MIN_SAVINGS_PCT=10):
            total = amazon_ofertas.mapear_ofertas(usuario=self.user)

        self.assertEqual(total, 1)
        self.assertTrue(Produto.objects.filter(
            marketplace="amazon", asin="BPRIVATE123", owner=self.user,
            fonte="amazon-creators-api", estado="ativo",
        ).exists())


class TenantSecurityTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password="test")
        self.other = get_user_model().objects.create_user("other", password="test")
        self.owner.perfil.marcar_verificado()
        self.other.perfil.marcar_verificado()

    def test_user_cannot_update_another_users_destination_rule(self):
        cfg = ConfiguracaoEnvio.objects.create(
            owner=self.owner, grupo_id="owner@g.us", grupo_nome="Original",
            intervalo_minutos=60, janela_inicio=8, janela_fim=20,
            min_desconto_percent=15,
        )
        self.client.force_login(self.other)

        self.client.post(reverse("scraper-configuracoes"), {
            "id": str(cfg.id),
            "canal": "whatsapp",
            "grupo_id": "hijack@g.us",
            "grupo_nome": "Hijacked",
            "intervalo_minutos": "15",
            "janela_inicio": "8",
            "janela_fim": "20",
            "min_desconto_percent": "1",
            "max_envios_dia": "99",
            "pausar_apos_falhas": "9",
        })

        cfg.refresh_from_db()
        self.assertEqual(cfg.owner, self.owner)
        self.assertEqual(cfg.grupo_id, "owner@g.us")
        self.assertEqual(cfg.grupo_nome, "Original")


class MercadoLivreCleanupIsolationTests(TestCase):
    def test_coupon_sync_preserves_private_products_from_other_marketplaces(self):
        owner = get_user_model().objects.create_user("amazon-owner", password="test")
        private_product = Produto.objects.create(
            marketplace="amazon",
            owner=owner,
            asin="B000TEST",
            campanha_id="same-campaign",
            origem="cupom",
            nome="Produto privado",
            preco_sem_desconto=100,
            preco_com_cupom=90,
            link_produto="https://www.amazon.com.br/dp/B000TEST",
        )

        _sincronizar_produtos_no_banco([{
            "campaignId": "same-campaign",
            "produtos_aplicaveis": [],
        }])

        self.assertTrue(Produto.objects.filter(pk=private_product.pk).exists())

    def test_coupon_sync_marks_old_shared_coupon_products_stale_instead_of_deleting(self):
        old_product = Produto.objects.create(
            marketplace="mercadolivre",
            campanha_id="coupon-stale",
            origem="cupom",
            nome="Produto antigo",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://example.com/old",
        )

        _sincronizar_produtos_no_banco([{
            "campaignId": "coupon-stale",
            "produtos_aplicaveis": [],
        }])

        old_product.refresh_from_db()
        self.assertEqual(old_product.estado, "stale")
        self.assertIn("sincronização", old_product.falha_verificacao)
