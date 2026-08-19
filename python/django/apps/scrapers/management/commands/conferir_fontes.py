"""Confere as fontes públicas contra a rede de verdade, sem gravar nada.

A suíte prova que o parser entende um HTML de exemplo. Ela não prova que o site
continua entregando aquele HTML — e é justamente assim que uma fonte morre: sem erro,
devolvendo zero, com todos os testes verdes. Este comando fecha essa distância.

Não escreve no banco: coleta, mede e imprime. Serve como checagem antes de um deploy
e como primeiro passo de diagnóstico quando o catálogo secar.

    python manage.py conferir_fontes
    python manage.py conferir_fontes --fonte promobit-cupons

Ele falha (código de saída 1) quando uma fonte devolve zero item, porque zero é o
sintoma que precisa acordar alguém.
"""
from django.core.management.base import BaseCommand

from apps.accounts.tenant import system_job

# Só fontes que funcionam sem credencial e sem navegador. As outras exigem conta
# conectada e seriam "falha" aqui por motivo errado.
FONTES = {
    "promobit-cupons": "coupons",
    "telegram-publico": "offers",
    "ml-cupons-afiliados": "coupons",
}


class Command(BaseCommand):
    help = "Coleta as fontes públicas ao vivo e relata o que cada uma devolveu."

    def add_arguments(self, parser):
        parser.add_argument("--fonte", default="",
                            help="confere só uma fonte (slug)")

    @system_job
    def handle(self, *args, **opts):
        from apps.scrapers.sources.registry import SOURCES

        alvo = (opts["fonte"] or "").strip()
        escolhidas = {k: v for k, v in FONTES.items() if not alvo or k == alvo}
        if not escolhidas:
            self.stderr.write(f"Fonte desconhecida: {alvo}")
            return

        vazias = []
        for slug, tipo in escolhidas.items():
            adaptador = SOURCES.get(slug)
            if adaptador is None:
                self.stderr.write(f"{slug}: não registrada")
                vazias.append(slug)
                continue
            try:
                metodo = (adaptador.discover_coupons if tipo == "coupons"
                          else adaptador.discover_offers)
                itens = list(metodo())
            except Exception as exc:
                self.stderr.write(f"{slug}: FALHOU ({type(exc).__name__}: {exc})")
                vazias.append(slug)
                continue

            metricas = dict(getattr(adaptador, "last_metrics", {}) or {})
            por_loja = {}
            for item in itens:
                por_loja[item.marketplace] = por_loja.get(item.marketplace, 0) + 1
            self.stdout.write(
                f"{slug:22s} {len(itens):>4} item(ns)  {por_loja or '{}'}  {metricas}"
            )
            for item in itens[:3]:
                rotulo = item.coupon_code or item.canonical_url[:48]
                self.stdout.write(f"    · {item.marketplace:13s} {rotulo}")
            if not itens:
                vazias.append(slug)

        if vazias:
            # Zero item é o sintoma de fonte morta e não pode passar despercebido
            # num pipeline de verificação.
            self.stderr.write(
                f"\nSem resultado: {', '.join(vazias)} — investigue antes de subir."
            )
            raise SystemExit(1)
        self.stdout.write("\nTodas as fontes públicas responderam com itens.")
