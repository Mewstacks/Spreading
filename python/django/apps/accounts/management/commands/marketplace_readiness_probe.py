"""Gate nominal, sem segredos, das conexões usadas pelo pipeline de cupons.

O diagnóstico geral é útil para suporte, mas não serve como canário: uma
sessão saudável de outro tenant pode esconder que a conta que será testada está
desconectada. Este comando sempre exige um username e avalia apenas a organização
ativa desse usuário.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.ml_session_crypto import MLSessionCryptoError
from apps.accounts.ml_sessions import (
    PROBE_FALHAS_PARA_DESCONECTAR as ML_FAILURE_LIMIT,
    load_storage_state,
)
from apps.accounts.models import BrowserSession, MercadoLivreSession, Perfil
from apps.accounts.tenant import system_context
from apps.scrapers.models import IntegracaoAfiliado
from apps.scrapers.report_sessions import (
    PROBE_FALHAS_PARA_DESCONECTAR as BROWSER_FAILURE_LIMIT,
)


MARKETPLACES = ("mercadolivre", "amazon", "shopee")


def _browser_readiness(record) -> tuple[bool, str]:
    if record is None:
        return False, "sessao_ausente"
    if record.status == "decrypt_error":
        return False, "decrypt_error"
    failures = int(record.probe_failures or 0)
    if failures >= BROWSER_FAILURE_LIMIT:
        return False, f"suspeitas_conclusivas={failures}"
    try:
        state = json.loads(record.encrypted_state)
    except Exception:
        return False, "storage_state_ilegivel"
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return False, "storage_state_invalido"
    if not state.get("cookies") and not state.get("origins"):
        return False, "storage_state_sem_credencial"
    return True, (
        "pronta"
        if failures == 0
        else f"utilizavel_com_suspeitas={failures}"
    )


def _ml_readiness(record, state) -> tuple[bool, str]:
    if record is None or state is None:
        return False, "sessao_ausente"
    if record.status not in {"active", "suspect"}:
        return False, f"status={record.status}"
    failures = int(record.probe_failures or 0)
    if failures >= ML_FAILURE_LIMIT:
        return False, f"suspeitas_conclusivas={failures}"
    if record.last_probe_result != "conectado":
        return False, f"probe={record.last_probe_result or 'nao_executado'}"
    if record.lb_readiness != "ready":
        return False, f"linkbuilder={record.lb_readiness or 'unknown'}"
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return False, "storage_state_invalido"
    if not state.get("cookies"):
        return False, "storage_state_sem_cookies"
    return True, "pronta_com_linkbuilder"


def _shopee_affiliate_readiness(integration) -> tuple[bool, str]:
    if integration is None:
        return False, "integracao_afiliada_ausente"
    if not integration.habilitada or integration.status != "conectada":
        return False, f"integracao={integration.status}"
    if not str(integration.identificador_conta or "").strip():
        return False, "app_id_ausente"
    if not str(integration.token or "").strip():
        return False, "secret_ausente"
    return True, "integracao_afiliada_pronta"


def _amazon_config_readiness(profile, *, require_creators=False) -> tuple[bool, str]:
    if profile is None or not str(profile.afiliado_tag_amazon or "").strip():
        return False, "partner_tag_ausente"
    if not require_creators:
        return True, "partner_tag_pronta"
    credentials = bool(
        str(profile.amazon_credential_id or "").strip()
        and str(profile.amazon_credential_secret or "").strip()
    )
    if not credentials:
        return False, "creators_credenciais_ausentes"
    if profile.amazon_elegivel is not True:
        return False, "creators_conta_inelegivel"
    return True, "partner_tag_e_creators_prontos"


class Command(BaseCommand):
    help = "Valida as conexões de marketplace de uma conta específica."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--marketplace", action="append", choices=MARKETPLACES,
            help="Limita o gate; repetível. Padrão: as três lojas.",
        )
        parser.add_argument(
            "--require-ready", action="store_true",
            help="Retorna erro se qualquer gate selecionado não estiver pronto.",
        )
        parser.add_argument(
            "--require-creators", action="store_true",
            help="Na Amazon, exige também credenciais e elegibilidade Creators API.",
        )
        parser.add_argument(
            "--live", action="store_true",
            help=(
                "Executa as sondas externas baratas disponíveis: HTTP do ML e "
                "API afiliada da Shopee. Não abre Chromium."
            ),
        )

    def handle(self, *args, **options):
        username = str(options["username"] or "").strip()
        selected = tuple(dict.fromkeys(options.get("marketplace") or MARKETPLACES))
        require_creators = bool(options.get("require_creators"))
        live = bool(options.get("live"))

        with system_context():
            user = get_user_model().objects.filter(username=username).first()
            if user is None:
                raise CommandError(f"Usuário {username!r} não encontrado.")
            profile = Perfil.objects.filter(user=user).first()
            organization_id = getattr(profile, "active_organization_id", None)
            if not organization_id:
                raise CommandError(f"Usuário {username!r} sem organização ativa.")

            browser_records = {
                row.provider: row
                for row in BrowserSession.objects.filter(
                    organization_id=organization_id,
                    user=user,
                    provider__in=("amazon_shop", "shopee_shop"),
                )
            }
            ml_record = MercadoLivreSession.objects.filter(
                organization_id=organization_id,
            ).first()
            try:
                ml_state = load_storage_state(user, touch=False) if ml_record else None
                ml_decrypt_error = ""
            except (MLSessionCryptoError, ValueError, TypeError):
                ml_state = None
                ml_decrypt_error = "decrypt_error"
            shopee_integration = IntegracaoAfiliado.objects.filter(
                owner=user, provedor="shopee",
            ).first()

        results: dict[str, tuple[bool, str]] = {}
        if "mercadolivre" in selected:
            results["mercadolivre"] = (
                (False, ml_decrypt_error)
                if ml_decrypt_error else _ml_readiness(ml_record, ml_state)
            )
        if "amazon" in selected:
            browser_ok, browser_reason = _browser_readiness(
                browser_records.get("amazon_shop")
            )
            config_ok, config_reason = _amazon_config_readiness(
                profile, require_creators=require_creators,
            )
            results["amazon"] = (
                browser_ok and config_ok,
                f"shop={browser_reason}; afiliacao={config_reason}",
            )
        if "shopee" in selected:
            browser_ok, browser_reason = _browser_readiness(
                browser_records.get("shopee_shop")
            )
            affiliate_ok, affiliate_reason = _shopee_affiliate_readiness(
                shopee_integration
            )
            results["shopee"] = (
                browser_ok and affiliate_ok,
                f"shop={browser_reason}; afiliacao={affiliate_reason}",
            )

        if live and "mercadolivre" in selected and ml_state is not None:
            from apps.scrapers.conexoes import sondar_sessao_ml

            verdict, _reason = sondar_sessao_ml(ml_state)
            previous_ok, previous_reason = results["mercadolivre"]
            results["mercadolivre"] = (
                previous_ok and verdict == "conectado",
                f"{previous_reason}; live={verdict}",
            )
        if live and "shopee" in selected and shopee_integration is not None:
            from apps.scrapers.shopee import (
                ShopeeError, credenciais_da_integracao, validar_credenciais,
            )

            previous_ok, previous_reason = results["shopee"]
            try:
                app_id, secret = credenciais_da_integracao(shopee_integration)
                validar_credenciais(app_id, secret)
                live_ok, live_reason = True, "conectada"
            except (ShopeeError, ValueError, TypeError):
                live_ok, live_reason = False, "recusada_ou_indisponivel"
            results["shopee"] = (
                previous_ok and live_ok,
                f"{previous_reason}; live={live_reason}",
            )

        for marketplace in selected:
            ok, reason = results[marketplace]
            self.stdout.write(
                f"{marketplace}: {'READY' if ok else 'NOT_READY'} ({reason})"
            )

        failed = [name for name, (ok, _reason) in results.items() if not ok]
        if options.get("require_ready") and failed:
            raise CommandError(
                f"Conta {username!r} ainda não está pronta: {', '.join(failed)}."
            )
        self.stdout.write(self.style.SUCCESS(
            f"Probe nominal concluído para {username!r}; "
            f"prontas={len(results) - len(failed)}/{len(results)}."
        ))

