from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import organization_for_user
from apps.scrapers.coupon_readiness import _preflight
from apps.scrapers.coupon_validation import (
    agendar_lote_validacao, agendar_validacao, registrar_resultado,
    validacoes_recentes_por_codigo,
)
from apps.scrapers.models import (
    CupomDisponibilidade, CupomNormalizado, CupomValidacao, FonteIngestao, Produto,
)


class CouponValidationLedgerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("checkout-validator")
        self.source = FonteIngestao.objects.create(
            slug="meliuz-cupons", marketplace="multiloja", nome="Meliuz",
        )
        self.coupon = CupomNormalizado.objects.create(
            fonte=self.source, external_id="meliuz:amazon:TESTE20",
            marketplace="amazon", titulo="Cupom TESTE20", codigo="TESTE20",
            redemption_mode="code", regras={
                "tipo_desconto": "fixo", "valor_desconto": 20,
                "modo_resgate": "codigo", "escopo": "produtos selecionados",
            }, evidencia={"confianca_origem": "comunidade"},
        )

    def _scheduled(self):
        validation, _ = agendar_validacao(
            self.coupon, self.user, product_key="B012345678",
            product_url="https://www.amazon.com.br/dp/B012345678",
            cart_context={"quantity": 1},
        )
        return validation

    def test_accepts_only_an_observed_monetary_reduction(self):
        result = registrar_resultado(
            self._scheduled(), status="accepted", subtotal_before="129.90",
            subtotal_after="109.90", evidence={"coupon_badge": "TESTE20"},
        )

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.discount_amount, Decimal("20.00"))
        self.assertTrue(result.no_purchase)
        self.assertEqual(result.organization, organization_for_user(self.user))

    def test_text_without_subtotal_reduction_is_inconclusive(self):
        result = registrar_resultado(
            self._scheduled(), status="accepted", subtotal_before="100.00",
            subtotal_after="100.00", evidence={"coupon_badge": "aplicado"},
        )

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "discount_not_observed")
        self.assertIsNone(result.discount_amount)

    def test_purchase_signal_discards_the_result(self):
        result = registrar_resultado(
            self._scheduled(), status="accepted", subtotal_before=100,
            subtotal_after=80, evidence={"order_created": True},
        )

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "purchase_boundary_breached")
        self.assertTrue(result.no_purchase)

    def test_accepted_checkout_releases_a_community_claim(self):
        validation = registrar_resultado(
            self._scheduled(), status="accepted", subtotal_before=100,
            subtotal_after=80,
        )
        validations = validacoes_recentes_por_codigo(self.user, [self.coupon])

        self.assertEqual(validation.status, "accepted")
        self.assertIsNone(_preflight(
            self.coupon, self.user, corroboracoes=set(),
            validacoes_checkout=validations,
        ))

    def test_terminal_checkout_rejection_discards_for_this_user(self):
        registrar_resultado(
            self._scheduled(), status="rejected", reason_code="expired",
        )
        validations = validacoes_recentes_por_codigo(self.user, [self.coupon])

        result = _preflight(
            self.coupon, self.user, corroboracoes=set(),
            validacoes_checkout=validations,
        )
        self.assertEqual(result["stage"], "discarded")
        self.assertEqual(result["reason_code"], "checkout_expired")

    def test_nonterminal_rejection_does_not_kill_the_coupon(self):
        registrar_resultado(
            self._scheduled(), status="rejected", reason_code="minimum_not_met",
        )
        validations = validacoes_recentes_por_codigo(self.user, [self.coupon])

        result = _preflight(
            self.coupon, self.user, corroboracoes=set(),
            validacoes_checkout=validations,
        )
        self.assertEqual(result["reason_code"], "community_uncorroborated")

    def test_scheduler_chooses_a_real_product_above_the_minimum(self):
        self.coupon.regras = {**self.coupon.regras, "valor_minimo": 150}
        self.coupon.save(update_fields=("regras",))
        organization = organization_for_user(self.user)
        CupomDisponibilidade.objects.create(
            organization=organization, usuario=self.user, cupom=self.coupon,
            channel="whatsapp", use_mode="code_notice", stage="collected",
            category="waiting", reason_code="community_uncorroborated",
        )
        Produto.objects.create(
            marketplace="amazon", nome="Barato", preco_sem_desconto=100,
            preco_com_cupom=100, preco_efetivo=100,
            link_produto="https://www.amazon.com.br/dp/B000000001",
            asin="B000000001",
        )
        expensive = Produto.objects.create(
            marketplace="amazon", nome="Elegível", preco_sem_desconto=200,
            preco_com_cupom=180, preco_efetivo=180,
            link_produto="https://www.amazon.com.br/dp/B000000002",
            asin="B000000002",
        )

        first = agendar_lote_validacao(self.user, limite=5)
        second = agendar_lote_validacao(self.user, limite=5)

        self.assertEqual(first["scheduled"], 1)
        self.assertEqual(CupomValidacao.objects.count(), 1)
        self.assertEqual(CupomValidacao.objects.get().product_key, expensive.asin)
        self.assertEqual(second["reused"], 1)

    def test_scheduler_respects_category_hint_in_the_code(self):
        self.coupon.codigo = "LIVROS15"
        self.coupon.regras = {**self.coupon.regras, "valor_minimo": 50}
        self.coupon.save(update_fields=("codigo", "regras"))
        CupomDisponibilidade.objects.create(
            organization=organization_for_user(self.user), usuario=self.user,
            cupom=self.coupon, channel="whatsapp", use_mode="code_notice",
            stage="collected", category="waiting",
            reason_code="community_uncorroborated",
        )
        Produto.objects.create(
            marketplace="amazon", nome="Kit Starlink", preco_sem_desconto=500,
            preco_com_cupom=450, preco_efetivo=450,
            link_produto="https://www.amazon.com.br/dp/B000000003",
            asin="B000000003",
        )
        book = Produto.objects.create(
            marketplace="amazon", nome="Livro Engenharia de Software",
            preco_sem_desconto=120, preco_com_cupom=90, preco_efetivo=90,
            link_produto="https://www.amazon.com.br/dp/B000000004",
            asin="B000000004", macro_categoria="Livros",
        )

        result = agendar_lote_validacao(self.user, limite=5)

        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(CupomValidacao.objects.get().product_key, book.asin)
