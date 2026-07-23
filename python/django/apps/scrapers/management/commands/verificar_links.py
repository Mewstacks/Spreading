"""Verifica o DESTINO dos links de afiliado ML e persiste o veredito.

Backfill/manutenção da fonte única de aprovação: percorre os LinkAfiliadoUsuario
com link gerado mas ainda SEM veredito (verificado_ok IS NULL) e roda a mesma
verificação de destino que o envio usaria, gravando verificado_ok/url_canonica.

Depois disto, a listagem só oferece envio para links realmente aprovados — um
link que redireciona para a vitrine /social/ do afiliado (e não para o anúncio)
some da tela de envio com o motivo, em vez de reprovar só no clique de enviar.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.session_paths import ml_auth_path


class Command(BaseCommand):
    help = ("Verifica o destino dos links de afiliado do Mercado Livre ainda sem "
            "veredito e persiste verificado_ok/url_canonica.")

    def add_arguments(self, parser):
        parser.add_argument("--usuario", help="Username a verificar (padrão: todos com sessão ML).")
        parser.add_argument("--limite", type=int, default=200,
                            help="Máximo de links a verificar por usuário (padrão 200).")

    def handle(self, *args, **options):
        from apps.scrapers.scraper_mercadolivre.link import verificar_links_pendentes

        username = (options.get("usuario") or "").strip()
        limite = options["limite"]
        User = get_user_model()
        usuarios = User.objects.filter(is_active=True).order_by(User.USERNAME_FIELD)
        if username:
            usuario = usuarios.filter(**{User.USERNAME_FIELD: username}).first()
            if usuario is None:
                raise CommandError(f'Usuário ativo "{username}" não encontrado.')
            usuarios = [usuario]
        else:
            usuarios = [u for u in usuarios if ml_auth_path(u)]

        total = {"aprovados": 0, "reprovados": 0, "transitorios": 0}
        for usuario in usuarios:
            r = verificar_links_pendentes(usuario, limite=limite)
            for k in total:
                total[k] += r[k]
            self.stdout.write(
                f"{usuario.get_username()}: {r['aprovados']} aprovado(s), "
                f"{r['reprovados']} reprovado(s), {r['transitorios']} transitório(s).")
        self.stdout.write(self.style.SUCCESS(
            f"Concluído: {total['aprovados']} aprovado(s), {total['reprovados']} "
            f"reprovado(s), {total['transitorios']} transitório(s)."))
