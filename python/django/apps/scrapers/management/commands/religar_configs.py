"""Religa ConfiguracaoEnvio desligadas por falha transitória. Uso:
    python manage.py religar_configs --dry-run   # lista o que faria
    python manage.py religar_configs             # aplica

One-shot de reparo. Até a taxonomia de erro existir (node.js/error_taxonomy.js +
whatsapp_client), `processar_configs_de_envio` contava qualquer falha contra a
regra e a desligava ao bater `pausar_apos_falhas`. Worker fora do ar, timeout ou
simples falta de oferta no nicho desligavam a automação de quem não tinha defeito
nenhum na configuração — e nada religava. Corrigir o código não desfaz o que já
está gravado no banco: é para isso que este comando existe.

Rode DEPOIS do deploy da correção. Antes dela, as configs religam e caem de novo.

Nada aqui é permanente: se uma config voltar a falhar por motivo real, o
orquestrador a desliga de novo — agora com razão.
"""
from django.core.management.base import BaseCommand

from apps.scrapers.models import ConfiguracaoEnvio

# Trechos de `motivo_pausa` que denunciam causa transitória. Casados sem
# distinção de caixa e sem acento-insensibilidade: são as strings literais que
# ofertas.py/whatsapp_client.py gravaram, incluindo as variantes sem acento que
# vêm do Node.
#
# Lista explícita em vez de "religar tudo": uma config parada por grupo apagado
# ou link sem tag de afiliado tem de continuar parada — religá-la só produziria
# falha nova. Use --all para revisar o resto na mão.
MOTIVOS_TRANSITORIOS = (
    "sem item elegível",
    "falha de transporte",
    "não está conectado",
    "nao esta conectado",
    "estava recarregando",
    "muitas requisições",
    "id de confirmação",
    "verificar o grupo de destino",
    "timeout",
    "sessão whatsapp do usuário ausente",
    "nenhum candidato passou",
)


def _e_transitorio(motivo: str) -> bool:
    alvo = (motivo or "").lower()
    return any(trecho in alvo for trecho in MOTIVOS_TRANSITORIOS)


class Command(BaseCommand):
    help = "Reativa configurações de envio desligadas por falha transitória."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Só lista o que seria religado; não escreve.")
        parser.add_argument("--all", action="store_true",
                            help="Religa TODA config inativa, não só as de motivo "
                                 "transitório. Use com --dry-run antes.")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        todas = opts["all"]

        inativas = ConfiguracaoEnvio.objects.filter(ativo=False)
        if not inativas.exists():
            self.stdout.write("Nenhuma configuração inativa. Nada a fazer.")
            return

        alvos, ignoradas = [], []
        for cfg in inativas:
            (alvos if todas or _e_transitorio(cfg.motivo_pausa) else ignoradas).append(cfg)

        for cfg in ignoradas:
            self.stdout.write(
                f"  ignorada #{cfg.id} ({cfg.grupo_nome or cfg.grupo_id}): "
                f"{cfg.motivo_pausa or 'sem motivo registrado'}"
            )
        for cfg in alvos:
            self.stdout.write(
                f"  religar #{cfg.id} ({cfg.grupo_nome or cfg.grupo_id}): "
                f"{cfg.motivo_pausa or 'sem motivo registrado'}"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {len(alvos)} config(s) seriam religadas; "
                f"{len(ignoradas)} ficariam paradas. Nada foi escrito."
            ))
            return

        for cfg in alvos:
            cfg.ativo = True
            cfg.falhas_consecutivas = 0
            cfg.motivo_pausa = ""
            # proximo_envio fica como está: as configs vencidas entram já no
            # próximo tick, e o jitter de agendar_proximo evita que todas
            # disparem no mesmo instante.
            cfg.save(update_fields=["ativo", "falhas_consecutivas", "motivo_pausa"])

        self.stdout.write(self.style.SUCCESS(
            f"{len(alvos)} config(s) religada(s); {len(ignoradas)} mantida(s) parada(s)."
        ))
