"""Materializa `CupomDisponibilidade` para contas que ficaram sem projeção.

Roda DEPOIS do deploy, fora do processo web:

    fly ssh console -a spreading-web -C \
        "sh -c 'env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py backfill_disponibilidade_cupons'"

Por que existe um comando em vez de a tela se virar: a projeção é um laço de
milhares de escritas e o processo web não consegue commitá-lo. O
`OrganizationContextMiddleware` envolve a request inteira num `transaction.atomic()`
(precisa dele para instalar o escopo RLS com `SET LOCAL`), então o `atomic()` por
cupom de `projetar_disponibilidade_cupons` vira savepoint. O laço batia no
`lock_timeout` de 15s contra o worker `cupons`, tudo voltava atrás e a conta
continuava com zero linhas -- 500 eterno em /scrapers/top/. Aqui, sob
`system_context()`, não há transação externa e cada cupom commita de verdade.

O `TENANT_SYSTEM_PROCESS=1` não é enfeite: sem ele o processo abre o banco com a
role de runtime, `system_context()` recusa com PermissionDenied e o comando morre
antes de ler uma linha. É a mesma exigência de `backfill_nome_norm`.

Idempotente: `projetar_disponibilidade_cupons` é um get_or_create por cupom e só
escreve quando o veredito muda. Pode ser repetido à vontade e interrompido a
qualquer momento -- o worker `cupons` continua de onde parou no próximo tick.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.accounts.tenant import system_context


class Command(BaseCommand):
    help = "Projeta a disponibilidade de cupons das contas sem linhas materializadas."

    def add_arguments(self, parser):
        parser.add_argument(
            "--usuario", action="append", default=None, dest="usuarios",
            help="Username ou id a projetar. Repetível. Padrão: todas as contas ativas.",
        )
        parser.add_argument(
            "--todas", action="store_true",
            help="Reprojeta também quem já tem linhas (padrão: só quem está zerado).",
        )

    def handle(self, *args, **opts):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.models import CupomDisponibilidade

        # Cross-tenant por natureza: o catálogo de cupons do ML é pool compartilhado
        # e o backfill atende várias organizações numa passada. Sem system_context a
        # RLS devolve zero linha e o comando "termina" sem escrever nada.
        with system_context():
            usuarios = self._alvos(opts["usuarios"])
            if not opts["todas"]:
                com_linhas = set(
                    CupomDisponibilidade.objects
                    .values_list("usuario_id", flat=True).distinct()
                )
                usuarios = [u for u in usuarios if u.pk not in com_linhas]
            if not usuarios:
                self.stdout.write("Nada a fazer: nenhuma conta sem projeção.")
                return
            for usuario in usuarios:
                # Uma conta sem organização (ou com catálogo vazio) não pode
                # interromper o backfill das demais.
                try:
                    resumo = projetar_disponibilidade_cupons(usuario)
                except Exception as exc:
                    self.stderr.write(
                        f"{usuario.username}: falhou ({type(exc).__name__}: {exc})"
                    )
                    continue
                estagios = ", ".join(
                    f"{estagio}={total}"
                    for estagio, total in sorted(resumo["stages"].items())
                ) or "sem cupons"
                self.stdout.write(
                    f"{usuario.username}: {resumo['total']} cupons ({estagios})"
                )

    def _alvos(self, referencias):
        usuarios = get_user_model().objects.filter(is_active=True)
        if not referencias:
            return list(usuarios.order_by("pk"))
        selecionados = []
        for referencia in referencias:
            usuario = usuarios.filter(username=referencia).first()
            if usuario is None and str(referencia).isdigit():
                usuario = usuarios.filter(pk=int(referencia)).first()
            if usuario is None:
                self.stderr.write(f"Conta ativa não encontrada: {referencia}")
                continue
            selecionados.append(usuario)
        return selecionados
