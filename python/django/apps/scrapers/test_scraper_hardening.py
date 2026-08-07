from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.scrapers.models import ExecucaoIngestao, FonteIngestao, Produto
from apps.scrapers.scraper_amazon import creators_api
from apps.scrapers.scraper_mercadolivre import link as ml_link


class LinkBuilderHardeningTests(SimpleTestCase):
    def test_rejeicao_brasil_e_terminal(self):
        with self.assertRaises(ml_link.UrlNaoPermitidaError):
            ml_link._validar_resultado_link(
                "⚠️ Este URL não é do Mercado Livre Brasil."
            )

    def test_url_isca_rejeita_host_externo_e_credenciais(self):
        self.assertIsNone(
            ml_link._montar_url_isca("https://example.com/MLB-123456789", "")
        )
        self.assertIsNone(
            ml_link._montar_url_isca(
                "https://usuario:senha@produto.mercadolivre.com.br/MLB-123456789",
                "",
            )
        )

    def test_url_isca_normaliza_http_para_https(self):
        self.assertEqual(
            ml_link._montar_url_isca(
                "http://produto.mercadolivre.com.br/MLB-123456789", "camp"
            ),
            "https://produto.mercadolivre.com.br/MLB-123456789?coupon_campaign_id=camp",
        )

    @patch.object(ml_link, "_linkbuilder_pronto", return_value=False)
    @patch.object(ml_link, "_registrar_veredito_lb")
    @patch.object(ml_link, "_pagina_de_login", return_value=False)
    @patch.object(ml_link, "_pagina_intersticial", return_value=False)
    def test_formulario_ausente_interrompe_antes_do_lote(
        self, _intersticial, _login, registrar, _pronto
    ):
        """Página abre sem login nem challenge, mas sem o formulário esperado.

        É o caso de fallback/experimento/layout novo do ML: sem esta parada, o
        loop marcaria os 40 produtos do lote como falhos por um seletor ausente.
        O veredito tem de ser inconclusivo — a conexão do usuário continua boa.
        """
        page = Mock()
        usuario = Mock()

        with self.assertRaisesRegex(ml_link.AuthError, "não ficou disponível"):
            ml_link._abrir_link_builder(page, usuario=usuario)

        registrar.assert_called_with(
            usuario,
            "inconclusivo",
            "os controles do Link Builder não ficaram disponíveis",
        )


class ScraperPersistenceHardeningTests(TestCase):
    def test_url_de_tracking_longa_e_reduzida_antes_do_banco(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            _normalizar_link_produto,
        )

        longa = (
            "https://click1.mercadolivre.com.br/mclics/click?"
            + "tracking=" + ("x" * 1200)
            + "&pdp_filters=item_id%3AMLB123456789"
        )

        self.assertEqual(
            _normalizar_link_produto(longa),
            "https://produto.mercadolivre.com.br/MLB-123456789",
        )

    def test_url_sem_item_perde_query_e_respeita_limite_do_model(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            _normalizar_link_produto,
        )

        longa = "https://www.mercadolivre.com.br/ofertas?tracking=" + ("x" * 1200)
        normalizada = _normalizar_link_produto(longa)

        self.assertEqual(normalizada, "https://www.mercadolivre.com.br/ofertas")
        self.assertLessEqual(len(normalizada), 1000)

    def test_lane_rapida_reutiliza_produto_ja_coletado_por_cupom(self):
        from unittest.mock import patch
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _upsert_ofertas

        url = "https://produto.mercadolivre.com.br/MLB-123456789"
        existente = Produto.objects.create(
            marketplace="mercadolivre",
            owner=None,
            origem="cupom_codigo",
            nome="Produto com cupom",
            preco_sem_desconto=120,
            preco_com_cupom=90,
            link_produto=url,
        )
        coletado = [{
            "nome": "Produto no feed",
            "preco_sem_desconto": 120,
            "preco_com_cupom": 80,
            "link_produto": url,
            "imagem_url": "https://img.example/item.jpg",
            "frete_full": True,
            "relampago": False,
        }]

        with patch("apps.scrapers.precos.registrar"):
            total = _upsert_ofertas(coletado)

        self.assertEqual(total, 1)
        self.assertEqual(Produto.objects.count(), 1)
        existente.refresh_from_db()
        self.assertEqual(existente.origem, "oferta")
        self.assertEqual(existente.preco_com_cupom, 80)

    def test_upsert_atualiza_a_observacao_mais_recente_se_ja_houver_duplicatas(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _upsert_resiliente

        url = "https://produto.mercadolivre.com.br/MLB-987654321"
        antigo = Produto.objects.create(
            marketplace="mercadolivre", owner=None, origem="cupom_codigo",
            nome="Observação antiga", preco_sem_desconto=100,
            preco_com_cupom=90, link_produto=url,
        )
        recente = Produto.objects.create(
            marketplace="mercadolivre", owner=None, origem="oferta",
            nome="Observação recente", preco_sem_desconto=100,
            preco_com_cupom=85, link_produto=url,
        )

        produto, criado = _upsert_resiliente(
            marketplace="mercadolivre", owner=None, link_produto=url,
            defaults={"origem": "oferta", "nome": "Atualizado",
                      "preco_sem_desconto": 100, "preco_com_cupom": 80},
        )

        self.assertFalse(criado)
        self.assertEqual(produto.pk, recente.pk)
        self.assertEqual(produto.nome, "Atualizado")
        antigo.refresh_from_db()
        self.assertEqual(antigo.nome, "Observação antiga")

    def test_busca_vazia_preserva_catalogo(self):
        existente = Produto.objects.create(
            marketplace="mercadolivre", origem="busca", nome="Fone Bluetooth",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
        )
        page = Mock()
        page.locator.return_value.all.return_value = []

        class Browser:
            def __enter__(self):
                return page, Mock()

            def __exit__(self, *_args):
                return False

        with patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper.iniciar_browser",
            return_value=Browser(),
        ), patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper._coletar_cards",
            return_value=[],
        ), patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper.pausa_humana"
        ):
            from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
                buscar_por_termo,
            )

            self.assertEqual(buscar_por_termo("fone", max_paginas=1), 0)

        self.assertTrue(Produto.objects.filter(pk=existente.pk).exists())

    def test_reconciliacao_fecha_execucao_abandonada(self):
        fonte = FonteIngestao.objects.create(
            slug="teste-orfa", marketplace="mercadolivre", nome="Teste"
        )
        execucao = ExecucaoIngestao.objects.create(fonte=fonte)
        ExecucaoIngestao.objects.filter(pk=execucao.pk).update(
            iniciada_em=timezone.now() - timedelta(hours=3)
        )

        from apps.scrapers.maintenance import reconciliar_execucoes_ingestao_orfas

        self.assertEqual(reconciliar_execucoes_ingestao_orfas(), 1)
        execucao.refresh_from_db()
        self.assertEqual(execucao.status, "error")
        self.assertIsNotNone(execucao.finalizada_em)

    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_container._ids_por_http",
        return_value=None,
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_container.storage_state",
        return_value=None,
    )
    def test_container_autenticado_sem_sessao_degrada_em_vez_de_falso_sucesso(
        self, _state, _http
    ):
        fonte = FonteIngestao.objects.create(
            slug="ml-container-teste", marketplace="mercadolivre", nome="Teste"
        )
        from apps.scrapers.models import CupomNormalizado

        CupomNormalizado.objects.create(
            fonte=fonte, external_id="container:1", marketplace="mercadolivre",
            titulo="Cupom", estado="ativo",
            regras={
                "container_url": "https://lista.mercadolivre.com.br/_Container_x",
                "is_mar_aberto": False,
            },
        )
        Produto.objects.create(
            marketplace="mercadolivre", origem="oferta", estado="ativo",
            nome="Produto", preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
        )
        from apps.scrapers.scraper_mercadolivre.cupons_container import (
            SessaoMLObrigatoriaError,
            casar_cupons_container,
        )

        with self.assertRaises(SessaoMLObrigatoriaError):
            casar_cupons_container()


class AmazonCredentialHardeningTests(SimpleTestCase):
    @patch.object(creators_api, "_obter_token", side_effect=creators_api.AmazonNotEligible("401"))
    def test_credencial_rejeitada_nao_e_embrulhada(self, _token):
        creds = creators_api.Credenciais("id", "secret", "host", "tag")
        with self.assertRaises(creators_api.AmazonNotEligible):
            creators_api._post("searchItems", {}, creds)


class AmazonTaxonomyTests(SimpleTestCase):
    """A Creators API já classifica o item; descartar isso escondia a loja inteira."""

    def _item(self, nos):
        return {
            "asin": "B0TAXON001",
            "itemInfo": {"title": {"displayValue": "DREAME F10 Robô Aspirador com câmera"}},
            "browseNodeInfo": {"browseNodes": nos},
            "offersV2": {"listings": [{"price": {
                "money": {"amount": 80}, "savingBasis": {"money": {"amount": 100}}}}]},
        }

    def test_categoria_vem_do_browse_node(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        m = az._mapear_item(self._item([
            {"displayName": "Aspiradores de Pó-Água", "isRoot": False},
        ]))

        self.assertEqual(m["categoria"], "Aspiradores de Pó-Água")

    def test_no_raiz_nao_vira_categoria(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        m = az._mapear_item(self._item([
            {"displayName": "Todos os departamentos", "isRoot": True},
            {"displayName": "Eletroportáteis do Lar", "isRoot": False},
        ]))

        self.assertEqual(m["categoria"], "Eletroportáteis do Lar")

    def test_browse_node_corrige_macro_que_o_titulo_erra(self):
        """'... com câmera' batia em 'Áudio, Vídeo e Fotografia' antes de
        'Eletrodomésticos': a lista de palavras-chave é ordenada."""
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            classificar_oferta_por_nome,
        )

        item = self._item([{"displayName": "Aspiradores de Pó e Cuidados com o Chão",
                            "isRoot": False}])
        self.assertEqual(
            classificar_oferta_por_nome(item["itemInfo"]["title"]["displayValue"]),
            "Áudio, Vídeo e Fotografia",
        )
        self.assertEqual(az._mapear_item(item)["macro_sugerida"], "Eletrodomésticos")

    def test_item_sem_browse_node_nao_quebra(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        m = az._mapear_item(self._item([]))

        self.assertEqual(m["categoria"], "")
        self.assertEqual(m["macro_sugerida"], "")


class MLPaginaCategoriaTests(SimpleTestCase):
    """O ML publica o domain_id no payload; perdê-lo esvaziava o filtro da vitrine."""

    def _page(self, texto, existe=True):
        tag = Mock()
        tag.count.return_value = 1 if existe else 0
        tag.text_content.return_value = texto
        page = Mock()
        page.locator.return_value = tag
        return page

    def _payload(self, corpo):
        return f"window._n.ctx.r={corpo};_n.ctx.r.assets=[]"

    def test_le_o_domain_id_de_cada_anuncio(self):
        from apps.scrapers.scraper_mercadolivre.categorias_pagina import mapear_domain_ids

        page = self._page(self._payload(
            '{"results":[{"id":"MLB123456","domain_id":"MLB-VACUUM_CLEANERS"}]}'))

        self.assertEqual(mapear_domain_ids(page), {"MLB123456": "VACUUM_CLEANERS"})

    def test_apelido_de_catalogo_herda_a_categoria_do_anuncio(self):
        from apps.scrapers.scraper_mercadolivre.categorias_pagina import mapear_domain_ids

        page = self._page(self._payload(
            '{"a":[{"id":"MLB999","domain_id":"MLB-CELLPHONES"},'
            '{"product_id":"MLB777","item_id":"MLB999"}]}'))

        mapa = mapear_domain_ids(page)
        self.assertEqual(mapa["MLB777"], "CELLPHONES")

    def test_script_ausente_avisa_em_vez_de_falhar_calado(self):
        """Era o `except Exception: pass`: o catálogo inteiro ia a DESCONHECIDO
        e não sobrava nada no log apontando para a leitura de categoria."""
        from apps.scrapers.scraper_mercadolivre import categorias_pagina

        page = self._page("", existe=False)

        with self.assertLogs(categorias_pagina.logger, level="WARNING") as capturado:
            self.assertEqual(categorias_pagina.mapear_domain_ids(page), {})
        self.assertIn("ausente", " ".join(capturado.output))

    def test_marcador_renomeado_pelo_ml_avisa(self):
        from apps.scrapers.scraper_mercadolivre import categorias_pagina

        page = self._page("window._n.ctx.OUTRO={};fim")

        with self.assertLogs(categorias_pagina.logger, level="WARNING") as capturado:
            self.assertEqual(categorias_pagina.mapear_domain_ids(page), {})
        self.assertIn("marcadores", " ".join(capturado.output).lower())

    def test_json_quebrado_avisa(self):
        from apps.scrapers.scraper_mercadolivre import categorias_pagina

        page = self._page(self._payload('{"results":[nao é json'))

        with self.assertLogs(categorias_pagina.logger, level="WARNING") as capturado:
            self.assertEqual(categorias_pagina.mapear_domain_ids(page), {})
        self.assertIn("ilegível", " ".join(capturado.output))

    def test_id_do_anuncio_sai_do_link_normalizado(self):
        from apps.scrapers.scraper_mercadolivre.categorias_pagina import id_do_anuncio

        self.assertEqual(
            id_do_anuncio("https://www.mercadolivre.com.br/p/MLB123456"), "MLB123456")
        self.assertEqual(id_do_anuncio("https://exemplo.com/sem-id"), "")


class MLTaxonomiaNaoRebaixaTests(SimpleTestCase):
    """Coleta que não descobriu categoria não pode apagar a que outra descobriu."""

    def test_categoria_descoberta_entra_no_update(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _taxonomia

        r = _taxonomia({"nome": "Aspirador", "categoria": "VACUUM_CLEANERS"}, None, {})

        self.assertEqual(r["defaults"]["categoria"], "VACUUM_CLEANERS")

    def test_card_fora_do_payload_nao_apaga_categoria_existente(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _taxonomia

        r = _taxonomia({"nome": "Aspirador", "categoria": ""}, None, {})

        self.assertNotIn("categoria", r["defaults"])
        self.assertEqual(r["create_defaults"]["categoria"], "DESCONHECIDO")

    def test_titulo_que_nao_denuncia_macro_nao_apaga_a_macro_existente(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _taxonomia

        r = _taxonomia({"nome": "Produto XPTO 3000", "categoria": ""}, None, {})

        self.assertNotIn("macro_categoria", r["defaults"])


class AmazonCollectionHardeningTests(SimpleTestCase):
    """Falha total da API não pode virar 'zero ofertas' silencioso."""

    def _creds(self):
        return creators_api.Credenciais("id", "secret", "host", "tag")

    def test_uma_keyword_quebrada_nao_derruba_as_outras(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        def coletar(termo, *args, **kwargs):
            if termo == "ruim":
                raise creators_api.AmazonAPIError("500")
            return [{"asin": "B0OK", "preco_sem_desconto": 100, "preco_com_cupom": 80}]

        with patch.object(az, "_coletar", side_effect=coletar):
            itens = az._coletar_termos(["ruim", "bom"], 15, creds=self._creds())

        self.assertEqual(len(itens), 1)

    def test_todas_as_keywords_quebradas_levantam_erro(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        with patch.object(az, "_coletar",
                          side_effect=creators_api.AmazonAPIError("429")):
            with self.assertRaisesRegex(creators_api.AmazonAPIError, "429"):
                az._coletar_termos(["a", "b"], 15, creds=self._creds())

    def test_credencial_recusada_interrompe_na_primeira_keyword(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        with patch.object(az, "_coletar",
                          side_effect=creators_api.AmazonNotEligible("403")) as coletar:
            with self.assertRaises(creators_api.AmazonNotEligible):
                az._coletar_termos(["a", "b", "c"], 15, creds=self._creds())

        self.assertEqual(coletar.call_count, 1)


def _item_mapeado(asin, nome, de, por, **extra):
    base = {
        "asin": asin, "nome": nome, "preco_sem_desconto": de, "preco_com_cupom": por,
        "link_produto": f"https://www.amazon.com.br/dp/{asin}", "imagem_url": "",
        "frete_full": False, "tem_promocao": False, "rotulo_promo": "",
        "cupom_confirmado": False, "categoria": "", "macro_sugerida": "",
    }
    base.update(extra)
    return base


class AmazonTermSearchTests(TestCase):
    def test_busca_descarta_item_abaixo_do_minimo_pedido(self):
        """`minSavingPercent` é pedido, não filtro: a API devolve 0% junto."""
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        coletados = [
            _item_mapeado("B0COMDESC", "Com desconto", 100.0, 50.0),
            _item_mapeado("B0SEMDESC", "Sem desconto", 90.0, 90.0),
        ]
        with patch.object(az, "_coletar_termos", return_value=coletados):
            total = az.buscar_por_termo("aspirador", min_desconto=15)

        self.assertEqual(total, 1)
        self.assertTrue(Produto.objects.filter(asin="B0COMDESC").exists())
        self.assertFalse(Produto.objects.filter(asin="B0SEMDESC").exists())

    def test_categoria_real_chega_ao_produto(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        coletados = [_item_mapeado(
            "B0CATEG01", "Robô aspirador", 100.0, 50.0,
            categoria="Aspiradores de Pó-Água", macro_sugerida="Eletrodomésticos")]
        with patch.object(az, "_coletar_termos", return_value=coletados):
            az.buscar_por_termo("aspirador", min_desconto=15)

        produto = Produto.objects.get(asin="B0CATEG01")
        self.assertEqual(produto.categoria, "Aspiradores de Pó-Água")
        self.assertEqual(produto.macro_categoria, "Eletrodomésticos")


class AmazonMarketplaceReportingTests(TestCase):
    def test_conta_desconectada_explica_em_vez_de_devolver_zero(self):
        from apps.scrapers.marketplaces.amazon import Amazon
        from apps.scrapers.marketplaces.base import MarketplaceIndisponivel
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        with patch.object(az, "buscar_por_termo",
                          side_effect=creators_api.AmazonConfigError("sem tag")):
            with self.assertRaisesRegex(MarketplaceIndisponivel, "não conectada"):
                Amazon().buscar_por_termo("aspirador")

    def test_feed_e_promocoes_compartilham_uma_unica_coleta(self):
        """Duas varreduras idênticas dobravam o consumo da cota da conta."""
        from django.contrib.auth import get_user_model
        from apps.scrapers.marketplaces.amazon import Amazon
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        user = get_user_model().objects.create_user("amazonfeed", password="x")
        with patch.object(az, "coletar_feed", return_value=[]) as coletar, \
                patch.object(az, "buscar_por_termo", return_value=0):
            self.assertTrue(Amazon()._scrape_usuario(user))

        coletar.assert_called_once_with(user)

    def test_api_totalmente_fora_marca_conta_e_libera_fallback(self):
        from django.contrib.auth import get_user_model
        from apps.scrapers.marketplaces.amazon import Amazon
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        user = get_user_model().objects.create_user("amazondown", password="x")
        with patch.object(az, "coletar_feed",
                          side_effect=creators_api.AmazonAPIError("429 sustentado")):
            self.assertFalse(Amazon()._scrape_usuario(user))

        user.perfil.refresh_from_db()
        self.assertIsNone(user.perfil.amazon_elegivel)
        self.assertIn("indisponível", user.perfil.amazon_ultimo_erro)

    def test_ciclo_publica_resultado_na_tela_de_fontes(self):
        """A linha da Creators API ficava `degraded`/0 para sempre, mesmo coletando."""
        from django.utils import timezone
        from apps.scrapers.marketplaces.amazon import Amazon

        inicio = timezone.now() - timedelta(seconds=5)
        Produto.objects.create(
            marketplace="amazon", asin="B0FONTE001", origem="oferta",
            fonte="amazon-creators-api", nome="Item", preco_sem_desconto=100,
            preco_com_cupom=70, link_produto="https://a/dp/B0FONTE001")

        Amazon()._reportar_fonte(inicio, contas=1, falhas=0)

        fonte = FonteIngestao.objects.get(slug="amazon-creators-api")
        self.assertEqual(fonte.status, "ok")
        self.assertEqual(fonte.ultimo_total, 1)
        self.assertIsNotNone(fonte.ultimo_sucesso)
        self.assertEqual(fonte.erro_publico, "")

    def test_todas_as_contas_falhando_degrada_a_fonte(self):
        from django.utils import timezone
        from apps.scrapers.marketplaces.amazon import Amazon

        Amazon()._reportar_fonte(timezone.now(), contas=2, falhas=2)

        fonte = FonteIngestao.objects.get(slug="amazon-creators-api")
        self.assertEqual(fonte.status, "degraded")
        self.assertEqual(fonte.falhas_consecutivas, 1)
        self.assertIn("Nenhuma conta", fonte.erro_publico)


class CamposDeCampanhaDuplicadosTests(TestCase):
    """A paginação de /cupons/filter repete campanhas entre páginas."""

    def _row(self, campaign_id, titulo):
        return {
            "campaignId": campaign_id, "title": titulo,
            "desconto": {"tipo": "porcentagem", "valor": 10.0},
            "valor_minimo": 0.0, "desconto_maximo": None,
            "link_produtos": "https://lista.mercadolivre.com.br/_Container_x",
            "codigo": "", "validade": None, "restrito": False, "estado": "ativo",
        }

    def test_campanha_repetida_na_varredura_nao_derruba_a_persistencia(self):
        """Duas linhas com a mesma chave no mesmo ON CONFLICT fazem o PostgreSQL
        abortar a instrução inteira — e a varredura completa se perdia."""
        from apps.scrapers.models import Cupom
        from apps.scrapers.scraper_mercadolivre.scraper import (
            _persistir_campanhas_cupons,
        )

        rows = [self._row("C1", "Primeira leitura"),
                self._row("C2", "Outra campanha"),
                self._row("C1", "Leitura mais recente")]

        _persistir_campanhas_cupons(rows, varredura_completa=False)

        self.assertEqual(Cupom.objects.count(), 2)
        self.assertEqual(Cupom.objects.get(campanha_id="C1").titulo,
                         "Leitura mais recente")


class MotivoDeReprovacaoDeCupomTests(TestCase):
    """O funil só dizia "N reprovados"; sem o motivo não dá para agir."""

    def _cupom(self, **kwargs):
        from apps.scrapers.models import CupomNormalizado

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug=kwargs.pop("slug", "mercadolivre-web"),
            defaults={"marketplace": "mercadolivre", "nome": "ML"})
        campos = {
            "fonte": fonte, "marketplace": "mercadolivre", "titulo": "Cupom",
            "external_id": "campanha:123", "codigo": "", "estado": "ativo",
            "regras": {"modo_resgate": "ativacao", "valor_desconto": 10,
                       "container_url": "https://lista.mercadolivre.com.br/_Container_x"},
        }
        campos.update(kwargs)
        return CupomNormalizado.objects.create(**campos)

    def _motivo(self, cupom):
        from apps.scrapers.management.commands.diagnostico_producao import Command

        return Command._motivo_reprovacao(cupom)

    def test_aponta_a_flag_quando_o_cupom_esta_apto(self):
        with self.settings(ML_CUPONS_ATIVACAO_ENABLED=False):
            self.assertIn("ML_CUPONS_ATIVACAO_ENABLED", self._motivo(self._cupom()))

    def test_aponta_container_ausente(self):
        cupom = self._cupom(regras={"modo_resgate": "ativacao", "valor_desconto": 10})
        self.assertIn("container_url", self._motivo(cupom))

    def test_aponta_campanha_ausente_no_external_id(self):
        cupom = self._cupom(external_id="checkout:XPTO")
        self.assertIn("external_id", self._motivo(cupom))

    def test_aponta_site_wide(self):
        cupom = self._cupom(regras={
            "modo_resgate": "ativacao", "valor_desconto": 10, "is_mar_aberto": True,
            "container_url": "https://lista.mercadolivre.com.br/_Container_x"})
        self.assertIn("mar aberto", self._motivo(cupom))


class SessaoMLExpiradaNaoPausaAutomacaoTests(TestCase):
    """Sessão caída é infraestrutura recuperável, não defeito da regra de envio."""

    def test_falha_de_sessao_no_envio_de_produto_e_transitoria(self):
        from django.contrib.auth import get_user_model
        from apps.scrapers import ofertas
        from apps.scrapers.auxiliar import SessaoExpirada
        from apps.scrapers.whatsapp_client import TRANSITORIO

        user = get_user_model().objects.create_user("sessaoml", password="x")
        user.perfil.marcar_verificado()
        produto = Produto.objects.create(
            marketplace="mercadolivre", origem="oferta", nome="Oferta",
            preco_sem_desconto=100, preco_com_cupom=60,
            link_produto="https://produto.mercadolivre.com.br/MLB-1")

        loja = Mock()
        loja.build_affiliate_link.side_effect = SessaoExpirada("sessão caiu")
        with patch("apps.scrapers.marketplaces.registry.get_marketplace",
                   return_value=loja), \
                patch.object(ofertas, "_canal_pronto_ou_erro", return_value=None):
            resultado = ofertas.enviar_oferta_de_produto(
                produto, "123@g.us", usuario=user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["precisa_login_ml"])
        # Sem esta classe, cinco ticks seguidos desligavam a regra (`ativo=False`)
        # e religá-la exigia ação manual mesmo depois de reconectar o ML.
        self.assertEqual(resultado["classe"], TRANSITORIO)
