"""Devolve à verificação os links reprovados por uma regra que ninguém podia passar.

Todo short link do Programa de Afiliados do ML resolve para a vitrine `/social/`
do afiliado — o item viaja dentro do `ref` cifrado. Medido em produção
(13/08/2026): os links de oferta APROVADOS caem exatamente no mesmo lugar que os
de cupom. A diferença era só que produto de cupom precisa provar o desconto, e a
prova era exigida NO DESTINO: 0 aprovados em 4.447 produtos com campanha, contra
10.807 aprovados entre os sem campanha.

A regra foi corrigida na origem (ver `link_http.relatorio_de_link_com_cupom`, que
lê o desconto na PDP de origem e a atribuição no short link). Este comando existe
para o passivo: as linhas reprovadas ANTES da correção, que a fila de verificação
não reabre sozinha — ela só olha quem ainda não tem veredito, de propósito, para
não gastar Chromium reconfirmando a mesma reprovação da mesma URL.

A URL destas linhas está boa; só o veredito estava errado. Por isso elas voltam
para a VERIFICAÇÃO (verificado_ok=None), não para a geração: nenhum link novo é
pedido ao Link Builder.

Reprovação que é do próprio produto (URL recusada pelo Programa, anúncio pausado)
é preservada, e link já aprovado nunca é tocado.

    python manage.py reabrir_links_reprovados_na_vitrine --dry-run
    python manage.py reabrir_links_reprovados_na_vitrine

NUNCA no `release_command`: o release anterior segue servindo enquanto isto roda,
repõe linhas e o laço não termina (ver DEPLOY.md).
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.accounts.tenant import system_job
from apps.scrapers.models import LinkAfiliadoUsuario

# Motivos gravados por regras que já foram corrigidas na origem. Os dois descrevem
# o mesmo tipo de erro: um veredito definitivo dado por uma condição que o link não
# tinha como satisfazer no momento em que foi olhado.
MOTIVOS_SUPERADOS = (
    # Exigia a PDP no destino; nenhum short link do Programa entrega isso.
    "Caiu na vitrine /social/",
    # Reprovava quem ainda não tinha ProdutoCupom confirmado — prova que o worker
    # de cupons reconstrói a cada ciclo. Virou espera, não veredito.
    "O desconto deste cupom não está comprovado",
)


def _afetadas():
    condicao = Q()
    for motivo in MOTIVOS_SUPERADOS:
        # `verificacao_motivo` é onde a reprovação de destino é registrada;
        # `ultimo_erro` cobre as linhas gravadas pelo caminho de geração.
        condicao |= Q(verificacao_motivo__icontains=motivo)
        condicao |= Q(ultimo_erro__icontains=motivo)
    return LinkAfiliadoUsuario.objects.filter(
        verificado_ok=False, produto__marketplace="mercadolivre",
    ).filter(condicao).exclude(link_afiliado="")


class Command(BaseCommand):
    help = ("Reabre a verificação dos links reprovados por caírem na vitrine "
            "/social/, sem pedir link novo ao Link Builder.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Só relata; não escreve nada.")

    @system_job
    def handle(self, *args, **opts):
        afetadas = _afetadas()
        total = afetadas.count()
        if not total:
            self.stdout.write("Nenhum link reprovado pela vitrine /social/.")
            return

        por_origem = Counter(afetadas.values_list("produto__origem", flat=True))
        self.stdout.write(f"AFETADAS: {total} linha(s)")
        for origem, n in por_origem.most_common():
            self.stdout.write(f"  {n:6d}  origem={origem or '(vazia)'}")

        if opts["dry_run"]:
            self.stdout.write("Execução seca: nada foi alterado.")
            return

        # `estado` volta a "pronto" porque o link EXISTE e é utilizável; o que
        # falta é veredito. `proxima_tentativa=None` tira o backoff herdado da
        # reprovação errada, para a fila pegá-las já no próximo ciclo.
        reabertas = afetadas.update(
            verificado_ok=None, estado="pronto", proxima_tentativa=None,
            verificacao_motivo="", ultimo_erro="",
        )
        self.stdout.write(
            f"REABERTAS: {reabertas} linha(s) voltaram para a fila de verificação "
            "com a URL que já tinham."
        )
