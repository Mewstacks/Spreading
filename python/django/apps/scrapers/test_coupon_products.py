import base64
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.scrapers.models import (
    CupomNormalizado, CupomPreparacao, FonteIngestao, Produto,
)


class CouponPreparationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("coupon-owner")
        self.other = get_user_model().objects.create_user("coupon-other")
        self.source = FonteIngestao.objects.create(
            slug="coupon-products-tests", marketplace="amazon", nome="Cupons")

    def _coupon(self, **overrides):
        values = {
            "fonte": self.source,
            "external_id": f"coupon-{CupomNormalizado.objects.count()}",
            "marketplace": "amazon",
            "titulo": "20% em livros selecionados",
            "codigo": "LIVRO20",
            "link": "https://www.amazon.com.br/promocao",
            "regras": {"tipo_desconto": "porcentagem", "valor_desconto": 20,
                       "modo_resgate": "codigo"},
            "estado": "ativo",
        }
        values.update(overrides)
        return CupomNormalizado.objects.create(**values)

    def _product(self, owner, **overrides):
        values = {
            "owner": owner, "marketplace": "amazon", "asin": f"ASIN{Produto.objects.count()}",
            "nome": "Livro selecionado", "origem": "oferta", "estado": "ativo",
            "preco_sem_desconto": 120, "preco_com_cupom": 100,
            "link_produto": "https://www.amazon.com.br/dp/ASINTEST",
            "imagem_url": "https://images.example/livro.jpg", "evidencia": {},
        }
        values.update(overrides)
        return Produto.objects.create(**values)

    def test_tema_parecido_nao_cria_associacao_mas_codigo_no_item_cria(self):
        from apps.scrapers.coupon_products import preparar_cupom

        cupom = self._coupon()
        produto = self._product(self.user)
        self.assertEqual(
            preparar_cupom(cupom, self.user, force=True, permitir_rede=False), [])
        self.assertEqual(
            CupomPreparacao.objects.get(cupom=cupom, usuario=self.user).status, "vazio")

        produto.evidencia = {"promotion": {"code": "LIVRO20"}}
        produto.save(update_fields=["evidencia"])
        relacoes = preparar_cupom(cupom, self.user, force=True, permitir_rede=False)

        self.assertEqual([row.produto_id for row in relacoes], [produto.id])
        self.assertEqual(relacoes[0].preco_final, Decimal("80.00"))
        self.assertEqual(
            CupomPreparacao.objects.get(cupom=cupom, usuario=self.user).status, "pronto")

    def test_preparacao_amazon_e_isolada_por_usuario(self):
        from apps.scrapers.coupon_products import ids_cupons_prontos, preparar_cupom

        cupom = self._coupon()
        self._product(self.user, evidencia={"promotional_text": "Use LIVRO20"})
        self._product(
            self.other, asin="ASINOTHER",
            link_produto="https://www.amazon.com.br/dp/ASINOTHER",
            evidencia={"promotional_text": "Use LIVRO20"})

        preparar_cupom(cupom, self.user, force=True, permitir_rede=False)
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), {cupom.id})
        self.assertEqual(ids_cupons_prontos(self.other, [cupom]), set())

        preparar_cupom(cupom, self.other, force=True, permitir_rede=False)
        self.assertEqual(ids_cupons_prontos(self.other, [cupom]), {cupom.id})

    def test_ativacao_amazon_oficial_e_publicavel_com_preco_final(self):
        from apps.scrapers.coupon_products import preparar_cupom
        from apps.scrapers.coupon_rules import cupom_publicavel

        source = FonteIngestao.objects.create(
            slug="amazon-public-coupons", marketplace="amazon",
            nome="Amazon — cupons oficiais",
        )
        cupom = self._coupon(
            owner=self.user, fonte=source, external_id="amazon-coupon:PROMO1",
            codigo="", regras={
                "tipo_desconto": "porcentagem", "valor_desconto": 10,
                "modo_resgate": "ativacao",
            },
            evidencia={
                "association": "amazon-official-coupon-page",
                "promotion_id": "PROMO1", "asins": ["B012345678"],
            },
        )
        produto = self._product(
            self.user, asin="B012345678",
            link_produto="https://www.amazon.com.br/dp/B012345678",
            evidencia={"coupon_final_price": 89.97},
        )

        self.assertTrue(cupom_publicavel(cupom))
        relacoes = preparar_cupom(
            cupom, self.user, force=True, permitir_rede=False)
        self.assertEqual([row.produto_id for row in relacoes], [produto.id])
        self.assertEqual(relacoes[0].preco_final, Decimal("89.97"))

    def test_mudanca_de_regra_invalida_fingerprint_pronto(self):
        from apps.scrapers.coupon_products import ids_cupons_prontos, preparar_cupom

        cupom = self._coupon()
        self._product(self.user, evidencia={"promotion_text": "LIVRO20"})
        preparar_cupom(cupom, self.user, force=True, permitir_rede=False)
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), {cupom.id})

        cupom.regras = {**cupom.regras, "valor_desconto": 25}
        cupom.save(update_fields=["regras"])
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), set())

    def test_calculo_decimal_respeita_minimo_teto_e_arredondamento(self):
        from apps.scrapers.coupon_products import calcular_precos

        produto = self._product(self.user, preco_sem_desconto=197.90,
                                preco_com_cupom=100)
        percentual = self._coupon(
            codigo="DESC33", external_id="percentual",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": "33,33",
                    "modo_resgate": "codigo"})
        self.assertEqual(calcular_precos(percentual, produto)[2], Decimal("66.67"))

        com_teto = self._coupon(
            codigo="TETO", external_id="teto",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 50,
                    "desconto_maximo": 12, "modo_resgate": "codigo"})
        self.assertEqual(calcular_precos(com_teto, produto)[2], Decimal("88.00"))

        minimo = self._coupon(
            codigo="MINIMO", external_id="minimo",
            regras={"tipo_desconto": "fixo", "valor_desconto": 10,
                    "valor_minimo": 101, "modo_resgate": "codigo"})
        self.assertIsNone(calcular_precos(minimo, produto))

    def test_lote_nao_e_bloqueado_por_promocoes_sem_codigo(self):
        from apps.scrapers.coupon_products import preparar_lote

        CupomNormalizado.objects.bulk_create([
            CupomNormalizado(
                owner=self.user, fonte=self.source, external_id=f"activation-{i}",
                marketplace="amazon", titulo=f"Ativação {i}", codigo="",
                link="https://www.amazon.com.br/promocao",
                regras={"modo_resgate": "ativacao"}, estado="ativo",
            )
            for i in range(205)
        ])
        publicavel = self._coupon(owner=self.user, external_id="publicavel-no-lote")
        self._product(
            self.user, evidencia={"promotion": {"code": "LIVRO20"}})

        resultado = preparar_lote(limite=1)

        self.assertEqual(resultado, {"processados": 1, "prontos": 1})
        self.assertEqual(
            CupomPreparacao.objects.get(cupom=publicavel, usuario=self.user).status,
            "pronto",
        )


class CouponMessageTests(SimpleTestCase):
    def _data(self):
        cupom = SimpleNamespace(
            marketplace="mercadolivre", anunciante_nome="", external_id="public:1",
            codigo="PRESENTE", titulo="Cupom", regras={"modo_resgate": "codigo"})
        produto = SimpleNamespace(
            nome=("Livro Chama de Ferro Capa Dura Loja Oficial Frete Grátis "
                  "Edição Especial com Brinde Exclusivo"),
            macro_categoria="Livros, Mídia e Conteúdo",
            preco_sem_desconto=197.90, preco_com_cupom=100,
        )
        relacao = SimpleNamespace(
            preco_original=Decimal("197.90"), preco_final=Decimal("83.54"))
        return cupom, [{"produto": produto, "relacao": relacao,
                        "link": "https://meli.la/1GWNQCg"}]

    def test_whatsapp_tem_negrito_somente_no_cabecalho_e_codigo(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom_produtos

        cupom, itens = self._data()
        mensagem = montar_mensagem_cupom_produtos(cupom, itens)

        self.assertTrue(mensagem.startswith("*Cupom ⚡️ Mercado Livre*"))
        self.assertIn("📖 Livro Chama de Ferro Capa Dura Edição Especial", mensagem)
        self.assertIn("🛒 De R$197,90 por R$83,54", mensagem)
        self.assertIn("➡️ https://meli.la/1GWNQCg", mensagem)
        self.assertTrue(mensagem.endswith("🎟 Use o cupom *PRESENTE*"))
        self.assertEqual(mensagem.count("*"), 4)

    def test_telegram_escapa_html_e_tem_dois_negritos(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom_produtos
        from apps.scrapers.senders.base import TelegramHTMLMarkup

        cupom, itens = self._data()
        itens[0]["produto"].nome = "Livro <Especial> & Capa dura"
        mensagem = montar_mensagem_cupom_produtos(
            cupom, itens, markup=TelegramHTMLMarkup())

        self.assertEqual(mensagem.count("<b>"), 2)
        self.assertEqual(mensagem.count("</b>"), 2)
        self.assertIn("Livro &lt;Especial&gt; &amp; Capa dura", mensagem)


class MercadoLivreCouponHTMLTests(SimpleTestCase):
    def test_extrai_produtos_do_container_ssr_sem_browser(self):
        from apps.scrapers.coupon_products import _produtos_ml_do_html

        html = """
        <div class="poly-card">
          <img class="poly-component__picture" src="https://http2.mlstatic.com/a.jpg">
          <h3><a class="poly-component__title"
             href="https://produto.mercadolivre.com.br/MLB-123456-produto#x">
             Produto de teste
          </a></h3>
          <s class="andes-money-amount--previous">
            <span class="andes-money-amount__fraction">199</span>
            <span class="andes-money-amount__cents">90</span>
          </s>
          <div class="poly-price__current">
            <span class="andes-money-amount__fraction">149</span>
            <span class="andes-money-amount__cents">99</span>
          </div>
          <svg aria-label="Enviado pelo FULL"></svg>
        </div>
        """
        rows = _produtos_ml_do_html(html)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nome_produto"], "Produto de teste")
        self.assertEqual(rows[0]["preco_original_sem_desconto"], "199.90")
        self.assertEqual(rows[0]["preco_vitrine_atual"], "149.99")
        self.assertTrue(rows[0]["frete_full"])


class CouponCollageTests(SimpleTestCase):
    def _items(self, n):
        return [{"produto": SimpleNamespace(imagem_url=f"https://img.example/{i}.jpg")}
                for i in range(n)]

    def test_colagens_de_1_5_e_9_fotos_sao_jpeg_quadrado_1080(self):
        from PIL import Image
        from apps.scrapers.colagem import montar_colagem_itens

        for quantidade in (1, 5, 9):
            with self.subTest(quantidade=quantidade), patch(
                "apps.scrapers.colagem._baixar_imagem",
                side_effect=lambda _url: Image.new("RGB", (640, 360), "blue"),
            ):
                b64, mime, validos = montar_colagem_itens(self._items(quantidade))
                imagem = Image.open(BytesIO(base64.b64decode(b64)))
                self.assertEqual(mime, "image/jpeg")
                self.assertEqual(imagem.size, (1080, 1080))
                self.assertEqual(len(validos), quantidade)

    def test_falha_parcial_remove_o_mesmo_item_da_foto_e_do_texto(self):
        from PIL import Image
        from apps.scrapers.colagem import montar_colagem_itens

        itens = self._items(3)
        with patch("apps.scrapers.colagem._baixar_imagem", side_effect=[
            Image.new("RGB", (10, 20)), None, Image.new("RGB", (20, 10)),
        ]):
            _b64, _mime, validos = montar_colagem_itens(itens)
        self.assertEqual(validos, [itens[0], itens[2]])

    def test_urls_locais_e_nao_https_sao_rejeitadas(self):
        from apps.scrapers.colagem import _url_publica

        self.assertFalse(_url_publica("http://images.example/a.jpg"))
        self.assertFalse(_url_publica("https://localhost/a.jpg"))
        self.assertFalse(_url_publica("https://127.0.0.1/a.jpg"))


class TelegramCouponMediaTests(SimpleTestCase):
    @patch("apps.scrapers.senders.telegram.requests.post")
    def test_colagem_e_enviada_via_multipart_com_legenda(self, post):
        from apps.scrapers.senders.telegram import TelegramSender

        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"ok": True, "result": {"message_id": 42}}),
        )
        usuario = SimpleNamespace(
            perfil=SimpleNamespace(telegram_bot_token="token-seguro"))
        imagem = base64.b64encode(b"jpeg-bytes").decode("ascii")

        resultado = TelegramSender().enviar_oferta(
            "@canal_teste", "mensagem", imagem_b64=imagem,
            legenda="legenda completa", usuario=usuario)

        self.assertTrue(resultado["sucesso"])
        _url, kwargs = post.call_args
        self.assertEqual(kwargs["data"]["caption"], "legenda completa")
        self.assertEqual(kwargs["files"]["photo"][1], b"jpeg-bytes")
        self.assertEqual(kwargs["files"]["photo"][2], "image/jpeg")
