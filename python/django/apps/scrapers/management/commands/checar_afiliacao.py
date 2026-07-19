"""Autoteste dos links de afiliado configurados por usuário."""
import os
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.afiliado import tag_amazon
from apps.scrapers.eventos import log_event
from apps.scrapers.models import LinkAfiliadoUsuario
from apps.scrapers.scraper_amazon.link import link_tem_tag_afiliado as amazon_tem_tag
from apps.scrapers.scraper_mercadolivre.link import (
    link_tem_tag_afiliado as ml_tem_tag,
)
from apps.scrapers.session_paths import ml_auth_path


class Command(BaseCommand):
    help = "Valida a atribuição dos links de Mercado Livre e Amazon por usuário."

    def add_arguments(self, parser):
        parser.add_argument(
            "--usuario",
            help="Username a verificar. Sem a opção, verifica usuários ativos com sessão ML.",
        )

    def handle(self, *args, **options):
        username = (options.get("usuario") or "").strip()
        User = get_user_model()
        usuarios = User.objects.filter(is_active=True).order_by(
            User.USERNAME_FIELD)

        if username:
            lookup = {User.USERNAME_FIELD: username}
            usuario = usuarios.filter(**lookup).first()
            if usuario is None:
                raise CommandError(f'Usuário ativo "{username}" não encontrado.')
            usuarios = [usuario]
        else:
            usuarios = [
                usuario for usuario in usuarios
                if os.path.exists(ml_auth_path(usuario))
            ]

        if not usuarios:
            self.stdout.write(self.style.WARNING(
                "Nenhum usuário ativo com sessão do Mercado Livre foi encontrado."))
            return

        falhas = 0
        for usuario in usuarios:
            falhas += self._checar_usuario(usuario)

        if falhas:
            self.stdout.write(self.style.ERROR(
                f"Concluído com {falhas} verificação(ões) sem atribuição."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Concluído: {len(usuarios)} usuário(s) sem falhas de atribuição."))

    def _checar_usuario(self, usuario) -> int:
        self.stdout.write(f"\n{usuario.get_username()}:")
        falhas = 0

        if not os.path.exists(ml_auth_path(usuario)):
            self.stdout.write(self.style.WARNING(
                "  Mercado Livre: sessão não encontrada; cache ainda pode ser usado."))

        cacheado = (
            LinkAfiliadoUsuario.objects
            .filter(
                usuario=usuario,
                produto__marketplace="mercadolivre",
            )
            .exclude(link_afiliado="")
            .select_related("produto")
            .order_by("-criado_em", "-id")
            .first()
        )
        if cacheado is None:
            self.stdout.write(self.style.WARNING(
                "  Mercado Livre: ainda não há link cacheado para validar."))
        elif ml_tem_tag(cacheado.link_afiliado, usuario=usuario):
            self.stdout.write(self.style.SUCCESS(
                f"  Mercado Livre: link cacheado válido ({cacheado.produto.nome[:60]})."))
        else:
            falhas += 1
            self._registrar_sem_tag(usuario, "mercadolivre", cacheado.link_afiliado)
            self.stdout.write(self.style.ERROR(
                "  Mercado Livre: o link cacheado mais recente não confirmou a atribuição."))

        tag = tag_amazon(usuario)
        if not tag:
            self.stdout.write(self.style.WARNING(
                "  Amazon: tag de afiliado não configurada."))
        else:
            host = "www.amazon.com.br"
            link_amostra = f"https://{host}/dp/B000000000?{urlencode({'tag': tag})}"
            if amazon_tem_tag(link_amostra, usuario=usuario):
                self.stdout.write(self.style.SUCCESS(
                    "  Amazon: tag confirmada no link de amostra."))
            else:
                falhas += 1
                self._registrar_sem_tag(usuario, "amazon", link_amostra)
                self.stdout.write(self.style.ERROR(
                    "  Amazon: o link de amostra não confirmou a tag configurada."))

        return falhas

    @staticmethod
    def _registrar_sem_tag(usuario, marketplace, link):
        log_event(
            "scraper",
            "afiliacao_sem_tag",
            f"O autoteste não confirmou a atribuição do link de {marketplace}.",
            level="warning",
            usuario=usuario,
            contexto={
                "causa": "link_sem_tag",
                "marketplace": marketplace,
                "link_host": link.split("/", 3)[2] if "://" in link else "",
            },
        )
