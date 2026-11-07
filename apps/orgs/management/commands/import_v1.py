from django.core.management.base import BaseCommand

from apps.orgs.v1_import import import_file


class Command(BaseCommand):
    help = "Importa o JSON de tools/export_v1.py (orgs, regras WhatsApp, links do dono)."

    def add_arguments(self, parser):
        parser.add_argument("file")

    def handle(self, *args, **options):
        self.stdout.write(str(import_file(options["file"])))
