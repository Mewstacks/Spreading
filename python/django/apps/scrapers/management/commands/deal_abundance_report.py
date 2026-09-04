"""Cobertura de deals por regra de envio, com déficit nomeado.

Responde a pergunta que `coupon_abundance_report` não responde: cada creator tem
deal suficiente NO NICHO DELE? Mil cupons prontos e zero deal em "Robô aspirador"
é aprovação no relatório de cupons e reprovação aqui — e aqui é onde importa.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.deal_abundance import relatorio_cobertura


class Command(BaseCommand):
    help = "Deals elegíveis por regra contra a meta, com motivo do déficit."

    def add_arguments(self, parser):
        parser.add_argument("--meta", type=int, default=None)
        parser.add_argument("--username", default="")
        parser.add_argument("--todas", action="store_true",
                            help="Inclui regras desativadas.")

    def handle(self, *args, **options):
        from apps.accounts.tenant import system_context

        with system_context():
            usuario = None
            username = str(options.get("username") or "").strip()
            if username:
                usuario = get_user_model().objects.filter(username=username).first()
                if usuario is None:
                    raise CommandError(f"Usuário não encontrado: {username}")
            relatorio = relatorio_cobertura(
                meta=options["meta"], usuario=usuario,
                apenas_ativas=not options["todas"],
            )
            self.stdout.write(
                "config\tdestino\tmacro\telegiveis\tcom_cupom\tmeta\tdeficit\t"
                "veredito\tmelhor_score\trejeicoes")
            for regra in relatorio["regras"]:
                rejeicoes = ", ".join(
                    f"{motivo}={total}"
                    for motivo, total in list(regra["rejeicoes"].items())[:4]
                ) or "-"
                self.stdout.write(
                    f"{regra['config_id']}\t{regra['destino'][:28]}\t"
                    f"{regra['macro'][:18] or '-'}\t{regra['elegiveis']}\t"
                    f"{regra['com_cupom']}\t{regra['meta']}\t{regra['deficit']}\t"
                    f"{regra['veredito']}\t{regra['melhor_score'] or '-'}\t{rejeicoes}"
                )
            self.stdout.write(f"meta por regra: {relatorio['meta']}")
            self.stdout.write(
                f"aprovado: {'sim' if relatorio['aprovado'] else 'nao'}")
            if relatorio["coleta_incompleta"]:
                self.stdout.write(
                    "coleta incompleta nas regras: "
                    + ", ".join(str(i) for i in relatorio["coleta_incompleta"])
                )
