"""Abundância por loja contra a meta, com déficit e prova de exaustão.

Saída em TSV, como os outros relatórios do funil, para poder ser colada numa
evidência de aceite sem reformatação.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.coupon_abundance import MARKETPLACES_META, relatorio_abundancia


class Command(BaseCommand):
    help = (
        "Cupons distintos prontos por marketplace contra a meta, com déficit, "
        "bloqueios e exaustão das fontes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--channel", default="whatsapp")
        parser.add_argument("--meta", type=int, default=None)
        parser.add_argument(
            "--username", default="",
            help=(
                "Mede prontidão e bloqueios somente para esta conta. Sem o "
                "argumento, mantém a visão global deduplicada do catálogo."
            ),
        )

    def handle(self, *args, **options):
        from apps.accounts.tenant import system_context

        with system_context():
            username = str(options.get("username") or "").strip()
            usuario = None
            if username:
                usuario = get_user_model().objects.filter(username=username).first()
                if usuario is None:
                    raise CommandError(f"Usuário não encontrado: {username}")
            relatorio = relatorio_abundancia(
                channel=options["channel"], meta=options["meta"], usuario=usuario,
            )
            relatorio["escopo_usuario"] = username or "global"
        self._imprimir(relatorio)

    def _imprimir(self, relatorio):
        self.stdout.write(
            f"META\tcupons_distintos_prontos={relatorio['meta']}\t"
            f"canal={relatorio['canal']}\t"
            f"usuario={relatorio.get('escopo_usuario', 'global')}\t"
            f"aprovado={int(relatorio['aprovado'])}"
        )
        self.stdout.write(
            "marketplace\tprontos\tmeta\tdeficit\tveredito\tdescoberta_24h\t"
            "meta_descoberta\tclasses\tdescoberta_ok\tmodos\tfontes_nao_exauridas"
        )
        for marketplace in MARKETPLACES_META:
            loja = relatorio["lojas"][marketplace]
            modos = ",".join(
                f"{modo}:{total}" for modo, total in sorted(loja["por_modo"].items())
            )
            self.stdout.write(
                f"{marketplace}\t{loja['prontos']}\t{loja['meta']}\t"
                f"{loja['deficit']}\t{loja['veredito']}\t"
                f"{loja['descoberta_24h']}\t{loja['meta_descoberta_24h']}\t"
                f"{','.join(loja['classes_descoberta']) or '-'}\t"
                f"{int(loja['descoberta_atingida'])}\t{modos or '-'}\t"
                f"{','.join(loja['fontes_nao_exauridas']) or '-'}"
            )
        self.stdout.write("marketplace\tstage\tcategory\treason_code\tcupons")
        for marketplace in MARKETPLACES_META:
            for bloqueio in relatorio["lojas"][marketplace]["bloqueios"]:
                self.stdout.write(
                    f"{marketplace}\t{bloqueio['stage']}\t{bloqueio['category']}\t"
                    f"{bloqueio['reason_code']}\t{bloqueio['cupons']}"
                )
        self.stdout.write(
            "marketplace\tfonte\texaustao\tstop_reason\tstatus\thealth\t"
            "vistos\taceitos"
        )
        for marketplace in MARKETPLACES_META:
            for fonte in relatorio["lojas"][marketplace]["fontes"]:
                self.stdout.write(
                    f"{marketplace}\t{fonte['fonte']}\t{fonte['exaustao']}\t"
                    f"{fonte['stop_reason'] or '-'}\t{fonte['status']}\t"
                    f"{fonte['health'] or '-'}\t{fonte['vistos']}\t{fonte['aceitos']}"
                )
        abaixo = [
            marketplace for marketplace in MARKETPLACES_META
            if relatorio["lojas"][marketplace]["deficit"]
        ]
        if abaixo:
            self.stderr.write(self.style.WARNING(
                "Abaixo da meta: " + ", ".join(
                    f"{marketplace} (-{relatorio['lojas'][marketplace]['deficit']}, "
                    f"{relatorio['lojas'][marketplace]['veredito']})"
                    for marketplace in abaixo
                )
            ))
