"""Pré-visualiza ou publica um único canário de cupom no grupo de homologação."""
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.accounts.tenant import system_job


MARCADORES_DE_TESTE = ("teste", "test", "smoke", "dev")


def _pares_prontos_da_regra(config, user, *, piso):
    """Escolhe da fonte de verdade produto + cupom + link, sem cupom solto.

    A projeção ``CupomDisponibilidade`` pode ficar um ciclo atrás da lane de
    links. O canário não pode dizer que não há oferta se o par já tem produto,
    foto e URL afiliada verificados pelo próprio funil de entrega.
    """
    from apps.scrapers.coupon_products import mapa_relacoes_prontas
    from apps.scrapers.coupon_rules import codigo_publicavel, cupons_visiveis_q
    from apps.scrapers.maintenance import cupons_frescos_q
    from apps.scrapers.models import CupomNormalizado
    from apps.scrapers.ofertas import _desconto_efetivo_do_item_cupom

    agora = timezone.now()
    query = CupomNormalizado.objects.filter(estado="ativo").filter(
        cupons_visiveis_q(user),
        Q(inicio__isnull=True) | Q(inicio__lte=agora),
        cupons_frescos_q(agora=agora),
    )
    if config.marketplace:
        query = query.filter(marketplace=config.marketplace)
    if config.macro_categoria:
        query = query.filter(
            produtos__status="confirmado",
            produtos__produto__macro_categoria=config.macro_categoria,
        ).distinct()
    cupons = list(query.order_by("-ultima_observacao", "-pk"))
    _preparadas, prontas = mapa_relacoes_prontas(user, cupons)
    for cupom in cupons:
        if not codigo_publicavel(cupom):
            continue
        for relacao in prontas.get(cupom.pk, []):
            if (config.macro_categoria
                    and relacao.produto.macro_categoria != config.macro_categoria):
                continue
            desconto = _desconto_efetivo_do_item_cupom({
                "produto": relacao.produto, "relacao": relacao,
            })
            if desconto >= piso:
                return SimpleNamespace(
                    kind="coupon", obj=cupom, score=0,
                    reasons=["par produto-cupom com link verificado"],
                ), relacao, desconto
    return None, None, None


class Command(BaseCommand):
    help = "Pré-visualiza um cupom pronto; --enviar publica um único canário no grupo de teste."

    def add_arguments(self, parser):
        parser.add_argument("--config", type=int, required=True)
        parser.add_argument("--username", default="lules")
        parser.add_argument(
            "--enviar", action="store_true",
            help="Publica o canário selecionado. Sem esta flag, não altera nada.",
        )

    @system_job
    def handle(self, *args, **options):
        from apps.scrapers.content_ranking import _coupon_candidates
        from apps.scrapers.coupon_products import relacoes_prontas_para_envio
        from apps.scrapers.ofertas import (
            _desconto_efetivo_do_item_cupom,
            _piso_desconto_cupom,
            enviar_cupom,
        )
        from apps.scrapers.models import ConfiguracaoEnvio

        user = get_user_model().objects.filter(username=options["username"]).first()
        if not user:
            raise CommandError("Usuário não encontrado.")
        config = ConfiguracaoEnvio.objects.filter(
            pk=options["config"], owner=user,
        ).first()
        if not config:
            raise CommandError("Regra não encontrada para este usuário.")
        destino = str(config.grupo_nome or "").casefold()
        if not any(marcador in destino for marcador in MARCADORES_DE_TESTE):
            raise CommandError("Canário só pode ser publicado em grupo de teste.")

        # O canário é uma prova específica do fluxo de cupom. A camada Deal pode
        # escolher um Deal já medido (e corretamente não cair no ranking legado),
        # mas isso não deve impedir a homologação da fila de cupons prontos.
        candidatos = _coupon_candidates(config, limit=20)
        piso = _piso_desconto_cupom(config)
        candidato = relacao = desconto = None
        for possivel in candidatos:
            relacoes = relacoes_prontas_para_envio(possivel.obj, user)
            if config.macro_categoria:
                relacoes = [
                    item for item in relacoes
                    if item.produto.macro_categoria == config.macro_categoria
                ]
            relacao_pronta = next(iter(relacoes), None)
            if not relacao_pronta:
                continue
            desconto_efetivo = _desconto_efetivo_do_item_cupom({
                "produto": relacao_pronta.produto, "relacao": relacao_pronta,
            })
            if desconto_efetivo >= piso:
                candidato, relacao, desconto = (
                    possivel, relacao_pronta, desconto_efetivo,
                )
                break
        if candidato is None:
            candidato, relacao, desconto = _pares_prontos_da_regra(
                config, user, piso=piso,
            )
        if candidato is None:
            raise CommandError(
                "Nenhum cupom elegível manteve produto, foto, link e desconto "
                f"efetivo de ao menos {piso:.0f}%.")
        cupom = candidato.obj

        self.stdout.write(
            "CANÁRIO "
            f"cupom={cupom.pk} produto={relacao.produto.pk} "
            f"nome={relacao.produto.nome[:100]!r} "
            f"de={relacao.preco_atual} por={relacao.preco_final} "
            f"desconto={desconto:.0f}% destino={config.grupo_nome!r}"
        )
        if not options["enviar"]:
            self.stdout.write(self.style.NOTICE("Prévia somente; nada foi enviado."))
            return

        resultado = enviar_cupom(
            cupom, config.grupo_id, canal=config.canal, usuario=user,
            destino_nome=config.grupo_nome, configuracao=config,
            score=candidato.score, motivos_score=candidato.reasons,
            relacao_id=relacao.pk,
        )
        if not resultado.get("sucesso"):
            raise CommandError(resultado.get("motivo") or "Canário não foi publicado.")
        self.stdout.write(self.style.SUCCESS(
            f"Canário enviado; operation_id={resultado.get('operation_id', '')}."
        ))
