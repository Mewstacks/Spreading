"""Sugere (e opcionalmente cria) canais de descoberta do Telegram para um usuário.

Deliberadamente NÃO liga nada sozinho e NÃO roda no boot. Monitorar um canal de
terceiro é decisão do dono da conta: é a reputação dele que vai junto com cada
mensagem re-divulgada. O comando existe para que essa decisão seja informada em vez
de o usuário ter que garimpar handle no Google — mas quem aperta o botão é ele.

    # só listar o que existe, sem criar nada
    python manage.py semear_canais --usuario g2rmano

    # criar os monitoramentos apontando para um grupo de destino
    python manage.py semear_canais --usuario g2rmano \\
        --destino-canal whatsapp --destino-grupo 5511999999999-1600000000@g.us --criar

Os canais nascem com ``ativo=False``. Ligar cada um é um ato consciente na tela, e
até lá nenhuma mensagem sai.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.tenant import system_job
from apps.scrapers.canais.seeds import CANAIS_SUGERIDOS, RECUSADOS, sugestoes_para
from apps.scrapers.models import CanalMonitorado


class Command(BaseCommand):
    help = "Lista e opcionalmente cria canais do Telegram sugeridos para descoberta."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True,
                            help="username do dono dos monitoramentos")
        parser.add_argument("--marketplace", default="",
                            help="filtra sugestões por loja (mercadolivre/amazon/shopee)")
        parser.add_argument("--destino-canal", default="whatsapp",
                            choices=("whatsapp", "telegram"))
        parser.add_argument("--destino-grupo", default="",
                            help="id do grupo de destino; obrigatório com --criar")
        parser.add_argument("--criar", action="store_true",
                            help="cria os CanalMonitorado (inativos) além de listar")

    @system_job
    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            usuario = User.objects.get(username=opts["usuario"])
        except User.DoesNotExist:
            raise CommandError(f"Usuário {opts['usuario']} não existe.")

        sugestoes = sugestoes_para(opts["marketplace"])
        if not sugestoes:
            raise CommandError(
                f"Nenhuma sugestão para o marketplace {opts['marketplace']!r}."
            )

        self.stdout.write("Canais sugeridos (medidos em 18/08/2026):\n")
        for canal in sugestoes:
            lojas = ", ".join(canal["marketplaces"])
            self.stdout.write(
                f"  @{canal['handle']:<28} {canal['densidade']:>3} links/20 posts  "
                f"[{lojas}]\n      {canal['nome']} — {canal['nota']}"
            )

        self.stdout.write("\nRecusados de propósito:")
        for handle, motivo in RECUSADOS.items():
            self.stdout.write(f"  @{handle:<28} {motivo}")

        if not opts["criar"]:
            self.stdout.write(
                "\nNada foi criado. Rode de novo com --criar e --destino-grupo "
                "para registrar os monitoramentos (eles nascem desligados)."
            )
            return

        destino = (opts["destino_grupo"] or "").strip()
        if not destino:
            raise CommandError("--destino-grupo é obrigatório junto com --criar.")

        criados = existentes = 0
        for canal in sugestoes:
            _, novo = CanalMonitorado.objects.get_or_create(
                owner=usuario, handle=canal["handle"],
                destino_canal=opts["destino_canal"], destino_grupo_id=destino,
                defaults={
                    # Nasce DESLIGADO: criar não é autorizar a publicar.
                    "ativo": False,
                    "organization": getattr(usuario, "organization", None),
                },
            )
            criados += int(novo)
            existentes += int(not novo)

        self.stdout.write(
            f"\n{criados} canal(is) criado(s), {existentes} já existia(m). "
            "Todos estão DESLIGADOS — ative um a um depois de conferir o conteúdo."
        )
