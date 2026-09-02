"""Canonicaliza links e funde `Produto` duplicado — revisável antes de executar.

Existe como comando, e não só como migração, porque a operação é destrutiva e
grande: em produção mexe em dezenas de milhares de linhas e apaga milhares.
`--dry-run` mostra o tamanho exato do estrago antes de qualquer escrita; uma
migração `RunPython` não dá para revisar olhando o diff.

    manage.py fundir_produtos_duplicados --dry-run
    manage.py fundir_produtos_duplicados
"""
from django.core.management.base import BaseCommand

from apps.accounts.tenant import system_context
from apps.scrapers import fusao_produtos


class Command(BaseCommand):
    help = (
        "Canonicaliza link_produto e funde produtos duplicados, preservando "
        "links de afiliado verificados, histórico de envio e publicações."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Só mede: não grava link canônico nem funde nada.",
        )
        parser.add_argument("--lote", type=int, default=200,
                            help="Grupos fundidos por transação.")

    def handle(self, *args, **options):
        dry = bool(options["dry_run"])
        # Catálogo compartilhado é cross-tenant: sem este contexto o RLS
        # esconde as linhas e o comando "roda com sucesso" sem ver nada.
        with system_context():
            mudam = fusao_produtos.canonicalizar_links(dry_run=dry)
            self.stdout.write(f"LINKS\tcanonicalizados={mudam}\tdry_run={int(dry)}")
            resumo = fusao_produtos.executar(lote=options["lote"], dry_run=dry)
        for chave in sorted(resumo):
            self.stdout.write(f"FUSAO\t{chave}={resumo[chave]}")
        if dry:
            self.stdout.write(self.style.WARNING(
                "dry-run: nada foi gravado nem apagado."))
        else:
            self.stdout.write(self.style.SUCCESS("Fusão concluída."))
