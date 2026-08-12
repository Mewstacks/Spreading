"""Devolve à fila os links travados por um problema de conta ou de capacidade.

Enquanto a causa de conta era gravada como falha do produto, cada ciclo contra uma
sessão do Mercado Livre caída (ou contra o único Chromium ocupado) penalizava o lote
inteiro: tentativas somadas, backoff crescente e, na oitava rodada, `nao_afiliavel`
em produto que nunca teve defeito. Em produção sobraram 1.528 linhas assim — 878 por
`LoginError` e 650 por `BrowserResourceUnavailable`.

A gravação foi corrigida na origem (ver `coupon_pipeline.afiliar_cupons` e
`afiliado.causa_de_conta`), e a reconexão da sessão já chama
`reabrir_bloqueios_de_conta`. Este comando existe para o passivo: as linhas
envenenadas ANTES da correção, que ninguém reabriria sem uma reconexão manual.

Falha real do produto ("o Programa de Afiliados não aceitou a URL") é preservada, e
link já aprovado nunca é tocado.

    python manage.py reabrir_bloqueios_de_conta --dry-run
    python manage.py reabrir_bloqueios_de_conta
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.accounts.tenant import system_job
from apps.scrapers.afiliado import _CAUSAS_DE_CAPACIDADE, _CAUSAS_DE_CONTA
from apps.scrapers.models import LinkAfiliadoUsuario


def _afetadas():
    condicao = Q()
    for causa in _CAUSAS_DE_CONTA + _CAUSAS_DE_CAPACIDADE:
        condicao |= Q(ultimo_erro__icontains=causa)
    return LinkAfiliadoUsuario.objects.filter(condicao).exclude(verificado_ok=True)


class Command(BaseCommand):
    help = ("Reabre links travados por sessão/capacidade, preservando as falhas "
            "que são do próprio produto.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Só relata; não escreve nada.")

    @system_job
    def handle(self, *args, **opts):
        afetadas = _afetadas()
        total = afetadas.count()
        if not total:
            self.stdout.write("Nenhuma linha travada por conta ou capacidade.")
            return

        por_causa = Counter()
        for erro in afetadas.values_list("ultimo_erro", flat=True):
            for causa in _CAUSAS_DE_CONTA + _CAUSAS_DE_CAPACIDADE:
                if causa.casefold() in (erro or "").casefold():
                    por_causa[causa] += 1
                    break
        por_estado = Counter(afetadas.values_list("estado", flat=True))

        self.stdout.write(f"AFETADAS: {total} linha(s)")
        for causa, n in por_causa.most_common():
            self.stdout.write(f"  {n:6d}  {causa}")
        self.stdout.write(f"  estados: {dict(por_estado)}")

        if opts["dry_run"]:
            self.stdout.write("Execução seca: nada foi alterado.")
            return

        from apps.scrapers.afiliado import reabrir_bloqueios_de_conta

        reabertas = reabrir_bloqueios_de_conta()
        self.stdout.write(
            f"REABERTAS: {reabertas} linha(s) voltaram para a fila como 'pendente', "
            "sem tentativas acumuladas."
        )
