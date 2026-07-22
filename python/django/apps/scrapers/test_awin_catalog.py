from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scrapers.awin import AwinError, listar_contas, sincronizar_integracao
from apps.scrapers.models import (
    ConfiguracaoEnvio, CupomNormalizado, FonteIngestao, IntegracaoAfiliado,
    ProgramaAfiliado,
)


def response(status, payload, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = payload
    return result


@override_settings(SECRETS_FERNET_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                   AWIN_INTEGRATION_ENABLED=True)
class AwinCatalogTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_user("awin-user", password="safe-pass-123")
        self.other = User.objects.create_user("other-user", password="safe-pass-123")
        self.user.perfil.email_verificado = True
        self.user.perfil.save(update_fields=["email_verificado"])
        self.other.perfil.email_verificado = True
        self.other.perfil.save(update_fields=["email_verificado"])
        self.source = FonteIngestao.objects.create(
            slug="awin-offers-api", marketplace="awin", nome="Awin", status="ok")
        self.integration = IntegracaoAfiliado.objects.create(
            owner=self.user, provedor="awin", identificador_conta="123",
            nome_conta="Publisher", token="secret-token-that-is-long-enough",
            status="conectada")
        self.program = ProgramaAfiliado.objects.create(
            integracao=self.integration, external_id="77", nome="Loja Boa",
            dominio="loja.example", dominios_validos=["loja.example"],
            status_vinculo="joined", link_status="online", comissao_max=2)

    @patch("apps.scrapers.awin.requests.request")
    def test_token_lists_publishers_and_is_encrypted_at_rest(self, request):
        request.return_value = response(200, [{"id": 123, "name": "Minha conta"}])
        self.assertEqual(listar_contas(self.integration.token),
                         [{"id": "123", "nome": "Minha conta"}])
        table = IntegracaoAfiliado._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT token FROM "{table}" WHERE id = %s', [self.integration.id])
            stored = cursor.fetchone()[0]
        self.assertNotEqual(stored, "secret-token-that-is-long-enough")

    @patch("apps.scrapers.awin.requests.request")
    def test_syncs_joined_programs_and_private_offers(self, request):
        request.side_effect = [
            response(200, [{"id": 77, "name": "Loja Boa", "displayUrl": "https://loja.example",
                            "validDomains": [{"domain": "loja.example"}],
                            "linkStatus": "online", "deeplinkEnabled": True}]),
            response(200, {"promotions": [{
                "promotionId": 9, "type": "voucher",
                "advertiser": {"id": 77, "name": "Loja Boa", "joined": True},
                "title": "30% OFF somente no app", "description": "Acima de R$ 100",
                "terms": "Somente no app", "startDate": "2026-07-22T00:00:00Z",
                "endDate": "2099-07-22T23:59:00Z",
                "urlTracking": "https://www.awin1.com/cread.php?awinmid=77",
                "voucher": {"code": "APP30", "exclusive": False, "attributable": True},
            }], "pagination": {"page": 1, "totalPages": 1}}),
            response(200, {"commissionRange": [
                {"min": 1, "max": 5, "type": "percentage"},
            ], "programmeInfo": {"deeplinkEnabled": True, "linkStatus": "online",
                                   "validDomains": [{"domain": "loja.example"}]}}),
        ]
        result = sincronizar_integracao(self.integration, forcar_programas=True)
        self.assertEqual(result["coupons"], 1)
        coupon = CupomNormalizado.objects.get(owner=self.user)
        self.assertEqual(coupon.programa.external_id, "77")
        self.assertTrue(coupon.restrito)
        self.assertEqual(coupon.codigo, "APP30")
        self.assertIn("awin1.com", coupon.link)
        self.program.refresh_from_db()
        self.assertEqual(self.program.comissao_max, 5)
        self.assertFalse(CupomNormalizado.objects.filter(owner=self.other).exists())

    @patch("apps.scrapers.awin.requests.request")
    def test_auth_failure_marks_reconnect_without_deleting_catalog(self, request):
        CupomNormalizado.objects.create(
            owner=self.user, integracao=self.integration, programa=self.program,
            fonte=self.source, external_id="old", marketplace="awin",
            titulo="Antigo", link="https://www.awin1.com/old")
        request.return_value = response(401, {})
        with self.assertRaises(AwinError):
            sincronizar_integracao(self.integration, forcar_programas=True)
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.status, "reconectar")
        self.assertTrue(CupomNormalizado.objects.filter(external_id="old").exists())

    def test_private_coupon_visibility_and_idor_protection(self):
        private = CupomNormalizado.objects.create(
            owner=self.user, fonte=self.source, external_id="private", marketplace="awin",
            titulo="Privado", link="https://www.awin1.com/private")
        self.client.force_login(self.other)
        page = self.client.get(reverse("scraper-top") + "?tipo=cupom")
        self.assertNotContains(page, "Privado")
        denied = self.client.post(reverse("scraper-cupom-manual-desativar", args=[private.id]))
        self.assertEqual(denied.status_code, 403)

    def test_manual_coupon_is_private_per_owner_even_with_same_code(self):
        manual, _ = FonteIngestao.objects.get_or_create(
            slug="manual-private",
            defaults={"marketplace": "multiloja", "nome": "Manual", "status": "ok"},
        )
        first = CupomNormalizado.objects.create(
            owner=self.user, fonte=manual, external_id="manual:same",
            marketplace="amazon", titulo="Cupom A", codigo="MESMO",
            link="https://amazon.com.br/a",
        )
        second = CupomNormalizado.objects.create(
            owner=self.other, fonte=manual, external_id="manual:same",
            marketplace="amazon", titulo="Cupom B", codigo="MESMO",
            link="https://amazon.com.br/b",
        )
        self.assertNotEqual(first.owner_id, second.owner_id)

    def test_manual_coupon_rejects_foreign_program_and_unsafe_url(self):
        foreign_integration = IntegracaoAfiliado.objects.create(
            owner=self.other, provedor="awin", identificador_conta="999",
            token="another-secret-token-long-enough", status="conectada",
        )
        foreign_program = ProgramaAfiliado.objects.create(
            integracao=foreign_integration, external_id="foreign", nome="Estrangeiro",
            dominio="foreign.example", dominios_validos=["foreign.example"],
            status_vinculo="joined", link_status="online",
        )
        self.client.force_login(self.user)
        payload = {
            "marketplace": "awin", "programa": foreign_program.id,
            "titulo": "Invasão", "codigo": "NOPE",
            "url": "https://foreign.example/oferta",
        }
        response_foreign = self.client.post(reverse("scraper-cupom-manual"), payload)
        self.assertRedirects(response_foreign, reverse("scraper-top"))
        self.assertFalse(CupomNormalizado.objects.filter(titulo="Invasão").exists())

        payload.update({"marketplace": "amazon", "titulo": "SSRF",
                        "url": "http://127.0.0.1:8000/admin"})
        response_unsafe = self.client.post(reverse("scraper-cupom-manual"), payload)
        self.assertRedirects(response_unsafe, reverse("scraper-top"))
        self.assertFalse(CupomNormalizado.objects.filter(titulo="SSRF").exists())

    def test_secret_is_never_rendered_and_coupon_title_is_escaped(self):
        CupomNormalizado.objects.create(
            owner=self.user, fonte=self.source, external_id="xss", marketplace="awin",
            titulo='<script>alert("x")</script>', link="https://www.awin1.com/xss",
        )
        self.client.force_login(self.user)
        account = self.client.get(reverse("scraper-conta"))
        self.assertNotContains(account, "secret-token-that-is-long-enough")
        page = self.client.get(reverse("scraper-top") + "?tipo=cupom")
        self.assertNotContains(page, '<script>alert("x")</script>')
        self.assertContains(page, "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;")

    def test_mutating_routes_require_login_and_csrf(self):
        anonymous = Client()
        self.assertEqual(anonymous.post(reverse("scraper-awin-desconectar")).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(
            csrf_client.post(reverse("scraper-awin-desconectar")).status_code, 403)
        self.assertEqual(
            csrf_client.post(reverse("scraper-cupom-manual"), {}).status_code, 403)

    def test_ranking_prioritizes_consumer_value_and_includes_restricted(self):
        expensive_commission = ProgramaAfiliado.objects.create(
            integracao=self.integration, external_id="88", nome="Comissão alta",
            dominio="high.example", status_vinculo="joined", link_status="online",
            comissao_max=90)
        weak = CupomNormalizado.objects.create(
            owner=self.user, integracao=self.integration, programa=expensive_commission,
            fonte=self.source, external_id="weak", marketplace="awin", titulo="10% OFF",
            codigo="LOW10", link="https://www.awin1.com/weak",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 10,
                    "modo_resgate": "codigo"})
        strong = CupomNormalizado.objects.create(
            owner=self.user, integracao=self.integration, programa=self.program,
            fonte=self.source, external_id="strong", marketplace="awin", titulo="40% OFF",
            codigo="BEST40", link="https://www.awin1.com/strong", restrito=True,
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 40,
                    "modo_resgate": "codigo", "escopo": "Somente no app"})
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="group", min_desconto_percent=0,
            incluir_restritos=True, incluir_sem_desconto=True)
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo
        candidates = selecionar_conteudo_para_grupo(config, limit=2)
        self.assertEqual(candidates[0].obj, strong)
        self.assertIn(weak, [item.obj for item in candidates])
        from apps.scrapers.ofertas import montar_mensagem_cupom
        self.assertIn("Condição", montar_mensagem_cupom(strong, link_afiliado=strong.link))

    @patch("apps.scrapers.ofertas.enviar_cupom")
    def test_automation_routes_coupon_candidate_to_coupon_sender(self, send_coupon):
        coupon = CupomNormalizado.objects.create(
            owner=self.user, integracao=self.integration, programa=self.program,
            fonte=self.source, external_id="route", marketplace="awin", titulo="50% OFF",
            codigo="ROUTE50", link="https://www.awin1.com/route",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 50,
                    "modo_resgate": "codigo"})
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="group", min_desconto_percent=0)
        send_coupon.return_value = {"sucesso": True}
        from apps.scrapers.ofertas import selecionar_e_enviar
        result = selecionar_e_enviar(None, "group", usuario=self.user,
                                     configuracao=config, verificar=False)
        self.assertTrue(result["sucesso"])
        self.assertEqual(send_coupon.call_args.args[0], coupon)
