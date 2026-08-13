"""Isolamento entre lojas no ciclo de vida do link de afiliado.

Regressão que originou este arquivo: `verificar_links_pendentes` do Mercado Livre
varria `LinkAfiliadoUsuario` sem recorte de loja. Todo link da Amazon entrava
naquela fila, era aberto no Chromium com a regra do ML e reprovado com "O link não
abre uma página de produto do Mercado Livre" — 47 links válidos em produção, alguns
levados até o estado terminal `nao_afiliavel`.
"""
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import Perfil
from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, LinkAfiliadoUsuario, Produto,
)


def _produto(marketplace, **extra):
    padrao = {
        "marketplace": marketplace, "nome": f"Produto {marketplace}",
        "preco_sem_desconto": 100.0, "preco_com_cupom": 80.0, "estado": "ativo",
        "origem": "oferta",
        "link_produto": (
            "https://www.amazon.com.br/dp/B0ABCDEFGH" if marketplace == "amazon"
            else "https://produto.mercadolivre.com.br/MLB-123456789"
        ),
        "imagem_url": "https://img.example/1.jpg",
    }
    if marketplace == "amazon":
        padrao["asin"] = "B0ABCDEFGH"
    padrao.update(extra)
    return Produto.objects.create(**padrao)


class MarketplaceIsolationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("isolamento")
        Perfil.objects.update_or_create(
            user=self.user, defaults={"afiliado_tag_amazon": "minhatag-20"},
        )
        # O Perfil é criado por signal e fica cacheado na instância do usuário; sem
        # recarregar, `tag_amazon` lê o objeto antigo (tag vazia).
        self.user.refresh_from_db()
        self.amazon = _produto("amazon")
        self.ml = _produto("mercadolivre")

    def test_verificador_ml_nao_carrega_link_amazon(self):
        """A fila do verificador do ML não pode nem enxergar item de outra loja."""
        from apps.scrapers.scraper_mercadolivre.link import verificar_links_pendentes

        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.amazon, estado="pronto",
            afiliado_ok=True, verificado_ok=None,
            link_afiliado="https://www.amazon.com.br/dp/B0ABCDEFGH?tag=minhatag-20",
        )
        with patch(
            "apps.scrapers.carga.browser_resource"
        ) as browser:
            resultado = verificar_links_pendentes(self.user, limite=10)

        # Sem linha elegível a função retorna antes de pedir o navegador.
        browser.assert_not_called()
        self.assertEqual(resultado,
                         {"aprovados": 0, "reprovados": 0, "transitorios": 0})
        linha = LinkAfiliadoUsuario.objects.get(produto=self.amazon)
        self.assertIsNone(linha.verificado_ok)
        self.assertEqual(linha.verificacao_motivo, "")

    def test_verificar_e_aprovar_recusa_produto_de_outra_loja(self):
        from apps.scrapers.scraper_mercadolivre import link as ml_link

        with patch.object(ml_link, "verificar_link_afiliado") as verificador:
            veredito = ml_link.verificar_e_aprovar(
                self.user, self.amazon, "https://www.amazon.com.br/dp/B0ABCDEFGH",
            )
        verificador.assert_not_called()
        self.assertEqual(veredito, "transitorio")

    def test_lane_de_verificacao_pergunta_a_cada_loja(self):
        """O worker roteia pelo adaptador; nenhuma loja recebe item de outra."""
        from apps.scrapers.management.commands.automacao import (
            _rodar_verificacao_links,
        )

        chamadas = {}

        def _registrar(slug):
            def _verificar(usuario, limite=20, produto_ids=None):
                chamadas.setdefault(slug, []).append(usuario)
                return {"aprovados": 1, "reprovados": 0, "transitorios": 0}
            return _verificar

        lojas = {
            slug: SimpleNamespace(verificar_links_pendentes=_registrar(slug))
            for slug in ("mercadolivre", "amazon")
        }
        with patch(
            "apps.scrapers.marketplaces.registry.MARKETPLACES", lojas,
        ):
            total = _rodar_verificacao_links(limite=5)

        self.assertEqual(set(chamadas), {"mercadolivre", "amazon"})
        self.assertEqual(total["aprovados"], 2)
        self.assertEqual(set(total["por_marketplace"]), {"mercadolivre", "amazon"})


class AmazonDeterministicLinkTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("amazon-link")
        Perfil.objects.update_or_create(
            user=self.user, defaults={"afiliado_tag_amazon": "minhatag-20"},
        )
        # O Perfil é criado por signal e fica cacheado na instância do usuário; sem
        # recarregar, `tag_amazon` lê o objeto antigo (tag vazia).
        self.user.refresh_from_db()
        self.produto = _produto("amazon")

    def test_link_gerado_conserva_asin_e_tag_e_nasce_verificado(self):
        from apps.scrapers.scraper_amazon.link import (
            gerar_link_afiliado_para_produto,
        )

        info = gerar_link_afiliado_para_produto(self.produto, usuario=self.user)
        self.assertIn("B0ABCDEFGH", info["link_afiliado"])
        self.assertIn("tag=minhatag-20", info["link_afiliado"])
        linha = LinkAfiliadoUsuario.objects.get(produto=self.produto)
        self.assertIs(linha.verificado_ok, True)
        self.assertEqual(linha.url_canonica, info["link_afiliado"])

    def test_link_sem_tag_do_usuario_nao_e_coerente(self):
        from apps.scrapers.scraper_amazon.link import link_coerente

        self.assertFalse(link_coerente(
            "https://www.amazon.com.br/dp/B0ABCDEFGH?tag=outra-20",
            self.produto, usuario=self.user))
        self.assertFalse(link_coerente(
            "https://www.amazon.com.br/dp/B0OUTROASIN?tag=minhatag-20",
            self.produto, usuario=self.user))
        self.assertTrue(link_coerente(
            "https://www.amazon.com.br/dp/B0ABCDEFGH?tag=minhatag-20",
            self.produto, usuario=self.user))

    def test_conta_sem_tag_nao_gasta_tentativa_por_produto(self):
        """Problema de conta é UM bloqueio, não N falhas de produto."""
        from apps.scrapers.scraper_amazon.link import verificar_links_pendentes

        Perfil.objects.filter(user=self.user).update(afiliado_tag_amazon="")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, estado="pendente",
            verificado_ok=None, link_afiliado="", tentativas=0,
        )
        resultado = verificar_links_pendentes(self.user)

        self.assertEqual(resultado["bloqueados"], 1)
        self.assertEqual(resultado["reason_code"], "amazon_tag_missing")
        linha = LinkAfiliadoUsuario.objects.get(produto=self.produto)
        self.assertEqual(linha.tentativas, 0)
        self.assertEqual(linha.estado, "pendente")


class AmazonLinkRepairTests(TestCase):
    """Reparo idempotente dos links invalidados pelo verificador errado."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("reparo")
        Perfil.objects.update_or_create(
            user=self.user, defaults={"afiliado_tag_amazon": "minhatag-20"},
        )
        # O Perfil é criado por signal e fica cacheado na instância do usuário; sem
        # recarregar, `tag_amazon` lê o objeto antigo (tag vazia).
        self.user.refresh_from_db()
        self.bom = _produto("amazon")
        self.indisponivel = _produto(
            "amazon", asin="B0INDISPON",
            link_produto="https://www.amazon.com.br/dp/B0INDISPON",
        )
        self.afetado = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.bom, afiliado_ok=True,
            link_afiliado="https://www.amazon.com.br/dp/B0ABCDEFGH?tag=minhatag-20",
            verificado_ok=False, estado="nao_afiliavel", tentativas=8,
            verificacao_motivo=(
                "O link não abre uma página de produto do Mercado Livre."),
            ultimo_erro="O link não abre uma página de produto do Mercado Livre.",
        )
        self.legitimo = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.indisponivel, afiliado_ok=True,
            link_afiliado="https://www.amazon.com.br/dp/B0INDISPON?tag=minhatag-20",
            verificado_ok=False, estado="pronto", tentativas=1,
            verificacao_motivo="produto indisponível na Amazon",
        )

    def _reparar(self):
        saida = StringIO()
        call_command("reparar_links_amazon", stdout=saida)
        return saida.getvalue()

    def test_repara_link_valido_e_preserva_reprovacao_legitima(self):
        saida = self._reparar()

        afetado = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self.assertIs(afetado.verificado_ok, True)
        self.assertEqual(afetado.estado, "pronto")
        self.assertEqual(afetado.verificacao_motivo, "")
        self.assertIsNone(afetado.proxima_tentativa)
        self.assertEqual(afetado.url_canonica, afetado.link_afiliado)

        legitimo = LinkAfiliadoUsuario.objects.get(pk=self.legitimo.pk)
        self.assertIs(legitimo.verificado_ok, False)
        self.assertEqual(legitimo.verificacao_motivo,
                         "produto indisponível na Amazon")
        self.assertIn("REPARO", saida)

    def test_execucao_seca_nao_altera_nada(self):
        saida = StringIO()
        call_command("reparar_links_amazon", "--dry-run", stdout=saida)

        afetado = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self.assertIs(afetado.verificado_ok, False)
        self.assertEqual(afetado.estado, "nao_afiliavel")
        self.assertIn("Execução seca", saida.getvalue())

    def test_reparo_e_idempotente(self):
        self._reparar()
        antes = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self._reparar()
        depois = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self.assertEqual(antes.link_afiliado, depois.link_afiliado)
        self.assertEqual(antes.tentativas, depois.tentativas)
        self.assertIs(depois.verificado_ok, True)

    def test_link_malformado_e_regerado_pelo_asin(self):
        LinkAfiliadoUsuario.objects.filter(pk=self.afetado.pk).update(
            link_afiliado="https://www.amazon.com.br/dp/B0ABCDEFGH?tag=antiga-20",
        )
        self._reparar()
        afetado = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self.assertIn("tag=minhatag-20", afetado.link_afiliado)
        self.assertIs(afetado.verificado_ok, True)


class ContencaoDeCapacidadeTests(TestCase):
    """Ficar sem navegador não é "a fonte já está executando"."""

    def _rodar_com_guard(self, acquired, motivo):
        from contextlib import contextmanager

        from apps.scrapers.sources import registry

        @contextmanager
        def _guard(slug, **kwargs):
            yield acquired, motivo

        with patch.object(registry, "_ingestion_guard", _guard):
            return registry.run_source("amazon-public-coupons")

    def test_capacidade_ocupada_tem_motivo_proprio(self):
        payload = self._rodar_com_guard(False, "capacity_deferred")
        self.assertEqual(payload["reason_code"], "capacity_deferred")
        self.assertEqual(payload["offers"], [])

    def test_fonte_ja_executando_mantem_o_motivo_antigo(self):
        payload = self._rodar_com_guard(False, "already_running")
        self.assertEqual(payload["reason_code"], "already_running")

    def test_pipeline_explica_capacidade_sem_dizer_que_esta_rodando(self):
        from apps.scrapers.coupon_pipeline import _coletar_adaptador, _metricas_vazias

        resultado = _metricas_vazias()
        with patch("apps.scrapers.sources.run_source", return_value={
            "status": "running", "offers": [], "coupons": [],
            "reason_code": "capacity_deferred",
        }):
            _coletar_adaptador("amazon-public-coupons", resultado)

        motivo = resultado["fontes"]["amazon-public-coupons"]["motivo"]
        self.assertIn("navegador", motivo)
        self.assertNotIn("já está em execução", motivo)

    def test_dois_ciclos_nao_processam_a_mesma_linha(self):
        """O segundo ciclo não pode reprocessar quem o primeiro já aprovou."""
        from apps.scrapers.scraper_amazon.link import verificar_links_pendentes

        user = get_user_model().objects.create_user("concorrencia")
        Perfil.objects.update_or_create(
            user=user, defaults={"afiliado_tag_amazon": "minhatag-20"},
        )
        user.refresh_from_db()
        produto = _produto("amazon")
        LinkAfiliadoUsuario.objects.create(
            usuario=user, produto=produto, afiliado_ok=True, verificado_ok=None,
            link_afiliado="https://www.amazon.com.br/dp/B0ABCDEFGH?tag=minhatag-20",
        )

        primeiro = verificar_links_pendentes(user)
        segundo = verificar_links_pendentes(user)

        self.assertEqual(primeiro["aprovados"], 1)
        self.assertEqual(segundo["aprovados"], 0)
        self.assertEqual(
            LinkAfiliadoUsuario.objects.filter(usuario=user).count(), 1)


class CapacidadeNaoEhAvariaTests(TestCase):
    """Ficar sem navegador é fila, não defeito — e não pode virar alarme.

    Em produção a lane de links gravava ~12 eventos `links_erro` por hora, cada um
    com traceback, só porque a lane de cupons estava com o único Chromium da
    máquina. Alarme que ninguém pode acionar afoga o log de Saúde e esconde o
    atraso real por capacidade.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("capacidade")
        _produto("mercadolivre")

    def _rodar(self, excecao):
        from apps.scrapers.management.commands.automacao import _rodar_links

        with patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True), \
                patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre."
                      "prefetch_links", side_effect=excecao), \
                patch("apps.scrapers.management.commands.automacao.log_event") as evento:
            return _rodar_links(lote=10), evento

    def test_navegador_ocupado_e_adiamento_sem_evento_de_erro(self):
        from apps.scrapers.carga import BrowserResourceUnavailable

        resultado, evento = self._rodar(
            BrowserResourceUnavailable("Capacidade de browser ocupada"))

        self.assertEqual(resultado["adiados"], 1)
        self.assertEqual(resultado["falhas"], 0)
        evento.assert_not_called()

    def test_sessao_caida_continua_sendo_reportada(self):
        """O silêncio vale só para capacidade: sessão morta exige ação humana."""
        from apps.scrapers.scraper_mercadolivre.link import LoginError

        resultado, evento = self._rodar(LoginError("sessão expirada"))

        self.assertEqual(resultado["adiados"], 0)
        evento.assert_called_once()
        self.assertEqual(evento.call_args[0][1], "links_erro")

    def test_capacidade_e_conta_sao_causas_distintas(self):
        from apps.scrapers.afiliado import causa_de_capacidade, causa_de_conta
        from apps.scrapers.carga import BrowserResourceUnavailable
        from apps.scrapers.scraper_mercadolivre.link import LoginError

        capacidade = BrowserResourceUnavailable("ocupado")
        sessao = LoginError("caiu")
        self.assertEqual(causa_de_capacidade(capacidade), "BrowserResourceUnavailable")
        self.assertEqual(causa_de_capacidade(sessao), "")
        # Ambas continuam impedindo penalidade por produto.
        self.assertTrue(causa_de_conta(capacidade))
        self.assertTrue(causa_de_conta(sessao))
        self.assertEqual(causa_de_conta(ValueError("erro do item")), "")


class ReaberturaDeBloqueiosTests(TestCase):
    """As linhas envenenadas pelo bug antigo precisam de uma passada de limpeza."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("reabertura")

    def _linha(self, nome, erro, **extra):
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto=f"https://produto.mercadolivre.com.br/MLB-{abs(hash(nome)) % 10**8}",
        )
        return LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, ultimo_erro=erro,
            tentativas=8, estado="erro", **extra)

    def test_reabre_conta_e_capacidade_preservando_falha_do_produto(self):
        from apps.scrapers.afiliado import reabrir_bloqueios_de_conta

        sessao = self._linha("por sessão",
                             "Falha operacional de afiliação (LoginError).")
        navegador = self._linha(
            "por navegador",
            "Falha operacional de afiliação (BrowserResourceUnavailable).")
        produto = self._linha(
            "do produto",
            "O Programa de Afiliados não aceitou a URL deste produto.")

        self.assertEqual(reabrir_bloqueios_de_conta(), 2)

        for linha in (sessao, navegador):
            linha.refresh_from_db()
            self.assertEqual(linha.estado, "pendente")
            self.assertEqual(linha.tentativas, 0)
            self.assertEqual(linha.ultimo_erro, "")
            self.assertIsNone(linha.proxima_tentativa)
        produto.refresh_from_db()
        self.assertEqual(produto.estado, "erro")
        self.assertEqual(produto.tentativas, 8)

    def test_nao_desfaz_link_ja_aprovado(self):
        from apps.scrapers.afiliado import reabrir_bloqueios_de_conta

        aprovado = self._linha(
            "aprovado apesar do erro antigo",
            "Falha operacional de afiliação (LoginError).",
            verificado_ok=True, link_afiliado="https://meli.la/ok")

        self.assertEqual(reabrir_bloqueios_de_conta(), 0)
        aprovado.refresh_from_db()
        self.assertIs(aprovado.verificado_ok, True)

    def test_comando_de_manutencao_reabre_e_relata(self):
        self._linha("por sessão", "Falha operacional de afiliação (LoginError).")
        saida = StringIO()
        call_command("reabrir_bloqueios_de_conta", stdout=saida)
        self.assertIn("1", saida.getvalue())

    def test_comando_em_execucao_seca_nao_altera(self):
        linha = self._linha("por sessão",
                            "Falha operacional de afiliação (LoginError).")
        saida = StringIO()
        call_command("reabrir_bloqueios_de_conta", "--dry-run", stdout=saida)
        linha.refresh_from_db()
        self.assertEqual(linha.estado, "erro")
        self.assertIn("seca", saida.getvalue().lower())


class AmazonSendVerdictTests(TestCase):
    """O envio precisa usar o motivo da loja que verificou, e não o do ML."""

    def test_motivo_de_reprovacao_vem_da_loja(self):
        from apps.scrapers.ofertas import _motivo_reprovacao_da_loja

        amazon = SimpleNamespace(slug="amazon")
        motivo = _motivo_reprovacao_da_loja(
            amazon, {"ok": False, "motivo": "produto indisponível"}, True,
        )
        self.assertEqual(motivo, "produto indisponível")
        self.assertNotIn("Mercado Livre", motivo)

        ml = SimpleNamespace(slug="mercadolivre")
        motivo_ml = _motivo_reprovacao_da_loja(
            ml, {"ok": False, "erros": [], "nome_confere": None}, True,
        )
        self.assertIn("Mercado Livre", motivo_ml)


class ReaberturaDeLinksReprovadosNaVitrineTests(TestCase):
    """Passivo da regra que nenhum link do ML podia satisfazer.

    Todo short link do Programa resolve para a vitrine `/social/` do afiliado — os
    de oferta APROVADOS inclusive. Exigir a PDP no destino reprovava 100% dos
    produtos de cupom (0 aprovados em 4.447 em produção). A regra foi corrigida na
    origem; estas linhas precisam voltar para a fila, que de propósito não reabre
    quem já tem veredito.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("vitrine")
        self.produto = _produto("mercadolivre", origem="cupom")
        self.outro = _produto(
            "mercadolivre", origem="cupom",
            link_produto="https://produto.mercadolivre.com.br/MLB-987654321",
        )
        self.afetado = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, afiliado_ok=True,
            link_afiliado="https://meli.la/vitrine", verificado_ok=False,
            estado="pronto", tentativas=3,
            verificacao_motivo=(
                "Caiu na vitrine /social/ (afiliado ok, mas não dá pra confirmar "
                "o cupom do item)."),
        )
        self.legitimo = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.outro, afiliado_ok=True,
            link_afiliado="https://meli.la/pausado", verificado_ok=False,
            estado="pronto", tentativas=1,
            verificacao_motivo="O anúncio está pausado ou não existe mais.",
        )

    def _reabrir(self):
        saida = StringIO()
        call_command("reabrir_links_reprovados_na_vitrine", stdout=saida)
        return saida.getvalue()

    def test_reabre_verificacao_sem_pedir_link_novo(self):
        saida = self._reabrir()

        afetado = LinkAfiliadoUsuario.objects.get(pk=self.afetado.pk)
        self.assertIsNone(afetado.verificado_ok)
        self.assertEqual(afetado.estado, "pronto")
        self.assertEqual(afetado.verificacao_motivo, "")
        self.assertIsNone(afetado.proxima_tentativa)
        # A URL está boa; só o veredito estava errado.
        self.assertEqual(afetado.link_afiliado, "https://meli.la/vitrine")
        self.assertIn("REABERTAS", saida)

    def test_preserva_reprovacao_que_e_do_proprio_anuncio(self):
        self._reabrir()

        legitimo = LinkAfiliadoUsuario.objects.get(pk=self.legitimo.pk)
        self.assertIs(legitimo.verificado_ok, False)
        self.assertIn("pausado", legitimo.verificacao_motivo)

    def test_dry_run_nao_escreve(self):
        saida = StringIO()
        call_command("reabrir_links_reprovados_na_vitrine", "--dry-run",
                     stdout=saida)

        self.afetado.refresh_from_db()
        self.assertIs(self.afetado.verificado_ok, False)
        self.assertIn("seca", saida.getvalue())


class LinkDeCupomSemProvaAindaTests(TestCase):
    """Sem prova AINDA não é prova contrária.

    O vínculo produto-cupom é reconstruído pelo worker de cupons a cada ciclo.
    Reprovar o link enquanto ele não chega o mandaria para a fila de GERAÇÃO — que
    de propósito não reabre link com veredito — e recriaria o impasse "aguardando
    link" que esta mudança desfaz.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("sem-prova")
        self.produto = _produto("mercadolivre", origem="cupom")
        self.linha = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, afiliado_ok=True,
            link_afiliado="https://meli.la/sem-prova", verificado_ok=None,
            estado="pronto",
        )

    def test_adia_em_vez_de_reprovar_e_nem_abre_o_encurtador(self):
        from apps.scrapers.scraper_mercadolivre.link import (
            verificar_links_pendentes,
        )

        def _nao_deve_abrir(*_a, **_k):
            raise AssertionError("não deve abrir o encurtador sem a prova")

        with patch("apps.scrapers.scraper_mercadolivre.link_http."
                   "relatorio_de_link_com_cupom", _nao_deve_abrir):
            resultado = verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(resultado["transitorios"], 1)
        self.assertEqual(resultado["reprovados"], 0)
        linha = LinkAfiliadoUsuario.objects.get(pk=self.linha.pk)
        self.assertIsNone(linha.verificado_ok)
        self.assertIsNotNone(linha.proxima_tentativa)
        self.assertIn("preparo", linha.verificacao_motivo)

    def test_com_prova_aprova_pelo_destino_do_programa(self):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.scraper_mercadolivre.link import (
            verificar_links_pendentes,
        )

        cupom = CupomNormalizado.objects.create(
            fonte=FonteIngestao.objects.create(
                slug="prova-src", marketplace="mercadolivre", nome="ML"),
            external_id="campanha:9", marketplace="mercadolivre",
            titulo="20% OFF", codigo="", estado="ativo",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20},
        )
        ProdutoCupom.objects.create(
            produto=self.produto, cupom=cupom, status="confirmado",
            preco_original=100, preco_atual=100, preco_final=80,
        )

        with patch("apps.scrapers.scraper_mercadolivre.link_http."
                   "relatorio_de_link_com_cupom",
                   return_value={"ok": True, "erros": []}) as verificador:
            resultado = verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(resultado["aprovados"], 1)
        self.assertTrue(verificador.call_args.kwargs["desconto_comprovado"])
