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
from apps.scrapers.models import LinkAfiliadoUsuario, Produto


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
