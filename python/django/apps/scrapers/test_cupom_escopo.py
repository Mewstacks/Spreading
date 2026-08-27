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
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection
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


def _projetar(usuario, channel="whatsapp"):
    """Marca como `ready` toda projeção de aviso do catálogo ativo.

    `selecionar_cupons_para_aviso` só enxerga cupom com projeção pronta, e a
    projeção real depende de sessão do ML e link de cupom verificado — condições
    de ambiente, não a regra sob teste. Fixá-las aqui é o que faz o teste falhar
    por causa da regra, e não por ausência de dado.
    """
    from apps.accounts.models import organization_for_user
    from apps.scrapers.models import CupomDisponibilidade, CupomNormalizado

    organization = organization_for_user(usuario)
    for cupom in CupomNormalizado.objects.filter(estado="ativo").exclude(codigo=""):
        CupomDisponibilidade.objects.update_or_create(
            organization=organization, usuario=usuario, cupom=cupom,
            channel=channel, use_mode="code_notice",
            defaults={"stage": "ready", "category": "", "reason_code": "",
                      "safe_detail": "", "retry_at": None},
        )


def _editor():
    """Stub do schema_editor: `_system_context` só precisa da conexão.

    O schema_editor real do SQLite recusa entrar dentro de uma transação, e o
    TestCase mantém uma aberta. O que interessa testar é o caminho da função, e
    ele passa por `schema_editor.connection`.
    """
    return SimpleNamespace(connection=connection)


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
        self.assertFalse(escopo_delimitado(sem_lista, codigos_contestados=set()))

        # `codigos_contestados` explícito mantém o teste sem banco: a checagem de
        # contradição só consulta quando o chamador não trouxe o conjunto pronto.
        site_inteiro = _Cupom({"modo_resgate": "codigo", "is_mar_aberto": True})
        self.assertTrue(escopo_delimitado(site_inteiro, codigos_contestados=set()))
        self.assertFalse(
            escopo_delimitado(site_inteiro, codigos_contestados={"ABC10"}))

        com_container = _Cupom({
            "modo_resgate": "codigo",
            "container_url": "https://lista.mercadolivre.com.br/_Container_x"})
        self.assertTrue(escopo_delimitado(com_container, codigos_contestados=set()))

        com_listagem = _Cupom({"modo_resgate": "codigo"},
                              link="https://lista.mercadolivre.com.br/_Container_y")
        self.assertTrue(escopo_delimitado(com_listagem, codigos_contestados=set()))

        com_ids = _Cupom({"modo_resgate": "codigo"}, evidencia={"asins": ["B0ABC"]})
        self.assertTrue(escopo_delimitado(com_ids, codigos_contestados=set()))


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

        migracao.expirar(registro, _editor())

        vivos = set(ProdutoCupom.objects.filter(
            status="confirmado").values_list("id", flat=True))
        self.assertNotIn(vitrine.id, vivos)
        self.assertNotIn(home.id, vivos)
        self.assertIn(listagem.id, vivos)
        self.assertIn(container.id, vivos)


class SiteInteiroContestadoTests(TestCase):
    """O caso real: a fonte publicou o MESMO código como site inteiro e como recorte.

    Em produção, 20/08/2026: `afiliados:MELIPROMO:site:...` com
    ``is_mar_aberto=True`` e `afiliados:MELIPROMO:geral:...` com escopo
    ``Vehicle Parts & Accessories``, as duas linhas ativas ao mesmo tempo. A
    primeira valia como passe livre e pôs o código em 45 das 73 publicações de 24h.
    """

    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})
        self.produto = Produto.objects.create(
            marketplace="mercadolivre",
            nome="Conjunto de Panelas Brinox Ceramic Life Smart Plus 8 Peças",
            link_produto="https://www.mercadolivre.com.br/panelas/p/MLB333",
            imagem_url="https://http2.mlstatic.com/p.jpg",
            preco_sem_desconto=1099.0, preco_com_cupom=569.04, preco_efetivo=569.04,
            estado="ativo", origem="oferta", fonte="mercadolivre-web",
            macro_categoria="Casa", ultima_verificacao=timezone.now(),
        )

    def _linha(self, sufixo, *, site, escopo):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"afiliados:MELIPROMO:{sufixo}",
            marketplace="mercadolivre", titulo=f"MELIPROMO — 25% OFF ({escopo})",
            codigo="MELIPROMO", link="https://www.mercadolivre.com.br/",
            redemption_mode="code", estado="ativo",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25.0,
                    "valor_minimo": 19.0, "desconto_maximo": 100.0,
                    "modo_resgate": "codigo", "escopo": escopo,
                    "container_url": "", "container_name": "",
                    "is_mar_aberto": site},
            evidencia={"fonte": "afiliados-github"},
        )

    def test_gemeo_estreito_derruba_o_passe_livre(self):
        from apps.scrapers.coupon_rules import site_wide_confiavel
        from apps.scrapers.ofertas import (
            _melhor_cupom_normalizado_obj, montar_mensagem,
        )
        aberto = self._linha("site", site=True, escopo="site inteiro")
        self._linha("geral", site=False, escopo="Vehicle Parts & Accessories")

        self.assertFalse(site_wide_confiavel(aberto))
        self.assertIsNone(
            _melhor_cupom_normalizado_obj(self.produto, usuario=self.usuario))
        mensagem = montar_mensagem(
            self.produto, "https://meli.la/122qTzh", None, usuario=self.usuario)
        self.assertNotIn("MELIPROMO", mensagem)

    def test_sem_gemeo_o_site_inteiro_continua_valendo(self):
        from apps.scrapers.coupon_rules import site_wide_confiavel
        from apps.scrapers.ofertas import _melhor_cupom_normalizado_obj
        aberto = self._linha("site", site=True, escopo="site inteiro")

        self.assertTrue(site_wide_confiavel(aberto))
        escolhido = _melhor_cupom_normalizado_obj(self.produto, usuario=self.usuario)
        self.assertEqual(getattr(escolhido, "id", None), aberto.id)

    def test_contestado_nao_prova_o_catalogo_inteiro(self):
        from apps.scrapers.coupon_products import _site_inteiro, _base_produtos
        aberto = self._linha("site", site=True, escopo="site inteiro")
        self._linha("geral", site=False, escopo="Vehicle Parts & Accessories")

        self.assertFalse(_site_inteiro(aberto))
        self.assertEqual(_base_produtos(aberto, None), [])

    def test_nao_contestado_continua_provando_o_catalogo(self):
        from apps.scrapers.coupon_products import _site_inteiro, _base_produtos
        aberto = self._linha("site", site=True, escopo="site inteiro")

        self.assertTrue(_site_inteiro(aberto))
        self.assertEqual(
            [p.id for p in _base_produtos(aberto, None)], [self.produto.id])

    def test_migracao_expira_os_vinculos_do_contestado(self):
        from importlib import import_module

        from django.apps import apps as registro

        migracao = import_module(
            "apps.scrapers.migrations.0064_purga_associacao_vitrine_generica")
        aberto = self._linha("site", site=True, escopo="site inteiro")
        estreito = self._linha(
            "geral", site=False, escopo="Vehicle Parts & Accessories")
        massa = ProdutoCupom.objects.create(
            produto=self.produto, cupom=aberto, status="confirmado",
            verificado_em=timezone.now(),
            evidencia={"regra": "associacao_comprovada"},
        )
        legitima = ProdutoCupom.objects.create(
            produto=self.produto, cupom=estreito, status="confirmado",
            verificado_em=timezone.now(),
            evidencia={"regra": "container", "item_id": "MLB333"},
        )

        migracao.expirar(registro, _editor())

        massa.refresh_from_db()
        legitima.refresh_from_db()
        self.assertEqual(massa.status, "expirado")
        self.assertEqual(legitima.status, "confirmado")


class MigracaoAbreContextoDeSistemaTests(TestCase):
    """A migração de dados PRECISA abrir `app.system_context` antes de consultar.

    Sem isso o RLS de produção esconde todas as linhas e a migração é registrada
    como aplicada sem ter mudado nada — foi o que aconteceu na primeira aplicação
    da 0064. Em SQLite não há RLS, então a suíte nunca veria o efeito; o que dá
    para travar aqui é o contrato: a função chama o helper.
    """

    def test_expirar_abre_o_contexto_antes_de_consultar(self):
        from importlib import import_module

        from django.apps import apps as registro

        for nome in ("0064_purga_associacao_vitrine_generica",
                     "0065_reaplica_purga_com_contexto"):
            migracao = import_module(f"apps.scrapers.migrations.{nome}")
            origem = import_module(
                "apps.scrapers.migrations.0064_purga_associacao_vitrine_generica")
            chamadas = []
            real = origem._system_context
            origem._system_context = lambda editor: chamadas.append(editor)
            try:
                migracao.expirar(registro, _editor())
            finally:
                origem._system_context = real
            self.assertEqual(len(chamadas), 1, f"{nome} não abriu o contexto")


class AvisoDeCuponsTests(TestCase):
    """O aviso em lote promete ESCOPO, então também não pode repetir a alegação.

    `selecionar_cupons_para_aviso` não passa pelo portão de associação — e não
    deve mesmo, porque a mensagem não promete produto nenhum. Mas cada bloco sai
    com "🏷️ <onde vale>", e para o código desmentido essa linha diria "site
    inteiro" no grupo.
    """

    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})

    def _linha(self, sufixo, *, site, escopo):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"afiliados:MELIPROMO:{sufixo}",
            marketplace="mercadolivre", titulo=f"MELIPROMO — 25% OFF ({escopo})",
            codigo="MELIPROMO", link="https://www.mercadolivre.com.br/",
            redemption_mode="code", estado="ativo",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25.0,
                    "valor_minimo": 19.0, "desconto_maximo": 100.0,
                    "modo_resgate": "codigo", "escopo": escopo,
                    "is_mar_aberto": site},
        )

    def test_mensagem_do_aviso_declara_o_escopo(self):
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons
        estreito = self._linha(
            "geral", site=False, escopo="Vehicle Parts & Accessories")

        texto = montar_mensagem_aviso_cupons([estreito], "mercadolivre")

        self.assertIn("MELIPROMO", texto)
        self.assertIn("Vehicle Parts & Accessories", texto)

    def test_linha_site_inteiro_desmentida_sai_da_selecao(self):
        from types import SimpleNamespace

        from apps.scrapers.ofertas import selecionar_cupons_para_aviso
        aberto = self._linha("site", site=True, escopo="site inteiro")
        self._linha("geral", site=False, escopo="Vehicle Parts & Accessories")
        configuracao = SimpleNamespace(
            marketplace="mercadolivre", canal="whatsapp", grupo_id="g@g.us",
            horas_cooldown=24, incluir_restritos=True,
        )

        _projetar(self.usuario)
        escolhidos = selecionar_cupons_para_aviso(configuracao, self.usuario)

        self.assertNotIn(aberto.id, [c.id for c in escolhidos])
        # A linha estreita do mesmo código continua elegível e leva o escopo certo.
        self.assertIn("MELIPROMO", [c.codigo for c in escolhidos])


class CupomDeComunidadeTests(TestCase):
    """Cupom lido de canal/agregador corrobora fonte oficial; sozinho, não anuncia.

    Em produção, 20/08/2026, o aviso em lote levaria `TODOSITE100` ao grupo:
    extraído por IA de uma mensagem do Telegram, `validade=None`, anunciado como
    "todo site" e sem nenhuma fonte oficial que o tivesse visto.
    """

    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.comunidade, _ = FonteIngestao.objects.get_or_create(
            slug="telegram-publico",
            defaults={"marketplace": "mercadolivre", "nome": "Telegram público"})
        self.oficial, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})

    def _cupom(self, fonte, codigo, *, sufixo=""):
        return CupomNormalizado.objects.create(
            fonte=fonte, external_id=f"{fonte.slug}:{codigo}{sufixo}",
            marketplace="mercadolivre", titulo=f"Cupom {codigo}", codigo=codigo,
            link="https://www.mercadolivre.com.br/", redemption_mode="code",
            estado="ativo",
            regras={"tipo_desconto": "fixo", "valor_desconto": 100.0,
                    "valor_minimo": 999.0, "modo_resgate": "codigo",
                    "escopo": "todo site", "is_mar_aberto": False},
            evidencia={"confianca_origem": "comunidade"}
            if fonte is self.comunidade else {},
        )

    def test_so_comunidade_fica_aguardando(self):
        from apps.scrapers.coupon_readiness import _preflight
        from apps.scrapers.coupon_rules import (
            comunidade_corroborada, cupom_de_comunidade,
        )
        cupom = self._cupom(self.comunidade, "TODOSITE100")

        self.assertTrue(cupom_de_comunidade(cupom))
        self.assertFalse(comunidade_corroborada(cupom))
        resultado = _preflight(cupom, self.usuario)
        self.assertIsNotNone(resultado)
        self.assertEqual(resultado["reason_code"], "community_uncorroborated")
        self.assertEqual(resultado["stage"], "collected")

    def test_corroborado_por_fonte_oficial_passa(self):
        from apps.scrapers.coupon_readiness import _preflight
        from apps.scrapers.coupon_rules import comunidade_corroborada
        cupom = self._cupom(self.comunidade, "TODOSITE100")
        self._cupom(self.oficial, "TODOSITE100", sufixo=":oficial")

        self.assertTrue(comunidade_corroborada(cupom))
        self.assertIsNone(_preflight(cupom, self.usuario))

    def test_fonte_oficial_nunca_precisa_de_corroboracao(self):
        from apps.scrapers.coupon_readiness import _preflight
        from apps.scrapers.coupon_rules import cupom_de_comunidade
        cupom = self._cupom(self.oficial, "MELHORNOML")

        self.assertFalse(cupom_de_comunidade(cupom))
        self.assertIsNone(_preflight(cupom, self.usuario))

    def test_promobit_sozinho_tambem_aguarda_corroboracao(self):
        from apps.scrapers.coupon_readiness import _preflight
        from apps.scrapers.coupon_rules import (
            aguarda_corroboracao_oficial, score_cupom,
        )
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="promobit-cupons",
            defaults={"marketplace": "amazon", "nome": "Promobit"},
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="promobit:amazon:LOJA15",
            marketplace="amazon", titulo="Cupom LOJA15", codigo="LOJA15",
            link="", redemption_mode="code", estado="ativo",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 15.0,
                    "modo_resgate": "codigo", "escopo": "selecao"},
            evidencia={"confianca_origem": "comunidade",
                       "transport": "promobit-next-data"},
        )
        self.assertTrue(aguarda_corroboracao_oficial(cupom))
        resultado = _preflight(cupom, self.usuario)
        self.assertEqual(resultado["stage"], "collected")
        self.assertEqual(resultado["reason_code"], "community_uncorroborated")
        self.assertEqual(score_cupom(cupom), 0)

    def test_corroboracao_em_lote_preserva_loja_e_codigo(self):
        from apps.scrapers.coupon_rules import (
            aguarda_corroboracao_oficial, corroboracoes_oficiais_em_lote,
        )
        ml = self._cupom(self.comunidade, "MESMOCODIGO")
        amazon = CupomNormalizado.objects.create(
            fonte=self.comunidade, external_id="amazon:comunidade:MESMOCODIGO",
            marketplace="amazon", titulo="Cupom Amazon", codigo="MESMOCODIGO",
        )
        self._cupom(self.oficial, "mesmocodigo", sufixo=":oficial")

        corroboracoes = corroboracoes_oficiais_em_lote([ml, amazon])

        self.assertFalse(aguarda_corroboracao_oficial(
            ml, corroboracoes=corroboracoes))
        self.assertTrue(aguarda_corroboracao_oficial(
            amazon, corroboracoes=corroboracoes))


class AvisoSemCodigoRepetidoTests(TestCase):
    """O mesmo código chega por três fontes; a mensagem deve trazê-lo uma vez."""

    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.oficial, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})
        self.promobit, _ = FonteIngestao.objects.get_or_create(
            slug="promobit-cupons",
            defaults={"marketplace": "mercadolivre", "nome": "Promobit"})
        self.telegram, _ = FonteIngestao.objects.get_or_create(
            slug="telegram-publico",
            defaults={"marketplace": "mercadolivre", "nome": "Telegram"})

    def _cupom(self, fonte, *, validade=None):
        return CupomNormalizado.objects.create(
            fonte=fonte, external_id=f"{fonte.slug}:CUPOMDOML",
            marketplace="mercadolivre", titulo="Cupom CUPOMDOML",
            codigo="CUPOMDOML", link="https://www.mercadolivre.com.br/",
            redemption_mode="code", estado="ativo", validade=validade,
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 25.0,
                    "valor_minimo": 29.0, "desconto_maximo": 500.0,
                    "modo_resgate": "codigo", "escopo": "Sellers"},
        )

    def test_uma_linha_por_codigo_e_a_oficial_vence(self):
        from types import SimpleNamespace

        from apps.scrapers.ofertas import selecionar_cupons_para_aviso
        oficial = self._cupom(self.oficial, validade=timezone.now()
                              + timezone.timedelta(days=10))
        self._cupom(self.promobit)
        self._cupom(self.telegram)
        configuracao = SimpleNamespace(
            marketplace="mercadolivre", canal="whatsapp", grupo_id="g@g.us",
            horas_cooldown=24, incluir_restritos=True,
        )

        _projetar(self.usuario)
        escolhidos = selecionar_cupons_para_aviso(configuracao, self.usuario)

        codigos = [c.codigo for c in escolhidos]
        self.assertEqual(codigos.count("CUPOMDOML"), 1)
        if escolhidos:
            self.assertEqual(escolhidos[0].id, oficial.id)
