"""Shopee: assinatura, adaptadores, link sem navegador e tela de conexão.

Nada aqui toca a rede. A Shopee é substituída no ponto de transporte
(``shopee.executar`` ou ``requests.post``), que é o único lugar onde o mundo
externo entra — o resto é lógica nossa e precisa ser testável sem credencial.

O que estes testes protegem, em ordem de importância:

1. **A assinatura casa com o corpo que viaja.** Se alguém trocar a serialização
   manual por ``requests(json=...)``, a assinatura passa a ser calculada sobre uma
   string diferente da enviada e a API devolve 401 — um erro que parece credencial
   errada e custa horas.
2. **A Shopee não pede navegador.** É a razão de ela existir no produto. Um teste
   falha se algum caminho voltar a acionar o slot de Chromium.
3. **Coleta parcial não apaga catálogo.** Mesma regra das outras fontes.
"""
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization
from apps.scrapers import shopee
from apps.scrapers.marketplaces.registry import get_marketplace
from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, IntegracaoAfiliado,
    LinkAfiliadoCupomUsuario,
)
from apps.scrapers.sources.shopee import ShopeeCampaignsSource, ShopeeOffersSource


class RespostaFalsa:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class AssinaturaTests(TestCase):
    def test_assinatura_usa_o_mesmo_corpo_que_viaja(self):
        """A string assinada tem de ser byte a byte a que vai no fio."""
        capturado = {}

        def _post(url, data=None, headers=None, timeout=None):
            capturado["data"] = data
            capturado["headers"] = headers
            return RespostaFalsa({"data": {"shopeeOfferV2": {"nodes": []}}})

        with patch("apps.scrapers.shopee.requests.post", side_effect=_post):
            shopee.executar("query { ping }", {"a": 1}, app_id="123", secret="segredo")

        corpo = capturado["data"].decode("utf-8")
        autorizacao = capturado["headers"]["Authorization"]
        timestamp = autorizacao.split("Timestamp=")[1].split(",")[0].strip()
        assinatura = autorizacao.split("Signature=")[1].strip()
        esperado = hashlib.sha256(
            f"123{timestamp}{corpo}segredo".encode("utf-8")
        ).hexdigest()
        self.assertEqual(assinatura, esperado)
        # E o corpo continua sendo JSON válido com a query pedida.
        self.assertEqual(json.loads(corpo)["variables"], {"a": 1})

    def test_erro_graphql_com_http_200_vira_excecao(self):
        """`errors` em resposta 200 não pode passar por coleta vazia."""
        with patch("apps.scrapers.shopee.requests.post",
                   return_value=RespostaFalsa({"errors": [{"code": 10020,
                                                           "message": "invalid"}]})):
            with self.assertRaises(shopee.ShopeeError) as caso:
                shopee.executar("query { ping }", app_id="1", secret="s")
        self.assertIn("Reconecte", caso.exception.public_message)
        self.assertFalse(caso.exception.retryable)

    def test_limite_de_taxa_e_retentavel(self):
        with patch("apps.scrapers.shopee.requests.post",
                   return_value=RespostaFalsa({"errors": [{"code": 10030,
                                                           "message": "too many"}]})):
            with self.assertRaises(shopee.ShopeeError) as caso:
                shopee.executar("query { ping }", app_id="1", secret="s")
        self.assertTrue(caso.exception.retryable)
        self.assertEqual(caso.exception.code, shopee.RATE_LIMITED)

    def test_sem_credencial_falha_antes_de_qualquer_chamada(self):
        with patch("apps.scrapers.shopee.requests.post") as post:
            with self.assertRaises(shopee.ShopeeConfigError):
                shopee.executar("query { ping }", app_id="", secret="")
        post.assert_not_called()


# O Secret vive num EncryptedCharField, e sem chave o campo levanta
# ImproperlyConfigured quando DEBUG=0 (que é o caso na suíte). A chave abaixo é de
# teste e não abre nada: mesmo padrão já usado em test_awin_catalog.
CHAVE_DE_TESTE = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@override_settings(SECRETS_FERNET_KEY=CHAVE_DE_TESTE)
class _BaseComIntegracao(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usuario = get_user_model().objects.create_user(
            "shopee-user", email="shopee@example.com", password="senha-123",
        )
        cls.usuario.perfil.marcar_verificado()
        ensure_personal_organization(cls.usuario)
        cls.integracao = IntegracaoAfiliado.objects.create(
            owner=cls.usuario, provedor="shopee", identificador_conta="12345",
            token="segredo", habilitada=True, status="conectada",
        )


class FonteOfertasTests(_BaseComIntegracao):
    def _no(self, **extra):
        base = {
            "itemId": "999", "shopId": "77", "productName": "Fone bluetooth",
            "productLink": "https://shopee.com.br/produto-999",
            "offerLink": "https://s.shopee.com.br/abc",
            "imageUrl": "https://cf.shopee.com.br/f.jpg",
            "priceMin": "100.00", "priceMax": "100.00", "priceDiscountRate": "50",
            "commissionRate": "0.08", "sales": "120", "ratingStar": "4.8",
            "shopName": "Loja Teste",
        }
        base.update(extra)
        return base

    def test_oferta_deriva_preco_de_referencia_do_desconto_declarado(self):
        with patch("apps.scrapers.sources.shopee.listar_produtos",
                   return_value=([self._no()], True)):
            itens = list(ShopeeOffersSource().discover_offers(owner=self.usuario))
        self.assertEqual(len(itens), 1)
        item = itens[0]
        self.assertEqual(item.marketplace, "shopee")
        self.assertEqual(item.external_id, "77_999")
        self.assertEqual(item.current_price, 100.0)
        # 100 com 50% de desconto => referência 200.
        self.assertEqual(item.reference_price, 200.0)

    def test_sem_desconto_declarado_nao_inventa_referencia(self):
        """Referência falsa vira "ótima promoção" falsa — pior que não ter oferta."""
        with patch("apps.scrapers.sources.shopee.listar_produtos",
                   return_value=([self._no(priceDiscountRate="0")], True)):
            itens = list(ShopeeOffersSource().discover_offers(owner=self.usuario))
        self.assertEqual(itens[0].reference_price, 0.0)

    def test_coleta_parcial_nao_declara_inventario_completo(self):
        fonte = ShopeeOffersSource()
        with patch("apps.scrapers.sources.shopee.listar_produtos",
                   return_value=([self._no()], False)):
            list(fonte.discover_offers(owner=self.usuario))
        self.assertFalse(fonte.last_metrics["complete"])

    def test_falha_da_api_degrada_sem_derrubar_a_coleta(self):
        fonte = ShopeeOffersSource()
        with patch("apps.scrapers.sources.shopee.listar_produtos",
                   side_effect=shopee.ShopeeError("fora do ar", retryable=True)):
            itens = list(fonte.discover_offers(owner=self.usuario))
        self.assertEqual(itens, [])
        self.assertFalse(fonte.last_metrics["complete"])

    def test_usuario_sem_integracao_nao_e_falha_de_fonte(self):
        outro = get_user_model().objects.create_user("sem-shopee", password="x")
        fonte = ShopeeOffersSource()
        itens = list(fonte.discover_offers(owner=outro))
        self.assertEqual(itens, [])
        self.assertEqual(fonte.last_metrics.get("reason"), "sem_credencial")

    def test_fonte_nao_pede_navegador(self):
        """A razão de a Shopee existir no produto: ela não entra na fila do Chromium."""
        self.assertFalse(getattr(ShopeeOffersSource, "requires_chromium", False))
        self.assertFalse(getattr(ShopeeCampaignsSource, "requires_chromium", False))


class FonteCampanhasTests(_BaseComIntegracao):
    def _campanha(self, **extra):
        agora = timezone.now()
        base = {
            "offerName": "Frete grátis acima de R$ 19",
            "offerLink": "https://s.shopee.com.br/campanha",
            "imageUrl": "https://cf.shopee.com.br/c.jpg",
            "offerType": "SHOP",
            "commissionRate": "0.12",
            "periodStartTime": int(agora.timestamp()),
            "periodEndTime": int((agora + timedelta(days=7)).timestamp()),
        }
        base.update(extra)
        return base

    def test_comissao_de_campanha_nao_vira_cupom(self):
        with patch("apps.scrapers.sources.shopee.listar_campanhas",
                   return_value=([self._campanha()], True)):
            fonte = ShopeeCampaignsSource()
            itens = list(fonte.discover_coupons(owner=self.usuario))
        self.assertEqual(itens, [])
        self.assertEqual(fonte.last_metrics["source_rows"], 1)
        self.assertEqual(
            fonte.last_metrics["rejected_by_reason"],
            {"affiliate_commission_is_not_customer_discount": 1},
        )

    def test_comissao_em_pontos_tambem_nao_vira_desconto(self):
        with patch("apps.scrapers.sources.shopee.listar_campanhas",
                   return_value=([self._campanha(commissionRate="15")], True)):
            itens = list(ShopeeCampaignsSource().discover_coupons(owner=self.usuario))
        self.assertEqual(itens, [])

    def test_janela_curta_nao_inventa_cupom_relampago(self):
        agora = timezone.now()
        curta = self._campanha(
            periodStartTime=int(agora.timestamp()),
            periodEndTime=int((agora + timedelta(hours=3)).timestamp()),
        )
        with patch("apps.scrapers.sources.shopee.listar_campanhas",
                   return_value=([curta], True)):
            itens = list(ShopeeCampaignsSource().discover_coupons(owner=self.usuario))
        self.assertEqual(itens, [])

    def test_campanha_sem_desconto_comprovado_nao_entra(self):
        with patch("apps.scrapers.sources.shopee.listar_campanhas",
                   return_value=([self._campanha(commissionRate="0")], True)):
            itens = list(ShopeeCampaignsSource().discover_coupons(owner=self.usuario))
        self.assertEqual(itens, [])

    def test_registro_legado_de_comissao_shopee_nao_fica_publicavel(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.coupon_rules import ativacao_publicavel
        from apps.scrapers.models import (
            CupomDisponibilidade, CupomNormalizado, FonteIngestao,
        )

        fonte = FonteIngestao.objects.create(
            slug="shopee-campaigns", marketplace="shopee", nome="Shopee",
            status="ok",
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, owner=self.usuario,
            external_id="shopee:campanha:SHOP:frete", marketplace="shopee",
            titulo="Frete grátis acima de R$ 19", codigo="",
            link="https://s.shopee.com.br/campanha",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 12},
            evidencia={"transport": "shopee-affiliate-api", "commission_rate": 12},
        )
        self.assertFalse(ativacao_publicavel(cupom, usuario=self.usuario))

        projetar_disponibilidade_cupons(self.usuario)
        self.assertEqual(
            CupomDisponibilidade.objects.get(
                cupom=cupom, usuario=self.usuario).stage,
            "discarded",
        )


class MarketplaceShopeeTests(_BaseComIntegracao):
    class ProdutoFalso:
        id = 42
        pk = 42
        link_produto = "https://shopee.com.br/produto-999"
        marketplace = "shopee"

    def _cupom_oficial(self, promotion="123"):
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="shopee-public-coupons",
            defaults={"marketplace": "shopee", "nome": "Shopee"},
        )
        return CupomNormalizado.objects.create(
            fonte=fonte, external_id=f"shopee-voucher:{promotion}",
            marketplace="shopee", titulo="R$ 20 OFF", codigo="",
            link=f"https://shopee.com.br/voucher/details?promotionId={promotion}",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "fixo",
                    "valor_desconto": 20},
            evidencia={"association": "shopee-official-coupon-page",
                       "promotion_id": promotion, "availability": "claimable"},
        )

    def test_registry_resolve_a_loja(self):
        self.assertEqual(get_marketplace("shopee").slug, "shopee")

    def test_link_de_afiliado_sai_por_api_sem_navegador(self):
        loja = get_marketplace("shopee")
        with patch("apps.scrapers.carga.browser_resource") as slot, \
                patch("apps.scrapers.shopee.gerar_link",
                      return_value="https://s.shopee.com.br/xyz") as gerar:
            info = loja.build_affiliate_link(self.ProdutoFalso(), usuario=self.usuario)
        slot.assert_not_called()
        self.assertEqual(info["link_afiliado"], "https://s.shopee.com.br/xyz")
        self.assertTrue(info["afiliado_ok"])
        # O rastreio por usuário viaja em subIds: sem isso a conciliação por
        # cliente vira adivinhação no relatório de conversão.
        self.assertIn(f"u{self.usuario.id}", gerar.call_args.kwargs["sub_ids"])

    def test_link_cru_de_produto_nao_conta_como_afiliado(self):
        loja = get_marketplace("shopee")
        self.assertFalse(loja.verify_affiliate_tag("https://shopee.com.br/produto-999"))
        self.assertTrue(loja.verify_affiliate_tag("https://s.shopee.com.br/xyz"))

    def test_cupom_oficial_recebe_link_da_conta_do_usuario(self):
        from apps.scrapers.ofertas import resolver_link_afiliado_cupom

        cupom = self._cupom_oficial()
        with patch("apps.scrapers.shopee.gerar_link",
                   return_value="https://s.shopee.com.br/cupom123") as gerar:
            primeiro = resolver_link_afiliado_cupom(cupom, self.usuario)
            segundo = resolver_link_afiliado_cupom(cupom, self.usuario)

        self.assertTrue(primeiro["sucesso"])
        self.assertTrue(segundo["cache"])
        gerar.assert_called_once()
        self.assertIn(f"u{self.usuario.pk}", gerar.call_args.kwargs["sub_ids"])
        self.assertTrue(LinkAfiliadoCupomUsuario.objects.get(
            usuario=self.usuario, cupom=cupom,
        ).verificado_ok)

    def test_cupom_oficial_so_fica_ready_depois_do_link_da_conta(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.ofertas import resolver_link_afiliado_cupom

        cupom = self._cupom_oficial("456")
        antes = projetar_disponibilidade_cupons(self.usuario)
        self.assertEqual(antes["reasons"].get("affiliate_link_pending"), 1)

        with patch("apps.scrapers.shopee.gerar_link",
                   return_value="https://s.shopee.com.br/cupom456"):
            self.assertTrue(
                resolver_link_afiliado_cupom(cupom, self.usuario)["sucesso"],
            )
        depois = projetar_disponibilidade_cupons(self.usuario)
        self.assertEqual(depois["stages"].get("ready"), 1)

    def test_sem_integracao_cupom_oficial_explica_o_bloqueio(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        self.integracao.status = "pendente"
        self.integracao.save(update_fields=["status"])
        self._cupom_oficial("789")

        resultado = projetar_disponibilidade_cupons(self.usuario)

        self.assertEqual(
            resultado["reasons"].get("shopee_integration_disconnected"), 1,
        )
        self.assertIsNone(resultado["stages"].get("ready"))

    def test_falha_da_api_nao_marca_produto_como_inafiliavel(self):
        loja = get_marketplace("shopee")
        with patch("apps.scrapers.shopee.gerar_link",
                   side_effect=shopee.ShopeeError("instável", retryable=True)):
            self.assertIsNone(
                loja.build_affiliate_link(self.ProdutoFalso(), usuario=self.usuario))

    def test_lote_para_quando_a_shopee_pede_ritmo_menor(self):
        """Insistir sob limite de taxa só aprofunda o bloqueio."""
        loja = get_marketplace("shopee")
        produtos = [self.ProdutoFalso() for _ in range(4)]
        respostas = [
            "https://s.shopee.com.br/1",
            shopee.ShopeeError("devagar", retryable=True, code=shopee.RATE_LIMITED),
        ]

        def _gerar(*args, **kwargs):
            valor = respostas.pop(0) if respostas else "https://s.shopee.com.br/n"
            if isinstance(valor, Exception):
                raise valor
            return valor

        with patch("apps.scrapers.shopee.gerar_link", side_effect=_gerar), \
                patch("apps.scrapers.afiliado.salvar_cache"), \
                patch("apps.scrapers.afiliado.registrar_falha"):
            gerados, falhas = loja.prefetch_links(produtos, usuario=self.usuario)
        self.assertEqual((gerados, falhas), (1, 1))


@override_settings(SHOPEE_INTEGRATION_ENABLED=True,
                   SECRETS_FERNET_KEY=CHAVE_DE_TESTE)
class TelaDeConexaoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            "shopee-tela", email="tela@example.com", password="senha-123",
        )
        self.usuario.perfil.marcar_verificado()
        ensure_personal_organization(self.usuario)
        self.client.force_login(self.usuario)

    def test_credencial_invalida_nao_e_gravada(self):
        with patch("apps.scrapers.shopee.validar_credenciais",
                   side_effect=shopee.ShopeeError("Credenciais recusadas.")):
            resposta = self.client.post(
                reverse("scraper-shopee-conectar"),
                {"app_id": "123", "app_secret": "errado"}, follow=True,
            )
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(
            IntegracaoAfiliado.objects.filter(provedor="shopee").exists(),
            "Credencial recusada não pode deixar integração pela metade.",
        )

    def test_conexao_valida_grava_e_aparece_na_tela(self):
        with patch("apps.scrapers.shopee.validar_credenciais", return_value=True):
            self.client.post(
                reverse("scraper-shopee-conectar"),
                {"app_id": "987654", "app_secret": "segredo-forte"},
            )
        integracao = IntegracaoAfiliado.objects.get(
            owner=self.usuario, provedor="shopee")
        self.assertEqual(integracao.status, "conectada")
        self.assertEqual(integracao.identificador_conta, "987654")
        conta = self.client.get(reverse("scraper-conta"))
        self.assertContains(conta, "987654")

    def test_desconectar_preserva_historico(self):
        integracao = IntegracaoAfiliado.objects.create(
            owner=self.usuario, provedor="shopee", identificador_conta="55",
            token="s", habilitada=True, status="conectada",
        )
        self.client.post(reverse("scraper-shopee-desconectar"))
        integracao.refresh_from_db()
        self.assertEqual(integracao.status, "desativada")
        self.assertFalse(integracao.habilitada)
        self.assertEqual(integracao.token, "")
