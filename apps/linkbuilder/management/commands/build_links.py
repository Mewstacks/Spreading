"""Gera os links meli.la que faltam, uma execução curta por vez. `--loop SEGUNDOS` repete."""
import time

from django.core.management.base import BaseCommand

from apps.linkbuilder import runner
from apps.orgs.models import Organization


class Command(BaseCommand):
    help = "Gera os links do Link Builder das ofertas que as regras ativas vão publicar."

    def add_arguments(self, parser):
        parser.add_argument("--org", default="", help="slug; vazio = todas as organizações")
        parser.add_argument("--loop", type=int, default=0)

    def handle(self, *args, **options):
        while True:
            orgs = Organization.objects.all()
            if options["org"]:
                orgs = orgs.filter(slug=options["org"])
            for org in orgs:
                try:
                    self.stdout.write(f"{org.slug}: {runner.run(org)}")
                except Exception as exc:  # noqa: BLE001  uma org ruim não derruba as outras
                    self.stderr.write(f"{org.slug}: falhou: {exc}")
            if not options["loop"]:
                return
            time.sleep(options["loop"])
