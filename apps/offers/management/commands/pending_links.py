from django.core.management.base import BaseCommand, CommandError

from apps.offers.links import pending_urls
from apps.orgs.models import Organization


class Command(BaseCommand):
    help = "Lista as URLs de hoje sem link do dono (cole no Link Builder do ML)."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True)

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options["org"])
        except Organization.DoesNotExist:
            raise CommandError("organização não encontrada") from None
        for url in pending_urls(org):
            self.stdout.write(url)
