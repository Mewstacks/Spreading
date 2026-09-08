"""Verifica os gates objetivos antes do canário privado e do deploy real."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.tenant import system_context
from apps.scrapers.production_readiness import avaliar


class Command(BaseCommand):
    help = "Gates read-only de produção para uma conta de divulgação."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="lules")
        parser.add_argument("--min-taxonomia", type=float, default=95.0)

    def handle(self, *args, **options):
        username = str(options["username"] or "").strip()
        minimo = float(options["min_taxonomia"])
        if not username:
            raise CommandError("--username é obrigatório.")
        if not 0 < minimo <= 100:
            raise CommandError("--min-taxonomia deve ficar entre 0 e 100.")

        with system_context():
            usuario = get_user_model().objects.filter(
                username=username, is_active=True,
            ).first()
            if usuario is None:
                raise CommandError(f"Usuário ativo não encontrado: {username}")
            resultado = avaliar(usuario, minimo_taxonomia=minimo)

        for nome, passou in resultado["gates"].items():
            estado = self.style.SUCCESS("PASSOU") if passou else self.style.ERROR("FALHOU")
            self.stdout.write(f"{estado}\t{nome}")
        taxonomia = resultado["taxonomia"]
        self.stdout.write(
            "taxonomia: "
            f"{taxonomia['classificados']}/{taxonomia['total']} "
            f"({taxonomia['percentual']:.1f}%; mínimo {resultado['minimo_taxonomia']:.1f}%)"
        )
        self.stdout.write(
            "alerta: "
            f"telegram={resultado['alerta']['telegram']} email={resultado['alerta']['email']}"
        )
        self.stdout.write(f"esteiras: {resultado['esteiras']}")
        self.stdout.write(
            "regras prioritárias Lu: "
            + (", ".join(map(str, resultado["config_ids_prioritarios"])) or "nenhuma")
        )
        if not resultado["aprovado"]:
            raise CommandError(
                "Produção não aprovada: corrija todos os gates antes do canário privado."
            )
        self.stdout.write(self.style.SUCCESS("PRODUÇÃO APTA PARA CANÁRIO PRIVADO"))
