from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, LinkAfiliadoUsuario, Produto,
)
from apps.scrapers.precos import registrar


class MedicaoTopTests(TestCase):
    def test_medir(self):
        u = get_user_model().objects.create_user("medidor", password="x")
        u.perfil.marcar_verificado()
        for i in range(20):
            p = Produto.objects.create(
                marketplace="mercadolivre", nome=f"Produto {i}", origem="oferta",
                preco_sem_desconto=200, preco_com_cupom=100,
                link_produto=f"https://example.com/p{i}")
            for _ in range(3):
                registrar("mercadolivre", "", p.link_produto, 150)
            LinkAfiliadoUsuario.objects.create(
                usuario=u, produto=p, link_afiliado=f"https://ml.com/sec/{i}",
                afiliado_ok=True)
        self.client.force_login(u)
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(reverse("scraper-top"))
        sqls = [q["sql"].lower() for q in ctx.captured_queries]
        prontos = sum(1 for p in r.context["produtos"] if p.afiliado_pronto)
        print("\n--- MEDICAO /scrapers/top/ com 20 produtos ---")
        print("TOTAL de queries      :", len(sqls))
        print("  precohistorico      :", sum("precohistorico" in s for s in sqls))
        print("  linkafiliadousuario :", sum("linkafiliadousuario" in s for s in sqls))
        print("badges 'afiliado'     :", prontos, "de", len(r.context["produtos"]))


class TopNaoEscalaComOCatalogoTests(TestCase):
    """O custo de /scrapers/top/ não pode crescer com o número de cupons.

    A tela chamava `relacoes_prontas_para_envio` cupom a cupom (2-3 queries cada) e
    depois REFAZIA o mesmo trabalho num segundo laço. Com os ~2.400 cupons ativos que
    o catálogo tem em produção, isso passava de doze mil queries em um único GET —
    a causa da tela de Promoções travar. Este teste falha se o padrão voltar.
    """

    def setUp(self):
        cache.clear()   # a taxonomia é cacheada; medir com cache frio é o pior caso
        self.user = get_user_model().objects.create_user("medidor-cupons", password="x")
        self.user.perfil.marcar_verificado()
        self.fonte = FonteIngestao.objects.create(
            slug="ml-cupons", nome="ML Cupons", status="ok")

    def _criar_cupons(self, quantidade):
        base = CupomNormalizado.objects.count()
        CupomNormalizado.objects.bulk_create([
            CupomNormalizado(
                fonte=self.fonte, marketplace="mercadolivre", estado="ativo",
                titulo=f"Cupom {i}", codigo=f"CUP{i}", external_id=f"cup-{i}",
            )
            for i in range(base, base + quantidade)
        ])

    def _queries_do_top(self):
        self.client.force_login(self.user)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("scraper-top"))
        return len(ctx.captured_queries)

    def test_custo_e_o_mesmo_com_10_e_com_200_cupons(self):
        self._criar_cupons(10)
        com_10 = self._queries_do_top()
        cache.clear()
        self._criar_cupons(200)
        com_200 = self._queries_do_top()
        # Margem para variação de contexto/sessão; o que importa é NÃO ser linear —
        # no padrão antigo a diferença seria de centenas de queries.
        self.assertLessEqual(
            com_200, com_10 + 5,
            f"20x mais cupons custou {com_200 - com_10} queries a mais: o N+1 voltou.",
        )


class TopClassificaAJanelaInteiraTests(TestCase):
    """A varredura de prontidão não pode parar ao encher a página pedida.

    `pendentes_ocultos`, o funil por loja, a promoção da loja menor e o total de
    páginas descrevem a JANELA INTEIRA, não os 20 cards. Enquanto a varredura
    parava cedo, um catálogo com muita oferta de uma loja e pouca de outra media,
    em 25/08/2026: zero Amazon na primeira página, Amazon fora do funil, o aviso
    de tag Amazon ausente sumido, 5 páginas oferecidas de 27 e `pendentes_ocultos`
    em 0 de 60. Ou seja: a loja menor desaparecia atrás do volume da maior — que é
    exatamente o oposto do que a tela existe para fazer.
    """

    def _produto(self, usuario, marketplace, i, *, pronto, preco):
        produto = Produto.objects.create(
            marketplace=marketplace, nome=f"{marketplace} {i}", origem="oferta",
            preco_sem_desconto=200, preco_com_cupom=preco,
            link_produto=f"https://example.com/{marketplace}/{i}")
        registrar(marketplace, "", produto.link_produto, preco)
        if pronto:
            LinkAfiliadoUsuario.objects.create(
                usuario=usuario, produto=produto,
                link_afiliado=f"https://loja.com/sec/{marketplace}{i}",
                afiliado_ok=True, verificado_ok=True)
        return produto

    def test_loja_menor_nao_some_atras_do_volume_da_maior(self):
        cache.clear()
        user = get_user_model().objects.create_user("janela", password="x")
        user.perfil.marcar_verificado()
        # A ordenação é por desconto, então a loja grande com desconto alto ocupa
        # todo o começo do ranking e a loja pequena cai depois da posição 100 — a
        # fronteira do primeiro lote. Era exatamente aí que a varredura parava.
        for i in range(120):
            self._produto(user, "mercadolivre", i, pronto=True, preco=100)
        for i in range(5):
            self._produto(user, "amazon", i, pronto=True, preco=190)
        for i in range(7):
            self._produto(user, "amazon", 100 + i, pronto=False, preco=190)

        self.client.force_login(user)
        resposta = self.client.get(reverse("scraper-top"))
        self.assertEqual(resposta.status_code, 200)

        lojas = {linha["slug"]: linha for linha in resposta.context["prontos_por_loja"]}
        self.assertIn("amazon", lojas, "A Amazon sumiu do funil por loja.")
        self.assertEqual(lojas["amazon"]["prontos"], 5)
        self.assertEqual(lojas["amazon"]["pendentes"], 7)
        # A contagem de pendentes descreve a janela, não o pedaço varrido.
        self.assertEqual(resposta.context["pendentes_ocultos"], 7)
        # E a loja menor aparece na PRIMEIRA página, que é o que
        # equilibrar_primeira_pagina existe para garantir.
        self.assertTrue(
            any(p.marketplace == "amazon" for p in resposta.context["produtos"]),
            "Nenhum item da loja menor na primeira página.",
        )
