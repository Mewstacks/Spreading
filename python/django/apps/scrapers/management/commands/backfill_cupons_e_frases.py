"""Backfill único após a mudança de filtros de cupom e do formato de mensagem.

1) Recalcula `CupomNormalizado.categoria` dos cupons ativos — os scrapers de
   produção gravavam `escopo=""`, deixando o dropdown de categoria vazio em prod.
2) Limpa `Produto.frase_llm` — o cache guardava a antiga *frase* de venda; agora o
   campo guarda o *título* curto, então precisa regenerar no próximo envio.

Idempotente: rodar de novo não faz mal. Rode após aplicar as migrations.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.coupon_rules import (
    derivar_categoria_cupom, regras_do_cupom, rotulo_anunciante)
from apps.scrapers.models import CupomNormalizado, Produto


class Command(BaseCommand):
    help = "Recalcula categoria dos cupons ativos e limpa o cache frase_llm."

    def add_arguments(self, parser):
        parser.add_argument("--so-cupons", action="store_true",
                            help="Só recalcula categoria dos cupons.")
        parser.add_argument("--so-frases", action="store_true",
                            help="Só limpa o cache frase_llm.")

    def handle(self, *args, **opts):
        so_cupons = opts.get("so_cupons")
        so_frases = opts.get("so_frases")

        if not so_frases:
            cupons = list(CupomNormalizado.objects.filter(estado="ativo").filter(
                Q(validade__isnull=True) | Q(validade__gte=timezone.now())))
            # Fallback do rótulo: categoria dominante dos produtos de cada campanha
            # (mesma fonte que a projeção usa), em uma query só.
            from apps.scrapers.scraper_mercadolivre.scraper import (
                _categoria_dominante_por_campanha)
            campanha_de = {}
            for cupom in cupons:
                ext = str(cupom.external_id or "")
                if ext.startswith("campanha:"):
                    campanha_de[cupom.pk] = ext.split(":", 1)[1]
            dominante = _categoria_dominante_por_campanha(list(campanha_de.values()))
            atualizados = 0
            for cupom in cupons:
                regras = regras_do_cupom(cupom)
                campos = []
                nova = derivar_categoria_cupom(cupom.titulo, regras)
                if nova and nova != (cupom.categoria or ""):
                    cupom.categoria = nova
                    campos.append("categoria")
                # Só preenche o anunciante derivado quando está vazio: Awin/manual já
                # gravam o anunciante real e não devem ser sobrescritos.
                if not (cupom.anunciante_nome or "").strip():
                    fallback = dominante.get(campanha_de.get(cupom.pk, ""), "")
                    rotulo = rotulo_anunciante(cupom.titulo, regras,
                                               categoria_fallback=fallback)
                    if rotulo:
                        cupom.anunciante_nome = rotulo
                        campos.append("anunciante_nome")
                if campos:
                    cupom.save(update_fields=campos)
                    atualizados += 1
            self.stdout.write(self.style.SUCCESS(
                f"Categoria/anunciante recalculado em {atualizados} cupom(ns)."))

        if not so_cupons:
            limpos = Produto.objects.exclude(frase_llm="").update(frase_llm="")
            self.stdout.write(self.style.SUCCESS(
                f"frase_llm limpo em {limpos} produto(s) (regenera no próximo envio)."))
