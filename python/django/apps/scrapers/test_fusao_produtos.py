"""A fusão não pode perder o que custou dinheiro para existir.

Cada teste aqui corresponde a uma forma concreta de a fusão destruir dado:
link de afiliado verificado (custou Playwright), histórico de envio (perder
faz reenviar oferta já enviada), publicação (auditoria imutável) e a
associação produto–cupom, cujo FK é CASCADE e leva os links junto se a ordem
das operações estiver errada.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers import fusao_produtos
from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, HistoricoEnvio,
    LinkAfiliadoProdutoCupomUsuario, LinkAfiliadoUsuario, Produto, ProdutoCupom,
)


class FusaoDeProdutosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("fusao", password="x")
        cls.fonte = FonteIngestao.objects.create(
            slug="fonte-fusao", marketplace="mercadolivre", nome="Fonte", status="ok",
        )

    def _produto(self, link, *, quando=None, **extra):
        produto = Produto.objects.create(
            marketplace="mercadolivre", link_produto=link,
            nome=extra.pop("nome", "Produto"), preco_sem_desconto=100,
            preco_com_cupom=80, **extra,
        )
        if quando:
            Produto.objects.filter(pk=produto.pk).update(ultima_observacao=quando)
            produto.refresh_from_db()
        return produto

    def _cupom(self, codigo="C1"):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"ext-{codigo}", marketplace="mercadolivre",
            titulo=codigo, codigo=codigo, estado="ativo", redemption_mode="code",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 10},
        )

    def test_canonicalizacao_faz_duplicatas_convergirem(self):
        base = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        self._produto(f"{base}?gclid=abc")
        self._produto(base)

        self.assertEqual(fusao_produtos.canonicalizar_links(), 1)
        self.assertEqual(
            set(Produto.objects.values_list("link_produto", flat=True)), {base},
        )
        self.assertEqual(len(fusao_produtos.planejar()), 1)

    def test_vence_a_observacao_mais_recente(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        velho = self._produto(link, quando=agora - timedelta(days=3), nome="Velho")
        novo = self._produto(link, quando=agora, nome="Novo")

        fusao_produtos.executar()

        sobrevivente = Produto.objects.get()
        self.assertEqual(sobrevivente.pk, novo.pk)
        self.assertFalse(Produto.objects.filter(pk=velho.pk).exists())

    def test_link_verificado_nunca_perde_para_vazio(self):
        """O caso caro: unique_together (usuario, produto) força escolher um."""
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        perdedor = self._produto(link, quando=agora - timedelta(days=1))
        vencedor = self._produto(link, quando=agora)

        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=vencedor, verificado_ok=None,
            link_afiliado="",
        )
        bom = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=perdedor, verificado_ok=True,
            verificado_em=agora, link_afiliado="https://afiliado/bom",
        )

        fusao_produtos.executar()

        sobrevivente = LinkAfiliadoUsuario.objects.get()
        self.assertEqual(sobrevivente.pk, bom.pk)
        self.assertEqual(sobrevivente.produto_id, vencedor.pk)
        self.assertTrue(sobrevivente.verificado_ok)

    def test_historico_e_publicacao_sao_preservados(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        perdedor = self._produto(link, quando=agora - timedelta(days=1))
        vencedor = self._produto(link, quando=agora)
        HistoricoEnvio.objects.create(produto=perdedor, usuario=self.user)

        fusao_produtos.executar()

        # Perder histórico faz o sistema reenviar oferta já enviada.
        self.assertEqual(HistoricoEnvio.objects.count(), 1)
        self.assertEqual(HistoricoEnvio.objects.get().produto_id, vencedor.pk)

    def test_associacao_de_cupom_colidida_mantem_o_veredito_mais_forte(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        perdedor = self._produto(link, quando=agora - timedelta(days=1))
        vencedor = self._produto(link, quando=agora)
        cupom = self._cupom()

        ProdutoCupom.objects.create(produto=vencedor, cupom=cupom, status="provavel")
        ProdutoCupom.objects.create(
            produto=perdedor, cupom=cupom, status="confirmado",
            verificado_em=agora, preco_final=50,
        )

        fusao_produtos.executar()

        rel = ProdutoCupom.objects.get()
        self.assertEqual(rel.produto_id, vencedor.pk)
        self.assertEqual(rel.status, "confirmado")
        self.assertEqual(rel.preco_final, 50)

    def test_cascade_da_associacao_nao_come_link_verificado(self):
        """O FK é CASCADE: apagar a associação perdedora antes de mover o link
        destruiria um link de afiliado que custou Playwright para existir."""
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        perdedor = self._produto(link, quando=agora - timedelta(days=1))
        vencedor = self._produto(link, quando=agora)
        cupom = self._cupom()

        ProdutoCupom.objects.create(produto=vencedor, cupom=cupom, status="provavel")
        rel_perdedora = ProdutoCupom.objects.create(
            produto=perdedor, cupom=cupom, status="confirmado")
        LinkAfiliadoProdutoCupomUsuario.objects.create(
            usuario=self.user, relacao=rel_perdedora, verificado_ok=True,
            verificado_em=agora, link_afiliado="https://afiliado/caro",
        )

        fusao_produtos.executar()

        sobrevivente = LinkAfiliadoProdutoCupomUsuario.objects.get()
        self.assertTrue(sobrevivente.verificado_ok)
        self.assertEqual(sobrevivente.link_afiliado, "https://afiliado/caro")
        self.assertEqual(sobrevivente.relacao.produto_id, vencedor.pk)

    def test_vencedor_absorve_campo_que_nao_tinha(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        self._produto(
            link, quando=agora - timedelta(days=1), nome_llm="Nome curto",
            categoria="ELETRONICOS",
        )
        self._produto(link, quando=agora, nome_llm="", categoria="DESCONHECIDO")

        fusao_produtos.executar()

        sobrevivente = Produto.objects.get()
        self.assertEqual(sobrevivente.nome_llm, "Nome curto")
        # DESCONHECIDO é "ninguém classificou", não pode barrar classificação real.
        self.assertEqual(sobrevivente.categoria, "ELETRONICOS")

    def test_e_idempotente(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        self._produto(link, quando=agora - timedelta(days=1))
        self._produto(link, quando=agora)

        primeiro = fusao_produtos.executar()
        segundo = fusao_produtos.executar()

        self.assertEqual(primeiro["produtos_apagados"], 1)
        self.assertEqual(segundo["produtos_apagados"], 0)
        self.assertEqual(Produto.objects.count(), 1)

    def test_dry_run_nao_grava_nada(self):
        agora = timezone.now()
        link = "https://www.mercadolivre.com.br/coisa/p/MLB123"
        self._produto(f"{link}?gclid=x", quando=agora - timedelta(days=1))
        self._produto(link, quando=agora)

        fusao_produtos.canonicalizar_links(dry_run=True)
        resumo = fusao_produtos.executar(dry_run=True)

        self.assertEqual(Produto.objects.count(), 2)
        self.assertEqual(resumo["produtos_apagados"], 0)
        self.assertTrue(
            Produto.objects.filter(link_produto=f"{link}?gclid=x").exists())

    def test_produtos_distintos_nao_sao_fundidos(self):
        agora = timezone.now()
        self._produto("https://www.mercadolivre.com.br/a/p/MLB1", quando=agora)
        self._produto("https://www.mercadolivre.com.br/b/p/MLB2", quando=agora)

        fusao_produtos.executar()

        self.assertEqual(Produto.objects.count(), 2)

    def test_tracking_sem_item_nao_colapsa_produtos_distintos(self):
        """Regressão do achado de produção: 2.522 anúncios numa chave só."""
        agora = timezone.now()
        base = "https://click1.mercadolivre.com.br/mclics/clicks/external/MLB/count"
        self._produto(f"{base}?a=AAA", quando=agora)
        self._produto(f"{base}?a=BBB", quando=agora)

        fusao_produtos.canonicalizar_links()
        fusao_produtos.executar()

        self.assertEqual(Produto.objects.count(), 2)
