from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers.coupon_validation import agendar_validacao
from apps.scrapers.coupon_validation_runner import (
    ValidationObservation, claim_pending_validations,
    defer_missing_checkout_sessions, run_validation_batch,
)
from apps.scrapers.models import CupomNormalizado, FonteIngestao


class CouponValidationRunnerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("validation-runner")
        source = FonteIngestao.objects.create(
            slug="runner-community", marketplace="multiloja", nome="Runner",
        )
        self.coupon = CupomNormalizado.objects.create(
            fonte=source, external_id="runner:amazon:TESTE20",
            marketplace="amazon", titulo="Cupom TESTE20", codigo="TESTE20",
            redemption_mode="code", regras={"modo_resgate": "codigo"},
        )

    def _scheduled(self):
        return agendar_validacao(
            self.coupon, self.user, product_key="B012345678",
            product_url="https://www.amazon.com.br/dp/B012345678",
            cart_context={"quantity": 1, "product_id": 42},
        )[0]

    def test_batch_accepts_only_through_the_central_monetary_gate(self):
        validation = self._scheduled()

        metrics = run_validation_batch(adapters={
            "amazon": lambda row: ValidationObservation(
                status="accepted", subtotal_before="100", subtotal_after="80",
                evidence={"coupon_badge": row.cupom.codigo},
            ),
        })

        validation.refresh_from_db()
        self.assertEqual(metrics["accepted"], 1)
        self.assertEqual(validation.status, "accepted")
        self.assertEqual(validation.evidence["cart_context"]["product_id"], 42)
        self.assertEqual(validation.evidence["coupon_badge"], "TESTE20")
        self.assertTrue(validation.no_purchase)

    def test_adapter_exception_is_sanitized_and_retried(self):
        validation = self._scheduled()

        def broken(_row):
            raise RuntimeError("cookie=segredo-absoluto")

        metrics = run_validation_batch(adapters={"amazon": broken})

        validation.refresh_from_db()
        self.assertEqual(metrics["adapter_errors"], 1)
        self.assertEqual(validation.status, "inconclusive")
        self.assertEqual(validation.reason_code, "adapter_error")
        self.assertNotIn("segredo", validation.safe_detail)
        self.assertIsNotNone(validation.retry_at)

    def test_malformed_adapter_result_does_not_leave_the_row_running(self):
        validation = self._scheduled()

        metrics = run_validation_batch(adapters={
            "amazon": lambda _row: ValidationObservation(status="invented"),
        })

        validation.refresh_from_db()
        self.assertEqual(metrics["adapter_errors"], 1)
        self.assertEqual(validation.status, "inconclusive")
        self.assertEqual(validation.reason_code, "adapter_error")

    def test_future_retry_is_not_claimed_and_unsupported_store_is_untouched(self):
        validation = self._scheduled()
        validation.retry_at = timezone.now() + timezone.timedelta(hours=1)
        validation.save(update_fields=("retry_at",))

        self.assertEqual(claim_pending_validations(
            marketplaces={"amazon"}, limit=3,
        ), [])
        self.assertEqual(run_validation_batch(adapters={"mercadolivre": lambda row: None})[
            "claimed"
        ], 0)
        validation.refresh_from_db()
        self.assertEqual(validation.status, "pending")

    def test_stale_running_attempt_is_recovered(self):
        validation = self._scheduled()
        validation.status = "running"
        validation.started_at = timezone.now() - timezone.timedelta(minutes=16)
        validation.save(update_fields=("status", "started_at"))

        claimed = claim_pending_validations(marketplaces={"amazon"}, limit=1)

        self.assertEqual([row.pk for row in claimed], [validation.pk])
        validation.refresh_from_db()
        self.assertEqual(validation.status, "running")
        self.assertGreater(validation.started_at, timezone.now() - timezone.timedelta(minutes=1))

    def test_missing_session_is_deferred_in_bulk_before_browser_lane(self):
        from unittest.mock import patch
        first = self._scheduled()
        second, _ = agendar_validacao(
            self.coupon, self.user, product_key="B000000002",
            product_url="https://www.amazon.com.br/dp/B000000002",
        )

        with patch(
            "apps.scrapers.coupon_validation_runner._checkout_session_available",
            return_value=False,
        ):
            result = defer_missing_checkout_sessions()

        self.assertEqual(result, {"amazon": 2})
        for validation in (first, second):
            validation.refresh_from_db()
            self.assertEqual(validation.status, "inconclusive")
            self.assertEqual(validation.reason_code, "session_required")
            self.assertIsNotNone(validation.retry_at)
            self.assertTrue(validation.no_purchase)
