"""Backfill da macro-categoria a partir do nome do produto.

O ciclo de cupons já classifica o que tem par confirmado a cada 15 minutos. Este
comando existe para a primeira passada sobre o que já está no banco, e para rodar
o catálogo inteiro quando se quiser — o ciclo, de propósito, só olha o que muda o
funil hoje.

    python manage.py categorizar_por_nome --dry-run
    python manage.py categorizar_por_nome --apenas-com-cupom
    python manage.py categorizar_por_nome
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.accounts.tenant import system_context
from apps.scrapers.categorizar_por_nome import (
    CATEGORIA_SEM_AUTORIDADE, macro_do_nome, popular_macro_por_nome,
)
from apps.scrapers.maintenance import produtos_frescos_q
from apps.scrapers.models import Produto


class Command(BaseCommand):
    help = "Preenche Produto.macro_categoria vazia a partir do nome."

    def add_arguments(self, parser):
        parser.add_argument("--limite", type=int, default=None)
        parser.add_argument(
            "--apenas-com-cupom", action="store_true",
            help="Só produtos com par confirmado e cupom ativo.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Mostra o que seria classificado, sem gravar.")
        parser.add_argument(
            "--frescos", action="store_true",
            help="No dry-run, limita à janela de produtos publicáveis atual.")
        parser.add_argument(
            "--amostras", type=int, default=12,
            help="No dry-run, quantidade de títulos sem veredito local a exibir.")
        parser.add_argument(
            "--elegiveis", action="store_true",
            help="No dry-run, audita só o mesmo pool que pode virar publicação.")

    def handle(self, *args, **opts):
        with system_context():
            if opts["dry_run"]:
                self._prever(opts)
                return
            n = popular_macro_por_nome(
                limite=opts["limite"],
                apenas_com_cupom=opts["apenas_com_cupom"],
                produto_ids=(
                    [produto.pk for produto in self._pool_elegivel()]
                    if opts["elegiveis"] else None
                ),
            )
            self.stdout.write(self.style.SUCCESS(
                f"{n} produto(s) classificado(s) pelo nome."))

    @staticmethod
    def _pool_elegivel():
        from apps.scrapers.ofertas import pool_de_produtos_elegiveis
        return pool_de_produtos_elegiveis(min_desconto_percent=15.0)

    def _prever(self, opts):
        # Espelha o alvo real de `popular_macro_por_nome`: uma macro antiga sobre
        # categoria DESCONHECIDO não é autoridade e pode ser corrigida pelo nome.
        sem_autoridade = (
            Q(macro_categoria__isnull=True) | Q(macro_categoria="")
            | Q(categoria__in=CATEGORIA_SEM_AUTORIDADE)
            | Q(categoria__isnull=True)
        )
        qs = Produto.objects.filter(sem_autoridade).exclude(nome="")
        if opts["elegiveis"]:
            ids = [produto.pk for produto in self._pool_elegivel()]
            qs = qs.filter(pk__in=ids)
        elif opts["frescos"]:
            qs = qs.filter(produtos_frescos_q())
        if opts["apenas_com_cupom"]:
            qs = qs.filter(
                cupons_normalizados__status="confirmado",
                cupons_normalizados__cupom__estado="ativo",
            ).distinct()
        qs = qs.order_by("-ultima_observacao", "-id")
        if opts["limite"]:
            qs = qs[:opts["limite"]]

        total = classificados = 0
        por_macro = {}
        exemplos = []
        for produto in qs.iterator(chunk_size=500):
            total += 1
            macro = macro_do_nome(produto.nome)
            if not macro:
                if len(exemplos) < max(0, opts["amostras"]):
                    exemplos.append(("sem veredito", produto.nome[:96]))
                continue
            classificados += 1
            por_macro[macro] = por_macro.get(macro, 0) + 1

        self.stdout.write(f"sem autoridade: {total} | classificáveis: {classificados}")
        for macro, n in sorted(por_macro.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {n:>5}  {macro}")
        if exemplos:
            self.stdout.write("amostras sem veredito local:")
            for macro, nome in exemplos:
                self.stdout.write(f"  {macro[:34]:<34} <- {nome}")
