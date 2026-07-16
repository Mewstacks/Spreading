from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.scrapers.models import LinkAfiliadoUsuario, Produto
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
