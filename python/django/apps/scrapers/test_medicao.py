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
