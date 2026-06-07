"""
Orquestrador manual do pipeline de envio de ofertas (CLI / debug).

Compartilha o núcleo `enviar_oferta_de_produto` com as tasks Celery.

Uso:
  python manage.py enviar_oferta                      # envia 1 oferta de verdade
  python manage.py enviar_oferta --limite 3           # até 3 ofertas
  python manage.py enviar_oferta --dry-run            # só imprime, não envia, não grava histórico
  python manage.py enviar_oferta --no-verificar       # pula a verificação no browser
  python manage.py enviar_oferta --grupo 12345@g.us   # sobrescreve o grupo de destino
  python manage.py enviar_oferta --macro Eletrônicos  # filtra macro-categoria
"""
from django.core.management.base import BaseCommand

from apps.scrapers.ofertas import selecionar_item_para_grupo, enviar_oferta_de_produto


class Command(BaseCommand):
    help = "Seleciona e envia ofertas de Mercado Livre para o WhatsApp."

    def add_arguments(self, parser):
        parser.add_argument("--limite", type=int, default=1)
        parser.add_argument("--cooldown", type=int, default=24, help="Horas de cooldown por produto.")
        parser.add_argument("--min-desconto", type=float, default=15.0, help="Desconto mínimo (%).")
        parser.add_argument("--grupo", type=str, default=None, help="grupoid de destino (sobrescreve o padrão).")
        parser.add_argument("--macro", action="append", default=None, help="Filtra macro-categoria (repetível).")
        parser.add_argument("--categoria", action="append", default=None, help="Filtra categoria (repetível).")
        parser.add_argument("--dry-run", action="store_true", help="Não envia nem grava histórico.")
        parser.add_argument("--no-verificar", action="store_true", help="Pula a verificação do link no browser.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        verificar = not opts["no_verificar"]

        vencedores = selecionar_item_para_grupo(
            macros_selecionadas=opts["macro"],
            categorias_selecionadas=opts["categoria"],
            limite_envio=opts["limite"],
            horas_cooldown=opts["cooldown"],
            min_desconto_percent=opts["min_desconto"],
        )

        if not vencedores:
            self.stdout.write(self.style.WARNING("Nenhum produto elegível para envio."))
            return

        enviados = 0
        for produto in vencedores:
            self.stdout.write(f"\n— Produto: {produto.nome[:70]}")
            r = enviar_oferta_de_produto(produto, opts["grupo"], verificar=verificar, dry_run=dry)

            if r.get("link"):
                self.stdout.write(f"  Link: {r['link']}")
            if r.get("verificacao"):
                v = r["verificacao"]
                self.stdout.write(f"    verif: produto={v.get('is_pagina_produto')} "
                                  f"cupom={v.get('cupom_detectado')} preço={v.get('preco_visivel')}")

            if dry and r.get("sucesso"):
                self.stdout.write(self.style.NOTICE("  [DRY-RUN] mensagem:"))
                self.stdout.write(r["mensagem"])
            elif r.get("sucesso"):
                enviados += 1
                self.stdout.write(self.style.SUCCESS(f"  Enviado (via {r.get('via')})."))
            else:
                self.stdout.write(self.style.ERROR(f"  Falhou: {r.get('motivo')}"))

        if not dry:
            self.stdout.write(self.style.SUCCESS(f"\nConcluído. {enviados} oferta(s) enviada(s)."))
