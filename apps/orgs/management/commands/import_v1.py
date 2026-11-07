from django.core.management.base import BaseCommand

from apps.offers import v1_catalog
from apps.orgs.models import Organization
from apps.orgs.v1_import import import_file


class Command(BaseCommand):
    help = "Importa o JSON de tools/export_v1.py e, opcional, catálogo e pares cupom+produto."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--catalog")
        parser.add_argument("--pairs")
        parser.add_argument("--org", default="lules")
        parser.add_argument("--amazon-tag", default="")

    def handle(self, *args, **options):
        self.stdout.write(str(import_file(options["file"])))
        org = Organization.objects.filter(slug=options["org"]).first()
        if org and options["amazon_tag"]:
            org.amazon_tag = options["amazon_tag"]
            org.save(update_fields=["amazon_tag"])
        if options["catalog"]:
            self.stdout.write("catálogo: " + str(
                v1_catalog.import_catalog(v1_catalog.load(options["catalog"]))))
        if options["pairs"]:
            self.stdout.write("pares: " + str(
                v1_catalog.import_pairs(v1_catalog.load(options["pairs"]), org)))
