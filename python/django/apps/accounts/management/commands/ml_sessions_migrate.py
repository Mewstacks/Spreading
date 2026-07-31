import hashlib
import json
import os
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.ml_session_crypto import decrypt_storage_state
from apps.accounts.ml_sessions import save_storage_state
from apps.accounts.models import MercadoLivreSession
from apps.accounts.tenant import system_context


_USER_FILE = re.compile(r"^auth_(\d+)\.json$")


def _digest(state):
    raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).digest()


class Command(BaseCommand):
    help = "Audita/migra auth_{user}.json para AES-256-GCM no Postgres."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        directory = settings.ML_AUTH_DIR
        if not directory or not os.path.isdir(directory):
            self.stdout.write("Diretório legado ausente; nada para migrar.")
            return

        files = sorted(
            name for name in os.listdir(directory)
            if name == "auth.json" or _USER_FILE.match(name)
        )
        errors = []
        migrated = 0
        User = get_user_model()

        with system_context():
            for name in files:
                if name == "auth.json":
                    errors.append(
                        "auth.json global é ambíguo e não será atribuído a nenhum tenant"
                    )
                    continue
                user_id = int(_USER_FILE.match(name).group(1))
                user = User.objects.filter(pk=user_id).first()
                if user is None:
                    errors.append(f"{name}: usuário inexistente")
                    continue
                path = os.path.join(directory, name)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        state = json.load(handle)
                    cookies = state.get("cookies") if isinstance(state, dict) else None
                    if not isinstance(cookies, list) or not any(
                        "mercadolivre" in str(cookie.get("domain", "")).lower()
                        or "mercadolibre" in str(cookie.get("domain", "")).lower()
                        for cookie in cookies if isinstance(cookie, dict)
                    ):
                        raise ValueError("storage state sem cookie Mercado Livre")
                    if not options["apply"]:
                        self.stdout.write(f"DRY-RUN {name}: válido para user={user_id}")
                        continue

                    before = _digest(state)
                    record = save_storage_state(user, state)
                    after = _digest(decrypt_storage_state(record))
                    if before != after:
                        raise ValueError("verificação após criptografia divergiu")
                    os.remove(path)
                    migrated += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"{name}: migrado e plaintext removido"
                    ))
                except Exception as exc:
                    errors.append(f"{name}: {exc}")

            decrypt_errors = MercadoLivreSession.objects.filter(
                status="decrypt_error",
            ).count()
            if decrypt_errors:
                errors.append(f"{decrypt_errors} sessão(ões) com decrypt_error")

        if errors:
            for error in errors:
                self.stderr.write(error)
            raise CommandError(f"Migração/auditoria falhou com {len(errors)} erro(s).")
        action = "migradas" if options["apply"] else "validadas"
        self.stdout.write(self.style.SUCCESS(
            f"Sessões ML {action}: {migrated if options['apply'] else len(files)}."
        ))

