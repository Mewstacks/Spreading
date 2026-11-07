from django.core.management.base import BaseCommand

from apps.coupons.feed import fetch_feed, ingest


class Command(BaseCommand):
    help = "Baixa o feed de cupons do ML e atualiza o catálogo."

    def handle(self, *args, **options):
        total = ingest(fetch_feed())
        self.stdout.write(f"cupons no feed: {total}")
