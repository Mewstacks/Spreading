"""Um cupom só entra numa mensagem quando o escopo dele foi delimitado.

Regressão do incidente de 19/08/2026: o cupom MELIPROMO, que a própria página
oficial de afiliados declara como 25% em ``Vehicle Parts & Accessories``, saiu
anunciado num tablet Lenovo e num jogo de panelas Brinox. Todas as publicações do
dia levaram o mesmo código.

A causa não foi o código do cupom nem a regra de envio: foi
`coupon_products._coletar_ml_remoto` aceitar QUALQUER endereço
``*.mercadolivre.com.br`` como "a lista de produtos deste cupom". Um código
raspado da vitrine é gravado com ``link=/ofertas/cupons``; um cupom cujo container
a fonte não publicou recebe a home do ML como destino. As duas páginas respondem
200 com dezenas de cards de oferta — e cada card virava um ``ProdutoCupom``
"confirmado", que é exatamente o que libera o código a entrar na mensagem.
"""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.scrapers.models import (
    CupomCodigo, CupomNormalizado, FonteIngestao, Produto, ProdutoCupom,
)

# Um card SSR de vitrine: é o que /ofertas/cupons e a home entregam, e é o que o
# preparo lia como se fosse a lista de participantes do cupom.
CARD_DE_VITRINE = """
<div class="poly-card">
  <img class="poly-component__picture" src="https://http2.mlstatic.com/a.jpg">
  <h3><a class="poly-component__title"
     href="https://produto.mercadolivre.com.br/MLB-987654-perfume">
     Perfume Love Lily Eau de Parfum
  </a></h3>
  <div class="poly-price__current">
    <span class="andes-money-amount__fraction">170</span>
    <span class="andes-money-amount__cents">69</span>
  </div>
</div>
"""


class _Resposta:
    status_code = 200
    headers: dict = {}

    def __init__(self, texto=""):
        self.text = texto

    def raise_for_status(self):
        return None


class _Sessao:
    """Sessão HTTP falsa que registra a URL aberta e devolve a vitrine."""

    def __init__(self, corpo, visitadas):
        self.corpo = corpo
        self.visitadas = visitadas

    def get(self, url, timeout=None):
        self.visitadas.append(url)
        return _Resposta(self.corpo)


class EscopoDelimitadoTests(TestCase):
    def setUp(self):
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"})
        self.visitadas = []

    def _cupom(self, *, link, container_url="", codigo="MELIPROMO"):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"checkout:{codigo}:{link[-12:]}",
            marketplace="mercadolivre", titulo=f"Cupom {codigo}", codigo=codigo,
            link=link, redemption_mode="code", estado="ativo", confianca="baixa",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25.0,
                    "valor_minimo": 19.0, "desconto_maximo": 100.0,
                    "modo_resgate": "codigo",
                    "escopo": "Vehicle Parts & Accessories",
                    "container_url": container_url, "container_name": "",
                    "is_mar_aberto": False},
            evidencia={"transport": "public-web", "association": "unverified"},
        )

    def _coletar(self, cupom, corpo=CARD_DE_VITRINE):
        from apps.scrapers import coupon_products
        from apps.scrapers.scraper_mercadolivre import scraper as ml_scraper
        import apps.scrapers.ml_auth as ml_auth

        sessao_original = ml_scraper._ml_http_session
        state_original = ml_auth.storage_state
        ml_scraper._ml_http_session = lambda state: _Sessao(corpo, self.visitadas)
        ml_auth.storage_state = lambda usuario=None: {"cookies": []}
        try:
            return coupon_products._coletar_ml_remoto(cupom, usuario=None)
        finally:
            ml_scraper._ml_http_session = sessao_original
            ml_auth.storage_state = state_original

    def test_pagina_de_cupons_do_ml_nao_e_lista_do_cupom(self):
        cupom = self._cupom(link="https://www.mercadolivre.com.br/ofertas/cupons")

        resultado = self._coletar(cupom)

        self.assertEqual(resultado["veredito"], "escopo_indefinido")
        self.assertEqual(self.visitadas, [], "a vitrine nem deve ser aberta")
        self.assertFalse(ProdutoCupom.objects.filter(cupom=cupom).exists())

    def test_home_do_ml_nao_e_lista_do_cupom(self):
        cupom = self._cupom(link="https://www.mercadolivre.com.br/")

        resultado = self._coletar(cupom)

        self.assertEqual(resultado["veredito"], "escopo_indefinido")
        self.assertFalse(ProdutoCupom.objects.filter(cupom=cupom).exists())

    def test_listagem_publica_continua_provando_produto(self):
        cupom = self._cupom(
            link="https://www.mercadolivre.com.br/ofertas/cupons",
            container_url="https://lista.mercadolivre.com.br/_Container_aff-list-3",
        )

        resultado = self._coletar(cupom)

        self.assertEqual(resultado["veredito"], "itens_provados")
        self.assertEqual(
            self.visitadas, ["https://lista.mercadolivre.com.br/_Container_aff-list-3"])
        self.assertEqual(
            ProdutoCupom.objects.filter(cupom=cupom, status="confirmado").count(), 1)


class PortaoDaMensagemTests(TestCase):
    """Mesmo com um vínculo 'confirmado' herdado, o cupom sem escopo não sai."""

    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"})
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Tablet Lenovo Tab 10.1 64gb",
            link_produto="https://www.mercadolivre.com.br/tablet/p/MLB111",
            imagem_url="https://http2.mlstatic.com/x.jpg",
            preco_sem_desconto=1784.0, preco_com_cupom=953.91, preco_efetivo=953.91,
            estado="ativo", origem="oferta", fonte="mercadolivre-web",
            macro_categoria="Tecnologia", ultima_verificacao=timezone.now(),
        )

    def _cupom(self, *, container_url=""):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="checkout:MELIPROMO",
            marketplace="mercadolivre", titulo="Cupom MELIPROMO",
            codigo="MELIPROMO", link="https://www.mercadolivre.com.br/ofertas/cupons",
            redemption_mode="code", estado="ativo", confianca="baixa",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25.0,
                    "valor_minimo": 19.0, "desconto_maximo": 100.0,
                    "modo_resgate": "codigo",
                    "escopo": "Vehicle Parts & Accessories",
                    "container_url": container_url, "container_name": "",
                    "is_mar_aberto": False},
            evidencia={"association": "unverified"},
        )

    def _confirmar(self, cupom, *, url):
        ProdutoCupom.objects.create(
            produto=self.produto, cupom=cupom, status="confirmado",
            verificado_em=timezone.now(),
            evidencia={"regra": "pagina_oficial", "url": url},
        )

    def test_vinculo_colhido_na_vitrine_nao_publica_o_codigo(self):
        from apps.scrapers.ofertas import (
            _melhor_cupom_normalizado_obj, montar_mensagem,
        )
        cupom = self._cupom()
        self._confirmar(cupom, url="https://www.mercadolivre.com.br/ofertas/cupons")

        self.assertIsNone(
            _melhor_cupom_normalizado_obj(self.produto, usuario=self.usuario))
        mensagem = montar_mensagem(
            self.produto, "https://meli.la/x", None, usuario=self.usuario)
        self.assertNotIn("MELIPROMO", mensagem)

    def test_cupom_com_listagem_publica_sai_com_o_escopo_escrito(self):
        from apps.scrapers.ofertas import montar_mensagem
        cupom = self._cupom(
            container_url="https://lista.mercadolivre.com.br/_Container_aff-list-3")
        self._confirmar(
            cupom, url="https://lista.mercadolivre.com.br/_Container_aff-list-3")

        mensagem = montar_mensagem(
            self.produto, "https://meli.la/x", None, usuario=self.usuario)

        self.assertIn("CUPOM: MELIPROMO", mensagem)
        self.assertIn("Vale em:", mensagem)
        self.assertIn("Vehicle Parts & Accessories", mensagem)

    def test_cupom_de_site_inteiro_nao_ganha_linha_de_escopo(self):
        from apps.scrapers.ofertas import montar_mensagem
        cupom = self._cupom()
        cupom.regras = {**cupom.regras, "is_mar_aberto": True, "escopo": "site inteiro"}
        cupom.save(update_fields=["regras"])

        mensagem = montar_mensagem(
            self.produto, "https://meli.la/x", None, usuario=self.usuario)

        self.assertIn("CUPOM: MELIPROMO", mensagem)
        self.assertNotIn("Vale em:", mensagem)


class CodigoDeCheckoutOrfaoTests(TestCase):
    """`CupomCodigo` sem desconto valia para todo produto e era escolhido sempre."""

    def setUp(self):
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Cafeteira",
            link_produto="https://www.mercadolivre.com.br/cafeteira/p/MLB222",
            imagem_url="https://http2.mlstatic.com/y.jpg",
            preco_sem_desconto=699.0, preco_com_cupom=499.0, preco_efetivo=499.0,
            estado="ativo", origem="oferta", fonte="mercadolivre-web",
            ultima_verificacao=timezone.now(),
        )

    def test_codigo_sem_valor_de_desconto_nao_entra(self):
        from apps.scrapers.ofertas import _melhor_codigo

        CupomCodigo.objects.create(codigo="ORFAO", ativo=True, automatico=False)

        self.assertIsNone(_melhor_codigo(self.produto))

    def test_codigo_curado_com_desconto_continua_entrando(self):
        from apps.scrapers.ofertas import _melhor_codigo

        CupomCodigo.objects.create(codigo="CURADO", ativo=True, automatico=False,
                                   tipo_desconto="porcentagem", valor_desconto=10.0)

        self.assertEqual(_melhor_codigo(self.produto), "CURADO")


class EscopoDelimitadoUnitTests(SimpleTestCase):
    def test_classificacao_por_tipo_de_prova(self):
        from apps.scrapers.coupon_rules import escopo_delimitado

        class _Cupom:
            marketplace = "mercadolivre"

            def __init__(self, regras, link="", evidencia=None, external_id=""):
                self.regras = regras
                self.link = link
                self.evidencia = evidencia or {}
                self.external_id = external_id
                self.codigo = "ABC10"

        sem_lista = _Cupom({"modo_resgate": "codigo"},
                           link="https://www.mercadolivre.com.br/ofertas/cupons")
        self.assertFalse(escopo_delimitado(sem_lista))

        site_inteiro = _Cupom({"modo_resgate": "codigo", "is_mar_aberto": True})
        self.assertTrue(escopo_delimitado(site_inteiro))

        com_container = _Cupom({
            "modo_resgate": "codigo",
            "container_url": "https://lista.mercadolivre.com.br/_Container_x"})
        self.assertTrue(escopo_delimitado(com_container))

        com_listagem = _Cupom({"modo_resgate": "codigo"},
                              link="https://lista.mercadolivre.com.br/_Container_y")
        self.assertTrue(escopo_delimitado(com_listagem))

        com_ids = _Cupom({"modo_resgate": "codigo"}, evidencia={"asins": ["B0ABC"]})
        self.assertTrue(escopo_delimitado(com_ids))


class PurgaDaVitrineTests(TestCase):
    """A migração 0064 apaga o que já foi gravado a partir da vitrine genérica."""

    def _relacao(self, *, codigo, url, regra="pagina_oficial"):
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"})
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id=f"checkout:{codigo}",
            marketplace="mercadolivre", titulo=codigo, codigo=codigo,
            link=url, estado="ativo",
            regras={"modo_resgate": "codigo", "valor_desconto": 25.0},
        )
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome=f"Item {codigo}",
            link_produto=f"https://www.mercadolivre.com.br/{codigo}/p/MLB1",
            imagem_url="https://http2.mlstatic.com/z.jpg",
            preco_sem_desconto=200.0, preco_com_cupom=100.0, preco_efetivo=100.0,
            estado="ativo", origem="oferta", fonte="mercadolivre-web",
            ultima_verificacao=timezone.now(),
        )
        return ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            verificado_em=timezone.now(),
            evidencia={"regra": regra, "url": url},
        )

    def test_apaga_so_o_que_veio_de_pagina_generica(self):
        from importlib import import_module

        from django.apps import apps as registro

        migracao = import_module(
            "apps.scrapers.migrations.0064_purga_associacao_vitrine_generica")

        vitrine = self._relacao(
            codigo="VITRINE",
            url="https://www.mercadolivre.com.br/ofertas/cupons")
        home = self._relacao(codigo="HOME", url="https://www.mercadolivre.com.br/")
        listagem = self._relacao(
            codigo="LISTA",
            url="https://lista.mercadolivre.com.br/_Container_aff-list-3")
        container = self._relacao(
            codigo="CONT",
            url="https://lista.mercadolivre.com.br/_Container_x", regra="container")

        migracao.purgar(registro, None)

        vivos = set(ProdutoCupom.objects.values_list("id", flat=True))
        self.assertNotIn(vitrine.id, vivos)
        self.assertNotIn(home.id, vivos)
        self.assertIn(listagem.id, vivos)
        self.assertIn(container.id, vivos)
