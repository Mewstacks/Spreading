from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase

from apps.scrapers.sources.ml_lightning_coupons import extract_lightning_coupons


def _payload(*coupons):
    return {
        "brickStack": {
            "lightning-coupons-123": {"data": {"groupings": [
                {"coupons": list(coupons)},
            ]}},
        },
    }


def _carousel(*coupons):
    return {
        "brickStack": {
            "coupons-carousel-123": {
                "ui_type": "coupons-carousel",
                "data": {"coupons": list(coupons)},
            },
        },
    }


def _coupon(**overrides):
    row = {
        "campaign_id": "9988",
        "title": {"text": "20% OFF"},
        "category": "Em TECH",
        "amount": {
            "min_amount": "Compra minima R$ 1.099",
            "cap_amount": "Limite de desconto R$ 200",
        },
        "benefit_mode": "PERCENT",
        "status": {"id": "SCHEDULED"},
        "conditions": {"text": "Condicoes"},
        "code": "TECH20",
        "coupon_redeem_type": "CODE",
        "start_time": "15:00h",
        "start_date": "2026-08-28T18:00:00Z",
        "expiration_date": "2026-08-28T19:00:00Z",
        "segmentations": {"total_items": 321},
    }
    row.update(overrides)
    return row


class MLLightningCouponsTests(SimpleTestCase):
    now = datetime(2026, 8, 28, 17, 30, tzinfo=dt_timezone.utc)

    def test_parseia_agendado_com_janela_e_regras_reais(self):
        rows, metrics, health = extract_lightning_coupons(
            _payload(_coupon()), now=self.now,
        )

        self.assertEqual(health, "healthy")
        self.assertEqual(metrics["accepted"], 1)
        self.assertFalse(metrics["complete"])
        self.assertEqual(rows[0].coupon_code, "TECH20")
        self.assertTrue(rows[0].flash)
        self.assertEqual(rows[0].coupon_rules["valor_minimo"], 1099)
        self.assertEqual(rows[0].coupon_rules["desconto_maximo"], 200)
        self.assertEqual(rows[0].starts_at.isoformat(), "2026-08-28T18:00:00+00:00")

    def test_finalizado_nunca_e_republicado(self):
        rows, metrics, health = extract_lightning_coupons(
            _payload(_coupon(status={"id": "FINISHED"})), now=self.now,
        )

        self.assertEqual(rows, [])
        self.assertEqual(metrics["rejected_by_reason"], {"finished": 1})
        self.assertEqual(health, "healthy_empty")

    def test_agenda_antiga_e_degradada_em_vez_de_vazio_saudavel(self):
        old = _coupon(
            status={"id": "FINISHED"},
            start_date="2026-06-08T18:00:00Z",
            expiration_date="2026-06-08T19:00:00Z",
        )
        rows, metrics, health = extract_lightning_coupons(
            _payload(old), now=self.now,
        )

        self.assertEqual(rows, [])
        self.assertTrue(metrics["stale_inventory"])
        self.assertEqual(health, "degraded")

    def test_schema_ausente_e_degradado(self):
        rows, metrics, health = extract_lightning_coupons({}, now=self.now)

        self.assertEqual(rows, [])
        self.assertFalse(metrics["contract_found"])
        self.assertEqual(health, "degraded")

    def test_carrossel_novo_com_tokens_opacos_e_vazio_saudavel(self):
        token = _coupon(
            coupon_redeem_type=None, start_date=None,
            code="opq4aijZ53ykxseCjd2_l_GNPhXIm4bTm0vAzGmeySQ==",
        )

        rows, metrics, health = extract_lightning_coupons(
            _carousel(token), now=self.now,
        )

        self.assertEqual(rows, [])
        self.assertTrue(metrics["contract_found"])
        self.assertEqual(metrics["rejected_by_reason"], {"invalid_code": 1})
        self.assertEqual(health, "healthy_empty")

    def test_carrossel_novo_aceita_codigo_literal_sem_inventar_inicio(self):
        literal = _coupon(
            coupon_redeem_type=None, start_date=None, code="FLASH25",
            expiration_date="2026-08-28T20:00:00Z",
        )

        rows, metrics, health = extract_lightning_coupons(
            _carousel(literal), now=self.now,
        )

        self.assertEqual(health, "healthy")
        self.assertEqual(metrics["accepted"], 1)
        self.assertIsNone(rows[0].starts_at)
        self.assertTrue(rows[0].flash)
        self.assertTrue(rows[0].evidence["start_date_missing"])
