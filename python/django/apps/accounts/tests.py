import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
from datetime import timedelta
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.contrib.auth import get_user_model
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.test import (
    SimpleTestCase, TestCase, TransactionTestCase, override_settings,
)
from django.urls import reverse
from django.utils import timezone

from apps.accounts.ml_session_crypto import MLSessionCryptoError
from apps.accounts.ml_sessions import (
    has_storage_state, load_storage_state, save_storage_state,
)
from apps.accounts.models import (
    Membership,
    MercadoLivreSession,
    WhatsAppConnection,
)
from apps.accounts.rls import STRICT_TENANT_TABLES, policy_statements
from apps.accounts.tenant import (
    _context_signature, current_organization_id, executar_no_tenant,
    organization_context,
)
from apps.accounts.wa_capabilities import issue_capability, public_key_base64url
from apps.scrapers.models import Produto


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


ML_KEYS = json.dumps({
    "v1": _b64(bytes(range(32))),
    "v2": _b64(bytes(reversed(range(32)))),
})
WA_PRIVATE = _b64(bytes(range(32)))
TENANT_CONTEXT_KEY = _b64(bytes(range(48)))


class OrganizationBoundaryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("tenant-alice", password="test")
        self.bob = User.objects.create_user("tenant-bob", password="test")

    def test_user_creation_provisions_boundary_and_connection(self):
        organization = self.alice.personal_organization
        self.assertEqual(self.alice.perfil.organization, organization)
        membership = Membership.objects.get(
            organization=organization, user=self.alice,
        )
        connection = WhatsAppConnection.objects.get(organization=organization)

        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_active)
        self.assertEqual(connection.instance_id, str(self.alice.pk))
        self.assertNotEqual(
            organization.pk, self.bob.personal_organization.pk,
        )

    def test_private_model_derives_organization_from_owner(self):
        product = Produto.objects.create(
            owner=self.alice,
            nome="Privado",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://example.com/item",
        )
        self.assertEqual(product.organization, self.alice.personal_organization)
        self.assertEqual(product.data_scope, "organization")

    def test_cross_tenant_write_is_rejected_before_database(self):
        with organization_context(self.alice.personal_organization):
            with self.assertRaises(PermissionDenied):
                Produto.objects.create(
                    owner=self.bob,
                    nome="Tentativa",
                    preco_sem_desconto=100,
                    preco_com_cupom=80,
                    link_produto="https://example.com/item",
                )

    def test_owner_and_explicit_organization_must_match(self):
        with self.assertRaises(ValidationError):
            Produto.objects.create(
                owner=self.alice,
                organization=self.bob.personal_organization,
                nome="Tentativa",
                preco_sem_desconto=100,
                preco_com_cupom=80,
                link_produto="https://example.com/item",
            )


@override_settings(
    ML_SESSION_KEKS_JSON=ML_KEYS,
    ML_SESSION_CURRENT_KEY_VERSION="v1",
    ML_LEGACY_SESSION_READ_ENABLED=False,
)
class MercadoLivreEncryptionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "ml-secure", password="test",
        )
        self.state = {
            "cookies": [{"name": "ssid", "value": "segredo-cookie"}],
            "origins": [{"origin": "https://mercadolivre.com.br"}],
        }

    def test_roundtrip_does_not_store_plaintext(self):
        record = save_storage_state(self.user, self.state)
        self.assertEqual(load_storage_state(self.user), self.state)
        self.assertNotIn(b"segredo-cookie", bytes(record.ciphertext))
        self.assertNotIn(b"ssid", bytes(record.ciphertext))

    def test_ciphertext_tampering_is_detected_and_quarantined(self):
        record = save_storage_state(self.user, self.state)
        tampered = bytearray(record.ciphertext)
        tampered[-1] ^= 1
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            ciphertext=bytes(tampered),
        )

        with self.assertRaises(MLSessionCryptoError):
            load_storage_state(self.user)
        record.refresh_from_db()
        self.assertEqual(record.status, "decrypt_error")

    def test_aad_prevents_moving_session_to_another_tenant(self):
        other = get_user_model().objects.create_user("ml-other", password="test")
        record = save_storage_state(self.user, self.state)
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            organization=other.personal_organization,
        )

        with self.assertRaises(MLSessionCryptoError):
            load_storage_state(other)

    def test_key_rotation_reencrypts_with_current_version(self):
        record = save_storage_state(self.user, self.state)
        self.assertEqual(record.key_version, "v1")
        with override_settings(ML_SESSION_CURRENT_KEY_VERSION="v2"):
            record = save_storage_state(self.user, self.state)
            self.assertEqual(record.key_version, "v2")
            self.assertEqual(load_storage_state(self.user), self.state)

    def test_exact_legacy_file_migrates_then_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, f"auth_{self.user.pk}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle)
            with override_settings(
                ML_AUTH_DIR=directory,
                ML_LEGACY_SESSION_READ_ENABLED=True,
            ):
                self.assertEqual(load_storage_state(self.user), self.state)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(MercadoLivreSession.objects.filter(
                organization=self.user.personal_organization,
            ).exists())


@override_settings(
    ML_SESSION_KEKS_JSON=ML_KEYS,
    ML_SESSION_CURRENT_KEY_VERSION="v1",
    ML_LEGACY_SESSION_READ_ENABLED=False,
)
class VereditoDaSondaTests(TestCase):
    """A sonda nunca apaga a credencial — e uma suspeita isolada não desconecta.

    Antes, `conexoes.estado_ml` chamava `delete_storage_state` no primeiro veredito
    "expirado". Só que quem produzia esse veredito era um GET a partir do IP de
    datacenter da Fly, onde o gateway anti-bot do ML responde 302→login/403 a
    requisições autenticadas. O usuário conectava, via a tela verde, e um worker o
    desconectava minutos depois — apagando a sessão da organização inteira.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("ml-probe", password="test")
        self.org = self.user.personal_organization
        self.state = {"cookies": [{"name": "ssid", "value": "x"}], "origins": []}
        save_storage_state(self.user, self.state)

    def _suspeitar(self, vezes, *, espacadas=True):
        from apps.accounts.ml_sessions import PROBE_JANELA_S, registrar_veredito

        for _ in range(vezes):
            registrar_veredito(self.org, "suspeito", "o ML redirecionou para o login")
            if espacadas:
                MercadoLivreSession.objects.filter(organization=self.org).update(
                    last_probe_at=timezone.now() - timedelta(seconds=PROBE_JANELA_S + 1),
                )
        return MercadoLivreSession.objects.get(organization=self.org)

    def test_uma_suspeita_nao_apaga_nem_desconecta(self):
        record = self._suspeitar(1)
        self.assertEqual(record.status, "suspect")
        self.assertEqual(record.probe_failures, 1)
        self.assertEqual(load_storage_state(self.user), self.state)

    def test_suspeitas_repetidas_pedem_reconexao_sem_apagar(self):
        from apps.accounts.ml_sessions import PROBE_FALHAS_PARA_DESCONECTAR

        record = self._suspeitar(PROBE_FALHAS_PARA_DESCONECTAR)
        self.assertEqual(record.status, "expired")
        self.assertFalse(has_storage_state(self.user))
        # Os bytes continuam lá: a sonda segue rodando e um "conectado" reabilita.
        self.assertEqual(load_storage_state(self.user), self.state)

    def test_rajada_simultanea_conta_como_uma_suspeita(self):
        """Nove processos sondam em paralelo (ver python/Procfile). Sem a janela,
        uma única rajada valeria pelos três ciclos independentes que a política
        exige e desconectaria o usuário em segundos."""
        record = self._suspeitar(5, espacadas=False)
        self.assertEqual(record.probe_failures, 1)
        self.assertEqual(record.status, "suspect")

    def test_conectado_reabilita_a_sessao_sozinho(self):
        from apps.accounts.ml_sessions import (
            PROBE_FALHAS_PARA_DESCONECTAR, registrar_veredito,
        )

        self._suspeitar(PROBE_FALHAS_PARA_DESCONECTAR)
        registrar_veredito(self.org, "conectado")
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.status, "active")
        self.assertEqual(record.probe_failures, 0)
        self.assertTrue(has_storage_state(self.user))

    def test_inconclusivo_nao_conta_falha(self):
        from apps.accounts.ml_sessions import registrar_veredito

        registrar_veredito(self.org, "inconclusivo", "timeout")
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.probe_failures, 0)
        self.assertEqual(record.status, "active")

    def test_reconectar_zera_o_historico_da_sonda(self):
        self._suspeitar(2)
        save_storage_state(self.user, self.state)
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.probe_failures, 0)
        self.assertEqual(record.last_probe_result, "")
        self.assertEqual(record.status, "active")

    def test_leitura_de_sonda_nao_marca_uso(self):
        MercadoLivreSession.objects.filter(organization=self.org).update(
            last_used_at=None,
        )
        load_storage_state(self.user, touch=False)
        self.assertIsNone(
            MercadoLivreSession.objects.get(organization=self.org).last_used_at,
        )


@override_settings(
    WA_CAPABILITY_PRIVATE_KEY=WA_PRIVATE,
    WA_CAPABILITY_KEY_ID="test-ed25519",
    WA_CAPABILITY_ISSUER="spreading-web",
    WA_CAPABILITY_AUDIENCE="spreading-wa",
    WA_CAPABILITY_TTL_SECONDS=30,
)
class WhatsAppCapabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "wa-secure", password="test",
        )
        self.connection = self.user.personal_organization.whatsapp_connection

    def test_capability_is_tenant_session_action_and_time_bound(self):
        token = issue_capability(
            self.connection.instance_id, ["send"], single_use=True,
        )
        public_raw = base64.urlsafe_b64decode(
            public_key_base64url() + "==",
        )
        payload = jwt.decode(
            token,
            Ed25519PublicKey.from_public_bytes(public_raw),
            algorithms=["EdDSA"],
            issuer="spreading-web",
            audience="spreading-wa",
        )
        self.assertEqual(payload["sub"], str(self.user.personal_organization.pk))
        self.assertEqual(payload["organization_id"], payload["sub"])
        self.assertEqual(payload["session_id"], self.connection.instance_id)
        self.assertEqual(payload["actions"], ["send"])
        self.assertTrue(payload["single_use"])
        self.assertLessEqual(payload["exp"] - payload["iat"], 30)

    def test_unknown_session_is_denied(self):
        with self.assertRaises(PermissionDenied):
            issue_capability("tenant-inexistente", ["send"])


class FailClosedConfigurationTests(TestCase):
    @override_settings(
        SECURITY_FREEZE_NEW_TENANTS=True,
        PERMITIR_CADASTRO_PUBLICO=True,
    )
    def test_signup_freeze_wins_over_public_signup_setting(self):
        response = self.client.get(reverse("signup"))
        self.assertRedirects(response, reverse("login"))


@override_settings(WHATSAPP_WEB_ENABLED=False)
class MembershipRoleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "role-user", password="test", is_staff=True,
        )
        self.user.perfil.marcar_verificado()
        self.membership = Membership.objects.get(
            user=self.user,
            organization=self.user.personal_organization,
        )
        self.client.force_login(self.user)

    def test_viewer_can_read_but_cannot_mutate_or_start_sse_job(self):
        self.membership.role = "viewer"
        self.membership.save(update_fields=["role"])

        self.assertEqual(self.client.get(reverse("scraper-dashboard")).status_code, 200)
        self.assertEqual(self.client.post(reverse("scraper-automacao")).status_code, 403)
        self.assertEqual(self.client.get(reverse("scraper-gerar-links")).status_code, 403)

    def test_operator_cannot_manage_credentials_or_connections(self):
        self.membership.role = "operator"
        self.membership.save(update_fields=["role"])

        self.assertEqual(self.client.get(reverse("scraper-dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("scraper-whatsapp")).status_code, 403)


class RLSPolicyTests(SimpleTestCase):
    def test_session_tables_are_protected_and_force_rls_is_emitted(self):
        self.assertIn("accounts_mercadolivresession", STRICT_TENANT_TABLES)
        self.assertIn("accounts_whatsappconnection", STRICT_TENANT_TABLES)
        self.assertIn("accounts_perfil", STRICT_TENANT_TABLES)
        statements = policy_statements(
            "accounts_mercadolivresession", mixed=False,
        )
        self.assertTrue(any("CREATE POLICY tenant_select" in sql for sql in statements))
        self.assertTrue(any("WITH CHECK" in sql for sql in statements))
        self.assertTrue(any("current_user IN" in sql for sql in statements))
        self.assertTrue(any("app.organization_signature" in sql for sql in statements))
        self.assertTrue(any("app.system_signature" in sql for sql in statements))
        self.assertTrue(any(
            "tenant_security.context_valid" in sql for sql in statements
        ))
        self.assertIn(
            'ALTER TABLE "accounts_mercadolivresession" FORCE ROW LEVEL SECURITY',
            statements,
        )


class TenantContextSigningTests(SimpleTestCase):
    @override_settings(
        APP_ENV="production",
        TENANT_CONTEXT_SIGNING_KEY=TENANT_CONTEXT_KEY,
    )
    def test_context_signature_is_hmac_sha256_and_tenant_bound(self):
        organization_id = "62df844f-824b-42bd-82c0-a25076c67ab4"
        expected = hmac.new(
            TENANT_CONTEXT_KEY.encode("utf-8"),
            f"organization:{organization_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            _context_signature("organization", organization_id),
            expected,
        )
        self.assertNotEqual(
            _context_signature(
                "organization",
                "dfc593bc-c7ea-483f-ab20-2eb335e81bd4",
            ),
            expected,
        )

    @override_settings(APP_ENV="production", TENANT_CONTEXT_SIGNING_KEY="")
    def test_production_context_without_key_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            _context_signature("system")


class PonteORMForaDoLoopTests(TransactionTestCase):
    """`executar_no_tenant`: a ponte que substituiu DJANGO_ALLOW_ASYNC_UNSAFE.

    A API sync do Playwright deixa um event loop rodando num greenlet desta mesma
    thread, e o @async_unsafe do Django derruba qualquer query enquanto isso durar.
    O bypass antigo era uma variável de ambiente GLOBAL AO PROCESSO: com 8 threads no
    gunicorn, o `finally` de um fluxo removia a permissão no meio de outro. Aqui a
    query é desviada para uma thread sem loop — e o tenant precisa ser reinstalado
    lá, porque contextvars não cruzam threads de executor.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("ponte-orm", password="test")
        self.organization = self.user.personal_organization

    def test_fora_do_playwright_roda_na_mesma_thread(self):
        # É isto que mantém `TestCase` (transação revertida no fim) enxergando as
        # escritas do resto da suíte: sem loop rodando, não há desvio nenhum.
        with organization_context(self.organization):
            thread = executar_no_tenant(threading.get_ident)
        self.assertEqual(thread, threading.get_ident())

    def test_dentro_do_playwright_desvia_para_outra_thread_e_persiste(self):
        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            thread = executar_no_tenant(threading.get_ident)
            executar_no_tenant(
                Produto.objects.create, owner=self.user, nome="Gravado pela ponte",
                preco_sem_desconto=100, preco_com_cupom=80,
                marketplace="mercadolivre", origem="oferta",
            )

        self.assertNotEqual(thread, threading.get_ident())
        self.assertTrue(
            Produto.objects.filter(nome="Gravado pela ponte").exists(),
            "a escrita feita na thread da ponte tem de estar commitada",
        )

    def test_o_escopo_de_organizacao_e_reinstalado_na_thread(self):
        # Sem reinstalar, a organização da chamada anterior sobreviveria na conexão
        # persistente do executor e vazaria para o tenant seguinte.
        vistos = []
        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            executar_no_tenant(lambda: vistos.append(current_organization_id()))
        self.assertEqual(vistos, [str(self.organization.pk)])

    def test_excecao_propaga_em_vez_de_ficar_presa_no_future(self):
        def explode():
            raise ZeroDivisionError("falha real")

        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            with self.assertRaises(ZeroDivisionError):
                executar_no_tenant(explode)

    def test_sem_tenant_falha_fechado(self):
        # Falhar aqui é melhor que falhar na RLS minutos depois, com o browser já
        # fechado e o dado capturado perdido.
        with self.assertRaises(ValueError):
            executar_no_tenant(lambda: None)
