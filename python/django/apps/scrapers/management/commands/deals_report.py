"""Deals de uma regra de envio, com preço final, prova e auditorias.

Saída em TSV, como os outros relatórios do funil, para colar numa evidência de
aceite sem reformatação. As auditorias existem porque a camada Deal só pode ser
ligada com número, não com impressão: `--auditar-precos` prova que todo candidato
respeita `preco_final = vitrine - benefício`, e `--auditar-nicho` prova que nenhum
publicável casa termo negativo ou cai fora da faixa.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.deals import gerar_deals


class Command(BaseCommand):
    help = "Deals elegíveis por regra de envio, com score, prova e auditorias."

    def add_arguments(self, parser):
        parser.add_argument("--config", type=int, default=None,
                            help="Id da ConfiguracaoEnvio. Sem ele, todas as ativas.")
        parser.add_argument("--username", default="", help="Restringe a uma conta.")
        parser.add_argument("--limite", type=int, default=5)
        parser.add_argument("--auditar-precos", action="store_true")
        parser.add_argument("--auditar-nicho", action="store_true")
        parser.add_argument("--shadow", action="store_true",
                            help="Divergências registradas entre legado e Deal.")
        parser.add_argument("--dias", type=int, default=7)

    def handle(self, *args, **options):
        from apps.accounts.tenant import system_context

        with system_context():
            if options["shadow"]:
                return self._shadow(options["dias"])
            configs = self._configs(options)
            if not configs:
                raise CommandError("Nenhuma regra de envio encontrada.")
            if options["auditar_precos"]:
                return self._auditar_precos(configs)
            if options["auditar_nicho"]:
                return self._auditar_nicho(configs)
            return self._listar(configs, options["limite"])

    def _configs(self, options):
        from apps.scrapers.models import ConfiguracaoEnvio

        consulta = ConfiguracaoEnvio.objects.select_related("owner")
        if options["config"]:
            return list(consulta.filter(pk=options["config"]))
        username = str(options.get("username") or "").strip()
        if username:
            usuario = get_user_model().objects.filter(username=username).first()
            if usuario is None:
                raise CommandError(f"Usuário não encontrado: {username}")
            consulta = consulta.filter(owner=usuario)
        return list(consulta.filter(
            ativo=True, tipo=ConfiguracaoEnvio.TIPO_OFERTAS,
        ).order_by("pk"))

    def _listar(self, configs, limite):
        self.stdout.write(
            "config\tpos\tscore\tprova\tvitrine\tbeneficio\tfinal\tcomprovado\tproduto\tmotivos")
        for config in configs:
            deals = gerar_deals(config, limite=limite)
            if not deals:
                self.stdout.write(f"{config.pk}\t-\t-\tsem_deal\t-\t-\t-\t-\t-\t-")
                continue
            for posicao, deal in enumerate(deals, start=1):
                nome = (getattr(deal.produto, "nome", "") or "")[:60]
                self.stdout.write(
                    f"{config.pk}\t{posicao}\t{deal.score}\t{deal.prova}\t"
                    f"{deal.preco_vitrine}\t{deal.beneficio_rs}\t{deal.preco_final}\t"
                    f"{'sim' if deal.desconto_comprovado else 'nao'}\t{nome}\t"
                    f"{'; '.join(deal.motivos[:3])}"
                )

    def _auditar_precos(self, configs):
        from apps.scrapers.deals import PROVAS_VALIDAS

        divergencias = 0
        total = 0
        for config in configs:
            for deal in gerar_deals(config, limite=None):
                total += 1
                if not deal.coerente():
                    divergencias += 1
                    self.stdout.write(
                        f"INCOERENTE\tconfig={config.pk}\t"
                        f"produto={getattr(deal.produto, 'pk', '?')}\t"
                        f"vitrine={deal.preco_vitrine}\tbeneficio={deal.beneficio_rs}\t"
                        f"final={deal.preco_final}"
                    )
                if deal.prova not in PROVAS_VALIDAS:
                    divergencias += 1
                    self.stdout.write(
                        f"PROVA_INVALIDA\tconfig={config.pk}\tprova={deal.prova}")
                if deal.cupom_perene and deal.preco_final < deal.preco_vitrine:
                    # Perene pode existir e pode ser publicado; o que não pode é
                    # creditar profundidade. `_valor_real` mede pela vitrine.
                    if "no fundo do histórico" in " ".join(deal.motivos):
                        divergencias += 1
                        self.stdout.write(
                            f"PERENE_CREDITADO\tconfig={config.pk}\t"
                            f"produto={getattr(deal.produto, 'pk', '?')}")
        self.stdout.write(f"avaliados: {total}")
        self.stdout.write(f"divergencias: {divergencias}")

    def _auditar_nicho(self, configs):
        from apps.scrapers.deals import _casa_algum_termo, _termos

        divergencias = 0
        total = 0
        for config in configs:
            negativos = _termos(getattr(config, "termos_negativos", ""))
            positivos = _termos(getattr(config, "termo_busca", ""))
            macro = str(getattr(config, "macro_categoria", "") or "").strip()
            for deal in gerar_deals(config, limite=None):
                total += 1
                produto = deal.produto
                if negativos and _casa_algum_termo(produto, negativos):
                    divergencias += 1
                    self.stdout.write(
                        f"TERMO_NEGATIVO\tconfig={config.pk}\t"
                        f"produto={getattr(produto, 'pk', '?')}")
                if (positivos or macro) and not (
                    (macro and str(getattr(produto, "macro_categoria", "") or "")
                     .strip() == macro)
                    or (positivos and _casa_algum_termo(produto, positivos))
                ):
                    divergencias += 1
                    self.stdout.write(
                        f"FORA_DO_NICHO\tconfig={config.pk}\t"
                        f"produto={getattr(produto, 'pk', '?')}")
                minimo = getattr(config, "preco_min", None)
                maximo = getattr(config, "preco_max", None)
                if minimo is not None and deal.preco_final < float(minimo):
                    divergencias += 1
                    self.stdout.write(f"ABAIXO_DA_FAIXA\tconfig={config.pk}")
                if maximo is not None and deal.preco_final > float(maximo):
                    divergencias += 1
                    self.stdout.write(f"ACIMA_DA_FAIXA\tconfig={config.pk}")
        self.stdout.write(f"avaliados: {total}")
        self.stdout.write(f"divergencias: {divergencias}")

    def _shadow(self, dias):
        from datetime import timedelta

        from django.utils import timezone

        from apps.scrapers.models import EventoOperacional

        desde = timezone.now() - timedelta(days=max(1, int(dias)))
        eventos = EventoOperacional.objects.filter(
            pipeline="selecao", evento="deal_shadow", criado_em__gte=desde,
        ).order_by("-criado_em")
        total = divergiu = 0
        self.stdout.write("quando\tconfig\tlegado\tdeal\tdivergiu\tscore\tprova")
        for evento in eventos[:200]:
            contexto = evento.contexto if isinstance(evento.contexto, dict) else {}
            total += 1
            divergiu += 1 if contexto.get("divergiu") else 0
            self.stdout.write(
                f"{evento.criado_em:%Y-%m-%d %H:%M}\t{contexto.get('config_id')}\t"
                f"{contexto.get('vencedor_legado')}\t{contexto.get('vencedor_deal')}\t"
                f"{contexto.get('divergiu')}\t{contexto.get('score_deal')}\t"
                f"{contexto.get('prova')}"
            )
        self.stdout.write(f"amostras: {eventos.count()}")
        self.stdout.write(f"divergencias na amostra listada: {divergiu}/{total}")
