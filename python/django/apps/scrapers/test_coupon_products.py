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

    def test_teto_do_payload_limita_o_desconto_anunciado(self):
        """10% num item de R$ 500 com "Limite de R$ 10" abatem R$ 10, não R$ 50."""
        from apps.scrapers.coupon_products import calcular_precos

        produto = self._product(self.user, preco_sem_desconto=600,
                                preco_com_cupom=500)
        cupom = self._coupon(
            codigo="CASA10", external_id="teto-real",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 10,
                    "desconto_maximo": 10, "valor_minimo": 89.90,
                    "modo_resgate": "codigo"})
        self.assertEqual(calcular_precos(cupom, produto)[2], Decimal("490.00"))

    def test_compra_minima_em_milhar_bloqueia_item_barato(self):
        """R$ 2.000 de mínimo lidos como R$ 2,00 liberavam qualquer produto."""
        from apps.scrapers.coupon_products import calcular_precos

        produto = self._product(self.user, preco_sem_desconto=350,
                                preco_com_cupom=300)
        cupom = self._coupon(
            codigo="APPLE250", external_id="minimo-milhar",
            regras={"tipo_desconto": "fixo", "valor_desconto": 250,
                    "valor_minimo": 2000.0, "modo_resgate": "codigo"})
        self.assertIsNone(calcular_precos(cupom, produto))

    def test_preco_base_do_cupom_e_a_vitrine_e_nao_acumula_desconto(self):
        """`preco_com_cupom` é a vitrine; o cupom só pode ser descontado uma vez."""
        from apps.scrapers.coupon_products import calcular_precos

        produto = self._product(self.user, origem="cupom",
                                preco_sem_desconto=200, preco_com_cupom=150)
        cupom = self._coupon(
            codigo="MEIA20", external_id="sem-duplo",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "modo_resgate": "codigo"})
        original, atual, final = calcular_precos(cupom, produto)
        self.assertEqual((original, atual, final),
                         (Decimal("200.00"), Decimal("150.00"), Decimal("120.00")))

    def test_codigo_precisa_aparecer_como_palavra_inteira(self):
        from apps.scrapers.coupon_products import _promocao_confirma_codigo

        cupom = self._coupon(codigo="PET15", external_id="palavra-inteira")
        curto = self._coupon(codigo="PET", external_id="curto-demais")
        dentro = self._product(
            self.user, asin="ASINSUB",
            link_produto="https://www.amazon.com.br/dp/ASINSUB",
            evidencia={"promotional_text": "Oferta CARPET15 por tempo limitado"})
        exato = self._product(
            self.user, asin="ASINEXATO",
            link_produto="https://www.amazon.com.br/dp/ASINEXATO",
            evidencia={"promotional_text": "Use o cupom PET15 no carrinho"})

        self.assertFalse(_promocao_confirma_codigo(cupom, dentro))
        self.assertTrue(_promocao_confirma_codigo(cupom, exato))
        # Código de 3 letras não prova associação nenhuma.
        self.assertFalse(_promocao_confirma_codigo(curto, exato))

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

    def _cupom_ml_de_container(self):
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})
        return self._coupon(
            fonte=fonte, marketplace="mercadolivre", codigo="",
            external_id="campanha:99",
            link="https://lista.mercadolivre.com.br/_Container_teste",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25,
                    "modo_resgate": "ativacao",
                    "container_url":
                        "https://lista.mercadolivre.com.br/_Container_teste"},
        )

    @staticmethod
    def _parede_de_login():
        """O que o ML devolve hoje em `lista.`: 200, mas na URL de verificação."""
        resposta = Mock(
            text="<html>Para continuar, acesse sua conta</html>",
            status_code=200,
            url=("https://www.mercadolivre.com.br/gz/account-verification"
                 "?go=https%3A%2F%2Flista.mercadolivre.com.br%2F_Container_teste"),
        )
        resposta.raise_for_status.return_value = None
        return resposta

    @patch("apps.scrapers.auxiliar.iniciar_browser")
    @patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session")
    @patch("apps.scrapers.ml_auth.storage_state", return_value={"cookies": []})
    def test_parede_de_login_nao_vira_cupom_sem_produto(
        self, _storage, http_session, iniciar_browser,
    ):
        """O incidente: 2232 cupons marcados "nenhum produto aplicável" e 6h de
        espera, quando na verdade ninguém conseguiu abrir a listagem."""
        from apps.scrapers.coupon_products import (
            BACKOFF_SEM_SESSAO, ERRO_SESSAO_ML, preparar_cupom,
        )

        http_session.return_value.get.return_value = self._parede_de_login()
        cupom = self._cupom_ml_de_container()

        antes = timezone.now()
        self.assertEqual(preparar_cupom(cupom, self.user, force=True), [])

        preparo = CupomPreparacao.objects.get(cupom=cupom)
        self.assertEqual(preparo.status, "erro")
        self.assertEqual(preparo.erro, ERRO_SESSAO_ML)
        # Espera curta: a sessão volta quando alguém reconecta, não em 6 horas.
        self.assertLess(preparo.proxima_tentativa, antes + BACKOFF_SEM_SESSAO
                        + timezone.timedelta(minutes=1))
        # E o Chromium não é gasto contra a mesma porta fechada.
        iniciar_browser.assert_not_called()

    @patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session")
    @patch("apps.scrapers.ml_auth.storage_state")
    def test_sessao_de_outra_organizacao_destrava_a_listagem_compartilhada(
        self, storage_state, http_session,
    ):
        """O catálogo público é preparado com a sessão de sistema; quando ela
        expira, a esteira inteira parava mesmo com usuários conectados.

        E a fila de candidatos NÃO pode parar no primeiro: em produção o primeiro
        usuário elegível era justamente o dono da sessão de sistema expirada.
        """
        from apps.scrapers.coupon_products import (
            _PAREDES_POR_CREDENCIAL, _coletar_ml_remoto,
        )

        _PAREDES_POR_CREDENCIAL.clear()
        self.addCleanup(_PAREDES_POR_CREDENCIAL.clear)
        sistema, alternativa = {"cookies": ["sistema"]}, {"cookies": ["usuario"]}
        # `self.other` é o dono da sessão morta — a mesma do sistema.
        storage_state.side_effect = lambda quem=None: (
            alternativa if getattr(quem, "id", None) == self.user.id else sistema
        )
        boa = Mock(
            status_code=200,
            url="https://lista.mercadolivre.com.br/_Container_teste",
            text="<html>listagem com produtos</html>",
        )
        boa.raise_for_status.return_value = None
        respostas = {
            id(sistema): self._parede_de_login(),
            id(alternativa): boa,
        }
        http_session.side_effect = lambda state: Mock(
            get=Mock(return_value=respostas[id(state)]),
        )
        cupom = self._cupom_ml_de_container()

        with patch(
            "apps.scrapers.coupon_products._produtos_ml_do_html",
            side_effect=lambda texto, limite=9: [{
                "nome_produto": "Item da campanha",
                "preco_original_sem_desconto": 100.0,
                "preco_vitrine_atual": 80.0,
                "link_produto": "https://produto.mercadolivre.com.br/MLB-1",
                "imagem_url": "https://http2.mlstatic.com/item.jpg",
            }] if texto else [],
        ):
            total = _coletar_ml_remoto(
                cupom, usuario=None,
                credenciais_alternativas=[self.other, self.user],
            )

        self.assertEqual(total, 1)
        self.assertTrue(ProdutoCupom.objects.filter(
            cupom=cupom, status="confirmado").exists())


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
            preco_original=Decimal("197.90"), preco_atual=Decimal("100.00"),
            preco_final=Decimal("83.54"))
        return cupom, [{"produto": produto, "relacao": relacao,
                        "link": "https://meli.la/1GWNQCg"}]

    def test_whatsapp_tem_negrito_somente_no_cabecalho_e_codigo(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom_produtos

        cupom, itens = self._data()
        mensagem = montar_mensagem_cupom_produtos(cupom, itens)

        self.assertTrue(mensagem.startswith("*Cupom Mercado Livre*"))
        self.assertIn("📖 Livro Chama de Ferro Capa Dura Edição Especial", mensagem)
        # "De" é o preço de VITRINE (100), não o de tabela (197,90): a diferença
        # anunciada tem que ser exatamente o que o cupom abate no checkout.
        self.assertIn("🛒 De R$100 por R$83,54", mensagem)
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
    def _items(self, n, marketplace="mercadolivre"):
        return [
            {"produto": SimpleNamespace(
                imagem_url=f"https://img.example/{i}.jpg",
                marketplace=marketplace,
            )}
            for i in range(n)
        ]

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

    def test_produto_amazon_ocupa_o_card_sem_perder_o_fundo_branco(self):
        from PIL import Image, ImageDraw
        from apps.scrapers.colagem import montar_colagem_itens

        origem = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(origem).rectangle((400, 400, 600, 600), fill="black")

        with patch("apps.scrapers.colagem._baixar_imagem", return_value=origem):
            b64, _mime, _validos = montar_colagem_itens(
                self._items(1, marketplace="amazon"))

        imagem = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        pixels_escuros = imagem.convert("L").point(lambda pixel: 255 if pixel < 64 else 0)
        esquerda, topo, direita, base = pixels_escuros.getbbox()

        self.assertGreater(direita - esquerda, 800)
        self.assertGreater(base - topo, 800)
        self.assertGreaterEqual(imagem.getpixel((0, 0))[0], 250)

    def test_miniaturas_pequenas_da_amazon_sao_ampliadas_na_grade(self):
        from PIL import Image, ImageDraw
        from apps.scrapers.colagem import montar_colagem_itens

        miniaturas = []
        for caixa in ((65, 65, 160, 160), (92, 92, 133, 133)):
            miniatura = Image.new("RGB", (226, 226), "white")
            desenho = ImageDraw.Draw(miniatura)
            desenho.rectangle(caixa, fill="black")
            # Artefatos escuros isolados na borda não podem impedir o recorte do
            # espaço branco — é o padrão observado nas miniaturas reais.
            desenho.point(((0, 0), (225, 0), (0, 225), (225, 225)), fill="black")
            miniaturas.append(miniatura)

        with patch("apps.scrapers.colagem._baixar_imagem", side_effect=miniaturas):
            b64, _mime, _validos = montar_colagem_itens(
                self._items(2, marketplace="amazon"))

        imagem = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        larguras = []
        for esquerda_celula in (0, 540):
            celula = imagem.crop((esquerda_celula, 0, esquerda_celula + 540, 1080))
            pixels_escuros = celula.convert("L").point(
                lambda pixel: 255 if pixel < 64 else 0)
            esquerda, _topo, direita, _base = pixels_escuros.getbbox()
            larguras.append(direita - esquerda)

        self.assertGreater(min(larguras), 400)
        self.assertLess(max(larguras) - min(larguras), 40)

    def test_margem_branca_de_outros_marketplaces_nao_e_recortada(self):
        from PIL import Image, ImageDraw
        from apps.scrapers.colagem import montar_colagem_itens

        origem = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(origem).rectangle((400, 400, 600, 600), fill="black")

        with patch("apps.scrapers.colagem._baixar_imagem", return_value=origem):
            b64, _mime, _validos = montar_colagem_itens(self._items(1))

        imagem = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        pixels_escuros = imagem.convert("L").point(lambda pixel: 255 if pixel < 64 else 0)
        esquerda, _topo, direita, _base = pixels_escuros.getbbox()

        self.assertLess(direita - esquerda, 300)

    def test_urls_locais_e_nao_https_sao_rejeitadas(self):
        from apps.scrapers.colagem import _url_publica

        self.assertFalse(_url_publica("http://images.example/a.jpg"))
        self.assertFalse(_url_publica("https://localhost/a.jpg"))
        self.assertFalse(_url_publica("https://127.0.0.1/a.jpg"))


class FotoDoEnvioTests(SimpleTestCase):
    """Teto da mídia que vai para o worker do WhatsApp.

    O worker tem um orçamento único de 55s para preflight + upload. Quando o
    sendMessage começa e estoura esse prazo, ele devolve `resultado: "incerto"` e
    a oferta NÃO é reenviada (para não duplicar no grupo) — a pessoa vê "a entrega
    não pôde ser confirmada". Uma foto na resolução original da loja era o caminho
    mais fácil para chegar lá.
    """

    def test_foto_grande_e_reduzida_para_caber_no_envio(self):
        from PIL import Image
        from apps.scrapers.colagem import (
            LADO_MAXIMO_ENVIO, MAX_BYTES_ENVIO, preparar_jpeg_b64,
        )

        # Ruído aleatório: uma imagem lisa comprime a quase nada e não exercitaria
        # nem a redução de lado nem a queda de qualidade.
        import os
        grande = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))

        b64, mime = preparar_jpeg_b64(grande)

        self.assertEqual(mime, "image/jpeg")
        imagem = Image.open(BytesIO(base64.b64decode(b64)))
        self.assertLessEqual(max(imagem.size), LADO_MAXIMO_ENVIO)
        self.assertLessEqual(len(base64.b64decode(b64)), MAX_BYTES_ENVIO)

    def test_foto_pequena_passa_sem_ser_ampliada(self):
        from PIL import Image
        from apps.scrapers.colagem import preparar_jpeg_b64

        b64, _mime = preparar_jpeg_b64(Image.new("RGB", (640, 360), "blue"))

        self.assertEqual(Image.open(BytesIO(base64.b64decode(b64))).size, (640, 360))

    def test_url_recusada_nao_derruba_o_envio(self):
        """Sem foto o envio segue como texto; o que não pode é estourar."""
        from apps.scrapers.ofertas import _baixar_imagem_b64

        self.assertEqual(_baixar_imagem_b64("https://127.0.0.1/a.jpg"), ("", ""))
        self.assertEqual(_baixar_imagem_b64(""), ("", ""))


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
        from apps.accounts.models import OrganizationFeatureOverride
        self.user = get_user_model().objects.create_user("ml-activation", password="test")
        OrganizationFeatureOverride.objects.create(
            organization=self.user.perfil.active_organization,
            feature="ML_CUPONS_ATIVACAO_ENABLED", state="enabled",
        )
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
        self.assertTrue(cupom_publicavel(self._cupom(), usuario=self.user))

    def test_sem_override_nasce_ligado(self):
        """Funcionalidade básica: sem override a organização já está liberada."""
        outro = get_user_model().objects.create_user("ml-no-activation", password="test")
        self.assertTrue(cupom_publicavel(self._cupom(), usuario=outro))

    def test_override_disabled_ainda_vence(self):
        """O kill switch por organização continua disponível para contenção."""
        from apps.accounts.models import OrganizationFeatureOverride
        outro = get_user_model().objects.create_user("ml-disabled", password="test")
        OrganizationFeatureOverride.objects.create(
            organization=outro.perfil.active_organization,
            feature="ML_CUPONS_ATIVACAO_ENABLED", state="disabled",
        )
        self.assertFalse(cupom_publicavel(self._cupom(), usuario=outro))

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
        """O kill switch global (env var) continua disponível para contenção."""
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

    def test_parede_de_login_encerra_a_passada_sem_gastar_browser(self):
        """Sem isto, cada container restante abria um Chromium para colher a
        mesma tela de login — e era daí que vinha "capacidade de browser ocupada"."""
        from apps.scrapers.scraper_mercadolivre.cupons_container import (
            SessaoMLObrigatoriaError, casar_cupons_container,
        )
        from unittest.mock import Mock, patch

        self._cupons(5)
        parede = Mock(
            status_code=200,
            text="<html>acesse sua conta</html>",
            url="https://www.mercadolivre.com.br/gz/account-verification?go=x",
        )
        with patch("apps.scrapers.scraper_mercadolivre.cupons_container.storage_state",
                   return_value={"cookies": []}), \
             patch("apps.scrapers.scraper_mercadolivre.cupons_container._ml_http_session",
                   return_value=Mock(get=Mock(return_value=parede))), \
             patch("apps.scrapers.auxiliar.iniciar_browser") as iniciar_browser:
            with self.assertRaises(SessaoMLObrigatoriaError):
                casar_cupons_container()
        iniciar_browser.assert_not_called()
        fonte = FonteIngestao.objects.get(slug="ml-public-containers")
        self.assertEqual(fonte.status, "degraded")
        self.assertIn("conexão do Mercado Livre", fonte.erro_publico)

    def test_sessao_de_reserva_assume_quando_a_do_contexto_e_barrada(self):
        """A sessão de sistema pode estar expirada enquanto outra conta está
        conectada — foi exatamente o caso em produção."""
        from apps.scrapers.scraper_mercadolivre.cupons_container import (
            casar_cupons_container,
        )
        from unittest.mock import Mock, patch

        get_user_model().objects.create_user("conta-conectada")
        self._cupons(2)
        parede = Mock(
            status_code=200, text="<html>acesse sua conta</html>",
            url="https://www.mercadolivre.com.br/gz/account-verification?go=x",
        )
        listagem = Mock(
            status_code=200,
            url="https://lista.mercadolivre.com.br/_Container_0",
            text='<a href="https://produto.mercadolivre.com.br/MLB-123456-x">i</a>',
        )
        sessoes = {"morta": Mock(get=Mock(return_value=parede)),
                   "viva": Mock(get=Mock(return_value=listagem))}
        with patch("apps.scrapers.scraper_mercadolivre.cupons_container.storage_state",
                   side_effect=lambda quem=None: "morta" if quem is None else "viva"), \
             patch("apps.scrapers.scraper_mercadolivre.cupons_container._ml_http_session",
                   side_effect=lambda state: sessoes[state]):
            vinculos = casar_cupons_container()

        self.assertGreater(vinculos, 0)
        self.assertTrue(ProdutoCupom.objects.filter(status="confirmado").exists())

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

    def test_mesma_identidade_pode_pertencer_a_cupons_diferentes(self):
        """Deduplicação é por campanha, nunca entre cupons independentes."""
        from apps.scrapers.coupon_products import (
            chave_produtos_cupom, mapa_relacoes_prontas,
        )
        primeiro = self._cupom_pronto("51")
        segundo = self._cupom_pronto("52")
        relacao = ProdutoCupom.objects.get(cupom=segundo)
        produto = relacao.produto
        produto.nome = ProdutoCupom.objects.get(cupom=primeiro).produto.nome
        produto.link_produto = "https://www.mercadolivre.com.br/p/MLB51"
        produto.save(update_fields=["nome", "link_produto"])
        CupomPreparacao.objects.filter(cupom=segundo).update(
            produtos_chave=chave_produtos_cupom(segundo))

        preparadas, prontas = mapa_relacoes_prontas(
            self.user, [primeiro, segundo])

        self.assertIn(primeiro.id, preparadas)
        self.assertIn(segundo.id, preparadas)
        self.assertIn(primeiro.id, prontas)
        self.assertIn(segundo.id, prontas)


class SemanticaDePrecoDoCatalogoMLTests(TestCase):
    """`preco_com_cupom` é a VITRINE, nos dois produtores do catálogo de cupom.

    O caminho legado gravava aqui o preço JÁ descontado e a vitrine no campo do
    preço de lista; `calcular_precos` então descontava o cupom outra vez e a
    mensagem anunciava um valor que loja nenhuma cobrava.
    """

    ROW = {
        "nome_produto": "Cafeteira Expresso",
        "categoria": "CASA",
        "link_produto": "https://www.mercadolivre.com.br/cafeteira/p/MLB1",
        "imagem_url": "https://http2.mlstatic.com/foto.jpg",
        "preco_original_sem_desconto": "250.00",   # preço de lista riscado
        "preco_vitrine_atual": "100.00",           # o que a página mostra
        "preco_final_com_cupom": "80.00",          # 20% do cupom, só p/ filtrar
    }

    def _sincronizar(self):
        from apps.scrapers.scraper_mercadolivre.scraper import (
            _sincronizar_produtos_no_banco,
        )
        _sincronizar_produtos_no_banco([
            {"campaignId": "13975432", "produtos_aplicaveis": [dict(self.ROW)]},
        ])
        return Produto.objects.get(link_produto=self.ROW["link_produto"])

    def test_grava_a_vitrine_e_o_preco_de_lista_nos_campos_certos(self):
        produto = self._sincronizar()
        self.assertEqual(produto.preco_com_cupom, 100.0)   # vitrine
        self.assertEqual(produto.preco_efetivo, 100.0)
        self.assertEqual(produto.preco_sem_desconto, 250.0)  # lista, não a vitrine
        # O preço pós-cupom NÃO é persistido: ele é recalculado na publicação.
        self.assertNotEqual(produto.preco_com_cupom, 80.0)

    def test_cupom_desconta_uma_vez_so(self):
        """O bug: 20% sobre 100 tem de dar 80, não 64."""
        from apps.scrapers.coupon_products import calcular_precos

        produto = self._sincronizar()
        cupom = SimpleNamespace(
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20},
            marketplace="mercadolivre", codigo="", evidencia={})
        original, atual, final = calcular_precos(cupom, produto)

        self.assertEqual(atual, Decimal("100.00"))
        self.assertEqual(final, Decimal("80.00"))
        self.assertEqual(original, Decimal("250.00"))

    def test_paridade_com_o_produtor_novo(self):
        """Os dois caminhos gravam os MESMOS campos — foi a divergência entre eles
        que deixou o desconto ser aplicado duas vezes."""
        from apps.scrapers.coupon_products import _produtos_ml_do_html

        produto_legado = self._sincronizar()
        # O produtor novo lê os cards SSR; monta um card com os mesmos números.
        html = (
            '<div class="poly-card">'
            '<a class="poly-component__title" href="https://ml.com.br/x/p/MLB2">Cafeteira</a>'
            '<img class="poly-component__picture" src="https://http2.mlstatic.com/f.jpg">'
            '<s class="andes-money-amount--previous">'
            '<span class="andes-money-amount__fraction">250</span></s>'
            '<div class="poly-price__current">'
            '<span class="andes-money-amount__fraction">100</span></div>'
            '</div>')
        rows = _produtos_ml_do_html(html, limite=1)
        self.assertTrue(rows, "o card de referência precisa ser parseável")
        row = rows[0]
        self.assertEqual(float(row["preco_vitrine_atual"]), produto_legado.preco_com_cupom)
        self.assertEqual(float(row["preco_original_sem_desconto"]),
                         produto_legado.preco_sem_desconto)


class RevalidacaoDaColagemTests(TestCase):
    """A colagem anuncia "De X por Y" por item, com Y calculado sobre a vitrine
    que estava no banco (até 3h atrás). Aqui ela é medida ao vivo."""

    def setUp(self):
        from apps.scrapers import preco_ao_vivo
        self.preco_ao_vivo = preco_ao_vivo
        self.user = get_user_model().objects.create_user("colagem-user")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML público"})
        self.cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="campanha:99", marketplace="mercadolivre",
            titulo="20% OFF", codigo="", estado="ativo",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "valor_minimo": 50, "modo_resgate": "ativacao"})
        self.produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Cafeteira",
            origem="cupom", campanha_id="99", fonte="mercadolivre-cupom",
            estado="ativo", link_produto="https://ml.com.br/cafeteira/p/MLB1",
            imagem_url="https://http2.mlstatic.com/f.jpg",
            preco_sem_desconto=250.0, preco_com_cupom=100.0, preco_efetivo=100.0)
        self.relacao = ProdutoCupom.objects.create(
            produto=self.produto, cupom=self.cupom, status="confirmado",
            verificado_em=timezone.now(), preco_original=Decimal("250.00"),
            preco_atual=Decimal("100.00"), preco_final=Decimal("80.00"))
        self.itens = [{"produto": self.produto, "relacao": self.relacao,
                       "link": "https://meli.la/abc"}]

    def _revalidar(self, relatorio):
        with patch.object(self.preco_ao_vivo, "sessao_ml", return_value=object()), \
             patch("apps.scrapers.scraper_mercadolivre.link_http.relatorio_de_preco",
                   return_value=relatorio):
            return self.preco_ao_vivo.revalidar_colagem(
                self.cupom, self.itens, usuario=self.user)

    def test_preco_que_caiu_recalcula_a_linha_e_mantem_o_item(self):
        mantidos, removidos = self._revalidar(
            {"preco": 90.0, "preco_de": 250.0, "bloqueio": "", "morto": False})

        self.assertEqual(len(mantidos), 1)
        self.assertEqual(removidos, [])
        # 20% sobre a vitrine viva de 90 -> 72, não os 80 do preparo.
        self.assertEqual(mantidos[0]["relacao"].preco_final, Decimal("72.00"))
        self.assertEqual(mantidos[0]["relacao"].preco_atual, Decimal("90.00"))

    def test_nao_escreve_no_catalogo_compartilhado(self):
        """RLS: sob o contexto do usuário estas linhas não são graváveis. A
        correção tem de vir da mutação em memória."""
        self._revalidar({"preco": 90.0, "preco_de": 250.0, "bloqueio": "", "morto": False})

        self.relacao.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(self.relacao.preco_final, Decimal("80.00"))
        self.assertEqual(self.produto.preco_com_cupom, 100.0)

    def test_item_que_sai_das_regras_do_cupom_e_removido(self):
        """Abaixo do valor mínimo não existe linha "De X por Y" verdadeira."""
        mantidos, removidos = self._revalidar(
            {"preco": 40.0, "preco_de": 250.0, "bloqueio": "", "morto": False})

        self.assertEqual(mantidos, [])
        self.assertEqual(len(removidos), 1)

    def test_anuncio_morto_e_removido(self):
        mantidos, removidos = self._revalidar(
            {"preco": 0.0, "preco_de": 0.0, "bloqueio": "", "morto": True})
        self.assertEqual(mantidos, [])
        self.assertEqual(len(removidos), 1)

    def test_bloqueio_mantem_a_linha_preparada(self):
        """Inconclusivo nunca reprova — challenge do ML não pode esvaziar a colagem."""
        mantidos, removidos = self._revalidar(
            {"preco": 0.0, "preco_de": 0.0, "bloqueio": "challenge", "morto": False})

        self.assertEqual(len(mantidos), 1)
        self.assertEqual(removidos, [])
        self.assertEqual(mantidos[0]["relacao"].preco_final, Decimal("80.00"))

    def test_sem_sessao_do_ml_mantem_tudo(self):
        with patch.object(self.preco_ao_vivo, "sessao_ml", return_value=None):
            mantidos, removidos = self.preco_ao_vivo.revalidar_colagem(
                self.cupom, self.itens, usuario=self.user)
        self.assertEqual(len(mantidos), 1)
        self.assertEqual(removidos, [])

    def test_orcamento_estourado_mantem_o_item(self):
        import time as _time

        def lento(*args, **kwargs):
            _time.sleep(1.5)
            return {"preco": 90.0, "preco_de": 250.0, "bloqueio": "", "morto": False}

        with patch.object(self.preco_ao_vivo, "sessao_ml", return_value=object()), \
             patch("apps.scrapers.scraper_mercadolivre.link_http.relatorio_de_preco",
                   side_effect=lento):
            inicio = _time.monotonic()
            mantidos, removidos = self.preco_ao_vivo.revalidar_colagem(
                self.cupom, self.itens, usuario=self.user, orcamento_s=0.2)
            decorrido = _time.monotonic() - inicio

        self.assertEqual(len(mantidos), 1)
        self.assertEqual(removidos, [])
        self.assertLess(decorrido, 1.4, "o orçamento não segurou o envio")


class BackfillDoDescontoDuplicadoTests(TestCase):
    """A migration 0054 conserta o que já está gravado errado.

    A re-raspagem não resolve: `scraper.main` pula toda campanha que já tenha
    produto, então as linhas ficam congeladas com o desconto aplicado duas vezes.
    """

    def _corrigir(self):
        import importlib

        from django.apps import apps as django_apps

        migracao = importlib.import_module(
            "apps.scrapers.migrations.0054_backfill_preco_cupom_duplicado")
        migracao.desfazer_desconto_duplicado(django_apps, None)

    def _produto(self, sufixo, **kw):
        campos = {
            "owner": None, "marketplace": "mercadolivre", "origem": "cupom",
            "fonte": "mercadolivre-cupom", "nome": f"Item {sufixo}",
            "estado": "ativo", "campanha_id": "99",
            "link_produto": f"https://ml.com.br/i{sufixo}/p/MLB{sufixo}",
        }
        campos.update(kw)
        return Produto.objects.create(**campos)

    def test_linha_legada_volta_para_a_vitrine(self):
        # Vitrine 100 no campo do preço de lista, pós-cupom 80 na vitrine.
        legado = self._produto(
            "1", preco_sem_desconto=100.0, preco_com_cupom=80.0,
            preco_fonte=100.0, preco_efetivo=80.0)

        self._corrigir()

        legado.refresh_from_db()
        self.assertEqual(legado.preco_com_cupom, 100.0)
        self.assertEqual(legado.preco_efetivo, 100.0)

    def test_linha_do_produtor_novo_fica_intacta(self):
        novo = self._produto(
            "2", preco_sem_desconto=250.0, preco_com_cupom=100.0,
            preco_fonte=100.0, preco_efetivo=100.0)

        self._corrigir()

        novo.refresh_from_db()
        self.assertEqual(novo.preco_com_cupom, 100.0)
        self.assertEqual(novo.preco_sem_desconto, 250.0)

    def test_oferta_comum_nao_e_tocada(self):
        """origem='oferta' tem de/por legítimos: `por` menor que `de` é normal."""
        oferta = self._produto(
            "3", origem="oferta", fonte="mercadolivre-web",
            preco_sem_desconto=100.0, preco_com_cupom=80.0,
            preco_fonte=100.0, preco_efetivo=80.0)

        self._corrigir()

        oferta.refresh_from_db()
        self.assertEqual(oferta.preco_com_cupom, 80.0)
