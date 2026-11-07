from django.core.management.base import BaseCommand, CommandError

from apps.offers.links import import_pairs
from apps.orgs.models import Organization


class Command(BaseCommand):
    help = "Importa linhas 'url_do_produto link_afiliado' de um arquivo (ou stdin com -)."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True)
        parser.add_argument("--etiqueta", default="")
        parser.add_argument("file")

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options["org"])
        except Organization.DoesNotExist:
            raise CommandError("organização não encontrada") from None
        if options["file"] == "-":
            import sys

            text = sys.stdin.read()
        else:
            with open(options["file"], encoding="utf-8") as handle:
                text = handle.read()
        self.stdout.write(str(import_pairs(org, text, options["etiqueta"])))
