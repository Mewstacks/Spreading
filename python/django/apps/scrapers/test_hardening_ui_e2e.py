"""E2E real dos estados operacionais adicionados ao painel de cupons."""

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from playwright.sync_api import sync_playwright

from apps.accounts.models import ensure_personal_organization
from apps.scrapers.models import (
    CupomDisponibilidade,
    CupomNormalizado,
    FonteIngestao,
)


class CouponOperationalDashboardE2ETests(StaticLiveServerTestCase):
    def setUp(self):
        # is_staff porque é o painel OPERACIONAL de cupons que está sob teste
        # (contadores por etapa e "Motivos de indisponibilidade"). Ele deixou de ser
        # exibido para o usuário comum — ver `diagnostico` em `top_promocoes`.
        self.user = get_user_model().objects.create_user(
            "coupon-e2e", password="e2e-password", is_staff=True,
        )
        self.user.perfil.marcar_verificado()
        organization = ensure_personal_organization(self.user)
        source = FonteIngestao.objects.create(
            slug="coupon-e2e-source", marketplace="mercadolivre",
            nome="Cupons E2E", status="ok", ultimo_total=1,
        )
        coupon = CupomNormalizado.objects.create(
            fonte=source, external_id="e2e-code", marketplace="mercadolivre",
            titulo="Cupom observável", codigo="E2E20",
            link="https://lista.mercadolivre.com.br/e2e",
            regras={
                "modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                "valor_desconto": 20,
            },
        )
        CupomDisponibilidade.objects.create(
            organization=organization, usuario=self.user, cupom=coupon,
            use_mode="code_notice", stage="waiting_link", category="no_link",
            reason_code="affiliate_link_pending",
            safe_detail="Código validado; link afiliado em preparação.",
        )

    def _login(self, page):
        page.goto(f"{self.live_server_url}/accounts/login/")
        page.locator("input[name=username]").fill("coupon-e2e")
        page.locator("input[name=password]").fill("e2e-password")
        page.get_by_role("button", name="Entrar").click()
        page.wait_for_url(f"{self.live_server_url}/")

    def test_estados_e_motivos_renderizam_sem_overflow_mobile_e_desktop(self):
        # Playwright precisa nascer depois do setup ORM e encerrar antes do flush:
        # seu loop interno faz o Django detectar contexto async na mesma thread.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for viewport in (
                    {"width": 390, "height": 844},
                    {"width": 1440, "height": 900},
                ):
                    context = browser.new_context(viewport=viewport)
                    page = context.new_page()
                    try:
                        self._login(page)
                        page.goto(f"{self.live_server_url}/scrapers/top/?tipo=cupom")
                        page.get_by_text("1 coletado(s)", exact=True).wait_for()
                        page.get_by_text("1 aguardando link", exact=True).wait_for()
                        self.assertTrue(page.get_by_text("E2E20", exact=True).is_visible())

                        page.get_by_text(
                            "Motivos de indisponibilidade", exact=False,
                        ).click()
                        self.assertTrue(
                            page.get_by_text(
                                "Código validado; link afiliado em preparação.",
                                exact=True,
                            ).is_visible()
                        )
                        dimensions = page.evaluate(
                            """() => ({
                              viewport: window.innerWidth,
                              document: document.documentElement.scrollWidth,
                            })"""
                        )
                        self.assertLessEqual(
                            dimensions["document"], dimensions["viewport"],
                            f"overflow horizontal no viewport {viewport}",
                        )
                    finally:
                        context.close()
            finally:
                browser.close()
