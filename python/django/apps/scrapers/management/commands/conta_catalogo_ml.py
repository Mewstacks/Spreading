"""Declara qual organização fornece a sessão do catálogo de cupons do ML.

A página /cupons/filter do Mercado Livre exige login, mas a tabela `Cupom` é
compartilhada (sem dono) e o conteúdo é igual para todas as contas. Em vez de
raspar o mesmo catálogo uma vez por organização, uma é declarada como fonte.

    manage.py conta_catalogo_ml                      # mostra a situação atual
    manage.py conta_catalogo_ml --definir minha-org  # passa a ser a fonte
    manage.py conta_catalogo_ml --limpar             # nenhuma fonte
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import MercadoLivreSession, Organization


class Command(BaseCommand):
    help = "Mostra ou define a organização fonte do catálogo de cupons do ML."

    def add_arguments(self, parser):
        parser.add_argument("--definir", metavar="SLUG",
                            help="Slug da organização que passa a ser a fonte.")
        parser.add_argument("--limpar", action="store_true",
                            help="Remove a marcação de todas as organizações.")

    def handle(self, *args, **opts):
        if opts["limpar"]:
            n = Organization.objects.filter(fonte_catalogo_ml=True).update(
                fonte_catalogo_ml=False)
            self.stdout.write(f"{n} organização(ões) desmarcada(s).")
            return

        slug = opts["definir"]
        if slug:
            org = Organization.objects.filter(slug=slug).first()
            if org is None:
                raise CommandError(
                    f"Organização '{slug}' não existe. Use o comando sem "
                    f"argumentos para ver a lista.")
            if org.status != "active":
                raise CommandError(
                    f"Organização '{slug}' está com status '{org.status}'.")
            # Só uma fonte por vez: duas contas raspando o mesmo catálogo global
            # só duplicariam trabalho e disputariam as mesmas linhas.
            with transaction.atomic():
                Organization.objects.filter(fonte_catalogo_ml=True).update(
                    fonte_catalogo_ml=False)
                Organization.objects.filter(pk=org.pk).update(fonte_catalogo_ml=True)
            self.stdout.write(self.style.SUCCESS(
                f"'{org.name}' ({org.slug}) agora é a fonte do catálogo de cupons."))
            if not MercadoLivreSession.objects.filter(
                    organization=org, status="active").exists():
                self.stdout.write(self.style.WARNING(
                    "Atenção: essa organização ainda NÃO tem sessão ativa do "
                    "Mercado Livre. Conecte em Conexão Mercado Livre antes de "
                    "rodar a raspagem de cupons."))
            return

        com_sessao = set(MercadoLivreSession.objects.filter(
            status="active").values_list("organization_id", flat=True))
        fonte = Organization.objects.filter(fonte_catalogo_ml=True).first()
        self.stdout.write(
            f"Fonte atual: {fonte.slug if fonte else '(nenhuma)'}\n")
        self.stdout.write("Organizações:")
        for org in Organization.objects.order_by("name"):
            marca = "*" if org.fonte_catalogo_ml else " "
            sessao = "sessão ML ok" if org.id in com_sessao else "SEM sessão ML"
            self.stdout.write(
                f" {marca} {org.slug:30} {org.status:10} {sessao}")
        if fonte is None:
            self.stdout.write(self.style.WARNING(
                "\nSem fonte definida, a raspagem de cupons de campanha do ML não "
                "roda. Defina com --definir <slug>."))
