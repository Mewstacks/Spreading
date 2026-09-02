import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.accounts.management.commands.marketplace_readiness_probe import (
    _amazon_config_readiness,
    _browser_readiness,
    _ml_readiness,
    _shopee_affiliate_readiness,
)


class MarketplaceReadinessRulesTests(SimpleTestCase):
    def test_browser_session_requires_decryptable_auth_evidence(self):
        ready = SimpleNamespace(
            status="active", probe_failures=0,
            encrypted_state=json.dumps({"cookies": [{"name": "sid"}]}),
        )
        self.assertEqual(_browser_readiness(ready), (True, "pronta"))
        self.assertEqual(_browser_readiness(None), (False, "sessao_ausente"))
        empty = SimpleNamespace(
            status="active", probe_failures=0,
            encrypted_state=json.dumps({"cookies": [], "origins": []}),
        )
        self.assertEqual(
            _browser_readiness(empty),
            (False, "storage_state_sem_credencial"),
        )

    def test_browser_session_only_drops_after_conclusive_limit(self):
        state = json.dumps({"cookies": [{"name": "sid"}]})
        suspect = SimpleNamespace(
            status="suspect", probe_failures=2, encrypted_state=state,
        )
        dropped = SimpleNamespace(
            status="suspect", probe_failures=3, encrypted_state=state,
        )
        self.assertEqual(
            _browser_readiness(suspect),
            (True, "utilizavel_com_suspeitas=2"),
        )
        self.assertEqual(
            _browser_readiness(dropped),
            (False, "suspeitas_conclusivas=3"),
        )

    def test_ml_requires_site_and_linkbuilder_proven_ready(self):
        state = {"cookies": [{"name": "ssid"}]}
        record = SimpleNamespace(
            status="active", probe_failures=0,
            last_probe_result="conectado", lb_readiness="ready",
        )
        self.assertEqual(
            _ml_readiness(record, state), (True, "pronta_com_linkbuilder")
        )
        record.lb_readiness = "login_required"
        self.assertEqual(
            _ml_readiness(record, state),
            (False, "linkbuilder=login_required"),
        )

    def test_ml_live_connected_substitui_probe_http_inconclusivo(self):
        state = {"cookies": [{"name": "ssid"}]}
        record = SimpleNamespace(
            status="active", probe_failures=0,
            last_probe_result="inconclusivo", lb_readiness="ready",
        )

        self.assertEqual(
            _ml_readiness(record, state),
            (False, "probe=inconclusivo"),
        )
        self.assertEqual(
            _ml_readiness(record, state, live_verdict="conectado"),
            (True, "pronta_com_linkbuilder"),
        )

    def test_shopee_requires_both_shopper_and_affiliate_credentials(self):
        integration = SimpleNamespace(
            habilitada=True, status="conectada",
            identificador_conta="123", token="secret",
        )
        self.assertEqual(
            _shopee_affiliate_readiness(integration),
            (True, "integracao_afiliada_pronta"),
        )
        integration.token = ""
        self.assertEqual(
            _shopee_affiliate_readiness(integration),
            (False, "secret_ausente"),
        )

    def test_amazon_creators_is_optional_but_explicitly_gateable(self):
        profile = SimpleNamespace(
            afiliado_tag_amazon="tag-20",
            amazon_credential_id="id",
            amazon_credential_secret="secret",
            amazon_elegivel=False,
        )
        self.assertEqual(
            _amazon_config_readiness(profile), (True, "partner_tag_pronta")
        )
        self.assertEqual(
            _amazon_config_readiness(profile, require_creators=True),
            (False, "creators_conta_inelegivel"),
        )
