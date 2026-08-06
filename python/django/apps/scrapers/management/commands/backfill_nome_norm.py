"""Preenche `Produto.nome_norm` no catálogo que já existia antes da coluna.

Roda DEPOIS do deploy, não no release_command. Ver a migração
0057_produto_nome_normalizado para o porquê: no release_command quem serve é o
release anterior, que não conhece a coluna, então ele repõe linhas vazias no mesmo
ritmo em que o backfill as drena.

    fly ssh console -a spreading-web -C \
        "sh -c 'env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py backfill_nome_norm'"

O `TENANT_SYSTEM_PROCESS=1` não é enfeite: sem ele o processo abre o banco com a
role de runtime, `system_context()` recusa com PermissionDenied e o comando morre
antes de ler uma linha. É a mesma exigência de `bootstrap_superuser`.

Idempotente: só toca linha com `nome_norm` vazio. Pode ser repetido à vontade e
interrompido a qualquer momento — a próxima execução continua de onde parou.
"""
import unicodedata

from django.core.management.base import BaseCommand
from django.db.models import Max

from apps.accounts.tenant import system_context


def normalizar(texto) -> str:
    if not texto:
        return ""
    decomposto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in decomposto if not unicodedata.combining(c)).casefold()


class Command(BaseCommand):
    help = "Preenche Produto.nome_norm nas linhas anteriores à coluna."

    def add_arguments(self, parser):
        parser.add_argument("--lote", type=int, default=2000,
                            help="Linhas por UPDATE (padrão 2000).")

    def handle(self, *args, **opts):
        from apps.scrapers.models import Produto

        lote_tamanho = max(1, opts["lote"])
        # Cross-tenant: o catálogo do ML é pool compartilhado (owner=None) e o da
        # Amazon é privado por usuário. Sem isto a RLS devolve zero linha e o
        # comando diz "nada a fazer" com o banco cheio.
        with system_context():
            # Avança pela PK, que é indexada. Filtrar por `nome_norm=""` a cada lote
            # faria uma varredura sequencial da tabela inteira por rodada — não há
            # índice nessa coluna, e não vale criar um só para isto.
            teto = Produto.objects.aggregate(m=Max("id"))["m"] or 0
            cursor = 0
            total = tocados = 0
            while cursor < teto:
                lote = list(
                    Produto.objects.filter(id__gt=cursor, id__lte=teto)
                    .only("id", "nome", "nome_norm").order_by("id")[:lote_tamanho]
                )
                if not lote:
                    break
                cursor = lote[-1].id
                total += len(lote)
                pendentes = []
                for produto in lote:
                    if produto.nome_norm:
                        continue
                    novo = normalizar(produto.nome)[:300]
                    if novo:
                        produto.nome_norm = novo
                        pendentes.append(produto)
                if pendentes:
                    Produto.objects.bulk_update(pendentes, ["nome_norm"])
                    tocados += len(pendentes)
                self.stdout.write(
                    f"  id<={cursor}  lidos={total}  preenchidos={tocados}")
        self.stdout.write(self.style.SUCCESS(
            f"Backfill concluído: {tocados} de {total} produto(s) preenchidos."))
