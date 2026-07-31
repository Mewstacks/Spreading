"""Força AGORA a releitura do catálogo de cupons do ML.

O deploy já não depende disto: a migração 0054 neutraliza os dados corrompidos e o
worker de scrape repõe os valores no ciclo seguinte. Este comando existe só para
quem não quer esperar o ciclo — ou para reparar o banco depois de um incidente.

Corrige os dados de cupom já gravados com as regras antigas.

Três defeitos deixaram lastro no banco e continuam sendo publicados até alguém
reescrever as linhas:

1. compra mínima com separador de milhar lida como unidade (R$ 2.000 -> R$ 2,00);
2. teto de desconto (`capAmount`) nunca gravado, então o preço final saía menor
   que o real;
3. produtos de `origem='cupom'` gravados com o preço JÁ descontado em
   `preco_com_cupom`, o que fazia o cupom ser aplicado duas vezes.

O comando invalida o que foi calculado com essas regras e repõe a fonte. Sem
`--pular-scrape` ele reabre o Mercado Livre para reler o catálogo de campanhas.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.scrapers.models import (
    CupomCodigo, CupomPreparacao, Produto, ProdutoCupom,
)


class Command(BaseCommand):
    help = "Reprocessa cupons e preços afetados pelos parsers antigos."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Só relata o que seria alterado, sem gravar nada.")
        parser.add_argument(
            "--pular-scrape", action="store_true",
            help="Não reabre o Mercado Livre; só invalida o que está no banco.")

    def handle(self, *args, **opts):
        seco = opts["dry_run"]
        prefixo = "[dry-run] " if seco else ""

        # A releitura vem PRIMEIRO, de propósito. Invalidar antes deixava o banco
        # num meio-termo pior que o original quando o scrape falhava (sessão do ML
        # expirada): produtos stale e catálogo ainda com os valores errados.
        relidos = None
        if not seco and not opts["pular_scrape"]:
            from apps.accounts.ml_sessions import SemContaDeCatalogo
            from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
            from apps.scrapers.scraper_mercadolivre.scraper import (
                mapear_cupons, projetar_catalogo_cupons,
            )
            try:
                relidos = mapear_cupons()
            except SemContaDeCatalogo as exc:
                raise CommandError(f"{exc} (nada foi alterado)")
            except SessaoExpirada:
                raise CommandError(
                    "Sessão do Mercado Livre expirada — nada foi alterado. "
                    "Reconecte em Conexão Mercado Livre e rode de novo. "
                    "Para invalidar o que está no banco sem reler o ML, use "
                    "--pular-scrape.")
            except BrowserError as exc:
                raise CommandError(
                    f"Não foi possível abrir o Mercado Livre ({exc}); nada foi "
                    f"alterado.")
            if not relidos:
                raise CommandError(
                    "O Mercado Livre não devolveu nenhum cupom; nada foi alterado "
                    "(repetir depois evita zerar o catálogo por falha de rede).")
            self.stdout.write(f"{relidos} cupom(ns) de campanha relido(s) do ML")
            ativos = projetar_catalogo_cupons()
            self.stdout.write(f"Catálogo reprojetado: {ativos} campanha(s) ativa(s)")

        automaticos = CupomCodigo.objects.filter(
            descricao="cupom ML (checkout)", automatico=False)
        n_auto = automaticos.count()
        if n_auto and not seco:
            automaticos.update(automatico=True)
        self.stdout.write(
            f"{prefixo}{n_auto} código(s) de checkout marcado(s) como automático")

        # Os produtos da lane de cupom foram gravados com a semântica antiga de
        # preço. Marcá-los stale tira-os do envio. Eles voltam com o contrato novo
        # (tabela | vitrine) quando o cupom correspondente for preparado:
        # `coupon_products._coletar_ml_remoto` casa pelo link, regrava os preços e
        # reativa a linha. O ciclo automático NÃO reescreve esta lane sozinho — ela
        # é sob demanda (preparar_lote) ou pelo botão Scraper (scraper.main).
        produtos = Produto.objects.filter(
            marketplace="mercadolivre", owner__isnull=True, origem="cupom",
        ).exclude(estado="stale")
        n_prod = produtos.count()
        if n_prod and not seco:
            produtos.update(
                estado="stale",
                falha_verificacao="Preço regravado: semântica de cupom corrigida",
                ultima_verificacao=timezone.now())
        self.stdout.write(
            f"{prefixo}{n_prod} produto(s) de cupom marcado(s) para nova coleta")

        # Preços por cupom e caches de preparo saíram do cálculo antigo (sem teto,
        # com mínimo errado). Expirar força `preparar_cupom` a recalcular tudo.
        relacoes = ProdutoCupom.objects.filter(status="confirmado")
        n_rel = relacoes.count()
        preparos = CupomPreparacao.objects.exclude(produtos_chave="")
        n_prep = preparos.count()
        if not seco:
            with transaction.atomic():
                relacoes.update(status="expirado")
                preparos.update(produtos_chave="", status="vazio",
                                erro="Invalidado por corrigir_cupons")
        self.stdout.write(
            f"{prefixo}{n_rel} vínculo(s) produto-cupom e {n_prep} preparo(s) "
            f"invalidado(s)")

        if relidos is None:
            self.stdout.write(self.style.WARNING(
                "Catálogo de campanhas NÃO foi relido. Rode sem --dry-run/"
                "--pular-scrape (ou aguarde o próximo ciclo) para repor "
                "estado, validade, compra mínima e teto."))
        else:
            self.stdout.write(self.style.SUCCESS("Cupons corrigidos."))
