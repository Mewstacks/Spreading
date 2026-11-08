import argparse
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.offers.models import SourceRun

STALE = timedelta(minutes=15)  # três ciclos de 5 minutos


class Command(BaseCommand):
    help = "Saúde do worker para o Docker: sai com 1 se o ciclo parou há mais de 15 minutos."

    def add_arguments(self, parser):
        parser.add_argument("--now", default=None, help=argparse.SUPPRESS)  # só para testes

    def handle(self, *args, **options):
        now = options.get("now") or timezone.now()
        latest = (SourceRun.objects.order_by("-checked_at")
                  .values_list("checked_at", flat=True).first())
        if latest is None:
            raise CommandError("nenhum ciclo registrado")
        age = now - latest
        minutes = int(age.total_seconds() // 60)
        if age > STALE:
            raise CommandError(f"ciclo parado há {minutes} min")
        self.stdout.write(f"ciclo há {minutes} min")
