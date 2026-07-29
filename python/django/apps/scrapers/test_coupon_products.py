import base64
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.scrapers.coupon_rules import cupom_publicavel
from apps.scrapers.models import (
    CupomNormalizado, CupomPreparacao, FonteIngestao, LinkAfiliadoUsuario, Produto,
    ProdutoCupom,
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
            "link_afiliado": "https://amzn.to/coupon-products-test",
            "imagem_url": "https://images.example/livro.jpg", "evidencia": {},
        }
        values.update(overrides)
        return Produto.objects.create(**values)

    def _verified(self, owner, product):
        return LinkAfiliadoUsuario.objects.create(
            usuario=owner, produto=product, afiliado_ok=True, estado="pronto",
            link_afiliado=f"https://affiliate.example/{owner.id}/{product.id}",
            verificado_ok=True, verificado_em=timezone.now(),
        )

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
        own = self._product(
            self.user, evidencia={"promotional_text": "Use LIVRO20"})
        other = self._product(
            self.other, asin="ASINOTHER",
            link_produto="https://www.amazon.com.br/dp/ASINOTHER",
            evidencia={"promotional_text": "Use LIVRO20"})

        preparar_cupom(cupom, self.user, force=True, permitir_rede=False)
        self._verified(self.user, own)
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), {cupom.id})
        self.assertEqual(ids_cupons_prontos(self.other, [cupom]), set())

        preparar_cupom(cupom, self.other, force=True, permitir_rede=False)
        self._verified(self.other, other)
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
        product = self._product(
            self.user, evidencia={"promotion_text": "LIVRO20"})
        preparar_cupom(cupom, self.user, force=True, permitir_rede=False)
        self._verified(self.user, product)
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), {cupom.id})

        cupom.regras = {**cupom.regras, "valor_desconto": 25}
        cupom.save(update_fields=["regras"])
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), set())

    def test_preparacao_vencida_nao_aparece_como_pronta(self):
        # Preparo "pronto" mas antigo (fora da janela de cache) não pode aparecer na
        # tela: o envio o repreparia e poderia não achar mais produtos. A tela só
        # promete o que o envio consegue montar agora.
        from datetime import timedelta

        from django.utils import timezone

        from apps.scrapers.coupon_products import (
            CACHE_HORAS, ids_cupons_prontos, preparar_cupom)

        cupom = self._coupon()
        product = self._product(
            self.user, evidencia={"promotion_text": "LIVRO20"})
        preparar_cupom(cupom, self.user, force=True, permitir_rede=False)
        self._verified(self.user, product)
        self.assertEqual(ids_cupons_prontos(self.user, [cupom]), {cupom.id})

        vencido = timezone.now() - timedelta(hours=CACHE_HORAS, minutes=1)
        CupomPreparacao.objects.filter(cupom=cupom).update(verificado_em=vencido)
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

    def test_lote_prioriza_cupom_ainda_nao_preparado(self):
        """Um cupom fresco não pode deixar o restante da fila sem vez."""
        from apps.scrapers.coupon_products import (
            atualizar_chave_cupom, preparar_lote,
        )

        fonte = FonteIngestao.objects.create(
            slug="coupon-priority-tests", marketplace="mercadolivre", nome="Cupons ML",
        )
        fresco = CupomNormalizado.objects.create(
            fonte=fonte, external_id="fresco", marketplace="mercadolivre",
            titulo="Cupom fresco", codigo="FRESCO",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10}, estado="ativo",
        )
        pendente = CupomNormalizado.objects.create(
            fonte=fonte, external_id="pendente", marketplace="mercadolivre",
            titulo="Cupom pendente", codigo="PENDENTE",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10}, estado="ativo",
        )
        CupomPreparacao.objects.create(
            cupom=fresco, usuario=None, status="pronto",
            produtos_chave=atualizar_chave_cupom(fresco), verificado_em=timezone.now(),
        )

        with patch("apps.scrapers.coupon_products.preparar_cupom", return_value=[object()]) as preparar:
            resultado = preparar_lote(limite=1)

        self.assertEqual(resultado, {"processados": 1, "prontos": 1})
        self.assertEqual(preparar.call_args.args[0].id, pendente.id)

    def test_limite_padrao_de_preparacao_e_doze(self):
        from apps.scrapers.coupon_products import PREPARO_LOTE_POR_CICLO

        self.assertEqual(PREPARO_LOTE_POR_CICLO, 12)

    @patch("apps.scrapers.auxiliar.iniciar_browser")
    @patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session")
    @patch("apps.scrapers.ml_auth.storage_state", return_value=None)
    @patch("apps.scrapers.ml_auth.avisar_sem_sessao")
    def test_container_sem_sessao_nao_abre_browser(
        self, _avisar, _storage, http_session, iniciar_browser,
    ):
        from apps.scrapers.coupon_products import _coletar_ml_remoto

        resposta = Mock(text="<html>sem produtos</html>")
        resposta.raise_for_status.return_value = None
        http_session.return_value.get.return_value = resposta
        cupom = self._coupon(
            marketplace="mercadolivre",
            link="https://www.mercadolivre.com.br/ofertas/cupons/teste",
            regras={
                "container_url":
                    "https://www.mercadolivre.com.br/ofertas/cupons/teste",
            },
        )

        self.assertEqual(_coletar_ml_remoto(cupom), 0)
        iniciar_browser.assert_not_called()


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

        self.assertTrue(mensagem.startswith("*Cupom Mercado Livre*"))
        self.assertIn("📖 Livro Chama de Ferro Capa Dura Edição Especial", mensagem)
        self.assertIn("🛒 De R$197,90 por R$83,54", mensagem)
        self.assertIn("➡️ https://meli.la/1GWNQCg", mensagem)
        self.assertTrue(mensagem.endswith("🎟 Use o cupom *PRESENTE*"))
        self.assertEqual(mensagem.count("*"), 4)

    def test_cupom_inclui_chamada_ia_sem_negrito_e_nome_resumido(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom_produtos

        cupom, itens = self._data()
        produto = itens[0]["produto"]
        # Caches antigos podiam guardar os próprios asteriscos da IA.
        produto.frase_llm = "*BORA RENOVAR ESSE SETUP*"
        produto.nome_llm = "Livro Chama de Ferro Capa Dura"

        mensagem = montar_mensagem_cupom_produtos(cupom, itens)

        self.assertTrue(mensagem.startswith(
            "BORA RENOVAR ESSE SETUP\n\n*Cupom Mercado Livre*"
        ))
        self.assertNotIn("*BORA RENOVAR ESSE SETUP*", mensagem)
        self.assertIn("📖 Livro Chama de Ferro Capa Dura", mensagem)
        self.assertNotIn("Brinde Exclusivo", mensagem)
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


class ProductMessageAITests(SimpleTestCase):
    @patch("apps.scrapers.ofertas._conteudo_marketing")
    def test_chamada_ia_nao_recebe_negrito_e_nome_curto_substitui_original(
        self, conteudo
    ):
        from apps.scrapers.ofertas import montar_mensagem

        conteudo.return_value = {
            "titulo": "TELA BRABA PRA JOGAR BONITO",
            "nome_curto": "Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz",
        }
        produto = SimpleNamespace(
            nome=("Monitor Gamer Samsung Odyssey G5 27, Resolução QHD, Taxa de "
                  "atualização de 165Hz & 1ms de tempo de resposta (MPRT), "
                  "Curvatura com 1000R, HDR 10, AMD FreeSync"),
            macro_categoria="Eletrônicos e Informática",
            preco_sem_desconto=2200,
            preco_com_cupom=1799,
            codigo_checkout="",
            marketplace="amazon",
            evidencia={},
        )

        mensagem = montar_mensagem(
            produto, "https://amazon.com.br/dp/ABC?tag=teste", None
        )

        self.assertTrue(mensagem.startswith(
            "TELA BRABA PRA JOGAR BONITO\n\n"
            "💻 *Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz*"
        ))
        self.assertNotIn("*TELA BRABA PRA JOGAR BONITO*", mensagem)
        self.assertNotIn("Curvatura com 1000R", mensagem)


class ProductAICacheTests(TestCase):
    @patch("apps.scrapers.llm.gerar_conteudo")
    def test_chamada_e_nome_curto_sao_gerados_juntos_e_cacheados(self, gerar):
        from apps.scrapers.ofertas import _conteudo_marketing

        gerar.return_value = {
            "titulo": "TELA BRABA PRA JOGAR BONITO",
            "nome_curto": "Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz",
        }
        produto = Produto.objects.create(
            marketplace="amazon",
            nome=("Monitor Gamer Samsung Odyssey G5 27, Resolução QHD, Taxa de "
                  "atualização de 165Hz, HDR 10 e AMD FreeSync"),
            preco_sem_desconto=2200,
            preco_com_cupom=1799,
            link_produto="https://amazon.com.br/dp/ABC",
        )

        primeiro = _conteudo_marketing(produto)
        produto.refresh_from_db()
        segundo = _conteudo_marketing(produto)

        self.assertEqual(primeiro, segundo)
        self.assertEqual(
            produto.nome_llm,
            "Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz",
        )
        self.assertEqual(produto.frase_llm, "TELA BRABA PRA JOGAR BONITO")
        gerar.assert_called_once()


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


@override_settings(ML_CUPONS_ATIVACAO_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
class CupomAtivacaoMercadoLivreTests(TestCase):
    """O ML migrou /ofertas/cupons para cupons de ATIVAÇÃO (clique, sem código
    digitável). Sem este ramo, 2357 de 2379 cupons ficavam inpublicáveis."""

    def setUp(self):
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})

    def _cupom(self, **kw):
        regras = {"tipo_desconto": "porcentagem", "valor_desconto": 60,
                  "valor_minimo": None, "desconto_maximo": None,
                  "modo_resgate": "ativacao", "escopo": "Itens para Casa",
                  "container_url": "https://lista.mercadolivre.com.br/_Container_13975432",
                  "container_name": "13975432", "is_mar_aberto": False,
                  "dia_inicio": "", "dia_fim": ""}
        regras.update(kw.pop("regras", {}))
        campos = {"fonte": self.fonte, "external_id": "campanha:13975432",
                  "marketplace": "mercadolivre", "titulo": "60% OFF — Itens para Casa",
                  "codigo": "", "regras": regras, "estado": "ativo"}
        campos.update(kw)
        return CupomNormalizado.objects.create(**campos)

    def test_campanha_com_container_publico_e_publicavel(self):
        self.assertTrue(cupom_publicavel(self._cupom()))

    def test_site_wide_nunca_e_publicavel(self):
        """Sem escopo não há como provar que o desconto se aplica ao item."""
        cupom = self._cupom(regras={"is_mar_aberto": True})
        self.assertFalse(cupom_publicavel(cupom))

    def test_sem_container_nao_e_publicavel(self):
        self.assertFalse(cupom_publicavel(self._cupom(regras={"container_url": ""})))

    def test_sem_valor_de_desconto_nao_e_publicavel(self):
        """Sem valor não há promessa a fazer na mensagem."""
        self.assertFalse(cupom_publicavel(self._cupom(regras={"valor_desconto": None})))

    def test_sem_campanha_no_external_id_nao_e_publicavel(self):
        self.assertFalse(cupom_publicavel(self._cupom(external_id="checkout:XPTO10")))

    @override_settings(ML_CUPONS_ATIVACAO_ENABLED=False)
    def test_flag_desligada_mantem_comportamento_anterior(self):
        """A flag nasce desligada: ligar joga milhares de cupons no ranking de envio
        de uma vez, e o worker publica em grupo real."""
        self.assertFalse(cupom_publicavel(self._cupom()))

    def test_amazon_continua_intacta(self):
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="amazon-public-coupons",
            defaults={"marketplace": "amazon", "nome": "Amazon"})
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="promo:1", marketplace="amazon",
            titulo="Cupom Amazon", codigo="", estado="ativo",
            regras={"modo_resgate": "ativacao"},
            evidencia={"association": "amazon-official-coupon-page",
                       "promotion_id": "P1", "asins": ["B01"]})
        self.assertTrue(cupom_publicavel(cupom))


@override_settings(ML_CUPONS_ATIVACAO_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
class ProdutoDeCupomPrecisaCarregarACampanhaTests(TestCase):
    """O link enviado é o do PRODUTO, e ele só carrega coupon_campaign_id quando
    produto.campanha_id está preenchido. Produto casado só pelo container vem do
    feed com campanha vazia: divulgá-lo diria "ative o cupom" e a pessoa cairia
    numa página sem cupom nenhum."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("cupomml", password="test")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})
        self.cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="campanha:777", marketplace="mercadolivre",
            titulo="20% OFF", codigo="", estado="ativo",
            regras={"modo_resgate": "ativacao", "valor_desconto": 20,
                    "tipo_desconto": "porcentagem", "is_mar_aberto": False,
                    "container_url": "https://lista.mercadolivre.com.br/_Container_777"})

    def _produto(self, nome, campanha_id):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="cupom",
            campanha_id=campanha_id, preco_sem_desconto=100, preco_com_cupom=80,
            imagem_url="https://img/x.jpg",
            link_produto=f"https://www.mercadolivre.com.br/p/MLB{nome}")

    def test_produto_da_pagina_oficial_entra(self):
        from apps.scrapers.coupon_products import _base_produtos
        certo = self._produto("111111", "777")
        self.assertIn(certo, _base_produtos(self.cupom, self.user))

    def test_produto_casado_so_pelo_container_fica_de_fora(self):
        from apps.scrapers.coupon_products import _base_produtos
        do_feed = self._produto("222222", "")           # veio do /ofertas
        ProdutoCupom.objects.create(                    # casar_cupons_container
            produto=do_feed, cupom=self.cupom, status="confirmado",
            evidencia={"regra": "container"})
        self.assertNotIn(do_feed, _base_produtos(self.cupom, self.user))

    def test_cupom_de_codigo_nao_exige_campanha_no_produto(self):
        """Com código digitável o desconto é aplicado no checkout: o link não
        precisa carregar a campanha."""
        from apps.scrapers.coupon_products import _base_produtos
        self.cupom.codigo = "TECH20"
        self.cupom.regras = {**self.cupom.regras, "modo_resgate": "codigo"}
        self.cupom.save()
        do_feed = self._produto("333333", "")
        ProdutoCupom.objects.create(produto=do_feed, cupom=self.cupom,
                                    status="confirmado", evidencia={"regra": "container"})
        self.assertIn(do_feed, _base_produtos(self.cupom, self.user))


@override_settings(ML_CUPONS_ATIVACAO_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
class MensagemDeCupomDeAtivacaoTests(TestCase):
    """Cupom sem código digitável não pode instruir o leitor a digitar nada."""

    def test_mensagem_manda_ativar_e_nunca_oferece_codigo(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:999", marketplace="mercadolivre",
            titulo="30% OFF — Tecnologia", codigo="", estado="ativo",
            regras={"modo_resgate": "ativacao", "valor_desconto": 30,
                    "tipo_desconto": "porcentagem", "is_mar_aberto": False,
                    "container_url": "https://lista.mercadolivre.com.br/_Container_999"})

        texto = montar_mensagem_cupom(cupom)

        self.assertIn("Ative o cupom no link", texto)
        self.assertNotIn("Use o cupom", texto)


class CasamentoDeContainerTests(TestCase):
    """A varredura pegava TODOS os cupons ativos com container (2357 em
    homologação), a 2 páginas cada, dentro de um try/except que só loga — ela não
    falhava, ela virava o ciclo inteiro."""

    def setUp(self):
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})
        Produto.objects.create(
            marketplace="mercadolivre", nome="Item", origem="oferta", estado="ativo",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456")

    def _cupons(self, quantos):
        for i in range(quantos):
            CupomNormalizado.objects.create(
                fonte=self.fonte, external_id=f"campanha:{i}", marketplace="mercadolivre",
                titulo=f"Cupom {i}", codigo="", estado="ativo",
                regras={"container_url": f"https://lista.mercadolivre.com.br/_Container_{i}",
                        "is_mar_aberto": False, "modo_resgate": "ativacao"})

    def test_limite_de_cupons_por_passada(self):
        from apps.scrapers.scraper_mercadolivre.cupons_container import casar_cupons_container
        self._cupons(30)
        chamadas = []

        def coletor(url, paginas):
            chamadas.append(url)
            return set()

        casar_cupons_container(coletor=coletor, limite_cupons=10)
        self.assertEqual(len(chamadas), 10)

    def test_orcamento_de_tempo_interrompe(self):
        from apps.scrapers.scraper_mercadolivre.cupons_container import casar_cupons_container
        self._cupons(10)
        chamadas = []

        def coletor(url, paginas):
            chamadas.append(url)
            return set()

        casar_cupons_container(coletor=coletor, orcamento_s=0)
        self.assertEqual(chamadas, [])

    def test_nunca_casados_vem_primeiro(self):
        from apps.scrapers.scraper_mercadolivre.cupons_container import _cupons_de_container
        self._cupons(3)
        antigo = CupomNormalizado.objects.get(external_id="campanha:0")
        produto = Produto.objects.first()
        ProdutoCupom.objects.create(
            produto=produto, cupom=antigo, status="confirmado",
            verificado_em=timezone.now(), evidencia={"regra": "container"})

        ordem = _cupons_de_container(limite=3)

        # O já casado vai para o fim; os nunca casados na frente.
        self.assertEqual(ordem[-1].id, antigo.id)

    def test_extrai_ids_do_html_sem_browser(self):
        from apps.scrapers.scraper_mercadolivre.cupons_container import _ids_do_html
        html = ('<a href="https://produto.mercadolivre.com.br/MLB-123456-x">a</a>'
                '<a href="/p/MLB987654?ref=1">b</a>'
                '<a href="https://exemplo.com/nada">c</a>')
        self.assertEqual(_ids_do_html(html), {"MLB123456", "MLB987654"})


class MapaDeRelacoesEmLoteTests(TestCase):
    """A tela chamava relacoes_prontas_para_envio (3 queries) por cupom do catálogo
    inteiro — ~7 mil queries com os 2379 de homologação, quase todas descartadas
    logo depois pelo filtro de publicáveis."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("lote", password="test")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})

    def _cupom_pronto(self, sufixo, *, com_link=True, preparado=True):
        from apps.scrapers.coupon_products import chave_produtos_cupom
        cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"campanha:{sufixo}",
            marketplace="mercadolivre", titulo=f"Cupom {sufixo}", codigo=f"COD{sufixo}",
            estado="ativo", regras={"modo_resgate": "codigo", "valor_desconto": 10})
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome=f"Produto {sufixo}", origem="cupom",
            preco_sem_desconto=100, preco_com_cupom=80,
            imagem_url="https://img/x.jpg",
            link_produto=f"https://www.mercadolivre.com.br/p/MLB{sufixo}")
        ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            preco_original=Decimal("100.00"), preco_atual=Decimal("90.00"),
            preco_final=Decimal("80.00"))
        if preparado:
            CupomPreparacao.objects.create(
                cupom=cupom, usuario=None, status="pronto",
                produtos_chave=chave_produtos_cupom(cupom),
                verificado_em=timezone.now())
        if com_link:
            LinkAfiliadoUsuario.objects.create(
                usuario=self.user, produto=produto, estado="pronto",
                link_afiliado=f"https://meli.la/{sufixo}", afiliado_ok=True,
                verificado_ok=True)
        return cupom

    def test_numero_de_queries_nao_cresce_com_a_quantidade(self):
        from apps.scrapers.coupon_products import mapa_relacoes_prontas
        poucos = [self._cupom_pronto(f"1{i}") for i in range(2)]
        with self.assertNumQueries(3):
            mapa_relacoes_prontas(self.user, poucos)

        muitos = poucos + [self._cupom_pronto(f"2{i}") for i in range(20)]
        with self.assertNumQueries(3):
            mapa_relacoes_prontas(self.user, muitos)

    def test_paridade_com_a_versao_por_cupom(self):
        """A versão single é a fonte do ENVIO. Se as duas divergirem, a tela oferece
        o que o envio recusa — exatamente o defeito que link_validacao documenta ter
        custado caro antes."""
        from apps.scrapers.coupon_products import (
            mapa_relacoes_prontas, relacoes_prontas_para_envio,
        )
        casos = [
            self._cupom_pronto("31"),                        # pronto
            self._cupom_pronto("32", com_link=False),        # preparado, sem link
            self._cupom_pronto("33", preparado=False),       # nem preparado
        ]
        _, prontas = mapa_relacoes_prontas(self.user, casos)
        for cupom in casos:
            esperado = bool(relacoes_prontas_para_envio(cupom, self.user))
            self.assertEqual(cupom.id in prontas, esperado,
                             f"divergência no cupom {cupom.external_id}")

    def test_preparados_inclui_quem_ainda_nao_tem_link(self):
        """A tela usa os preparados-sem-link para dizer 'aguardando link'."""
        from apps.scrapers.coupon_products import mapa_relacoes_prontas
        sem_link = self._cupom_pronto("41", com_link=False)
        preparadas, prontas = mapa_relacoes_prontas(self.user, [sem_link])
        self.assertIn(sem_link.id, preparadas)
        self.assertNotIn(sem_link.id, prontas)
