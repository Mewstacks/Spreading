import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


LEGACY_ML = re.compile(r"^auth(?:_\d+)?\.json$")


class Command(BaseCommand):
    help = "Falha se houver storage state ML em plaintext no volume ativo."

    def handle(self, *args, **options):
        root = settings.ML_AUTH_DIR
        if not root or not os.path.isdir(root):
            self.stdout.write("Volume de sessões ausente; nenhum plaintext encontrado.")
            return

        findings = []
        for entry in os.scandir(root):
            if entry.is_file(follow_symlinks=False) and LEGACY_ML.fullmatch(entry.name):
                findings.append(entry.name)

        if findings:
            for name in sorted(findings):
                self.stderr.write(f"plaintext legado encontrado: {name}")
            raise CommandError(
                f"{len(findings)} storage state(s) plaintext no volume ativo."
            )
        self.stdout.write(self.style.SUCCESS(
            "Auditoria de plaintext: zero storage state ML legado."
        ))
