"""Pré-visualiza ou publica um único canário de cupom no grupo de homologação."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.tenant import system_job


MARCADORES_DE_TESTE = ("teste", "test", "smoke", "dev")


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
        candidato = next(iter(candidatos), None)
        if candidato is None:
            raise CommandError("Nenhum cupom pronto e elegível para esta regra.")
        cupom = candidato.obj
        relacao = next(iter(relacoes_prontas_para_envio(cupom, user)), None)
        if not relacao:
            raise CommandError("O cupom selecionado perdeu produto, foto ou link verificado.")
        desconto = _desconto_efetivo_do_item_cupom({
            "produto": relacao.produto, "relacao": relacao,
        })
        piso = _piso_desconto_cupom(config)
        if desconto < piso:
            raise CommandError(
                f"Cupom selecionado tem desconto efetivo de {desconto:.0f}% "
                f"(mínimo editorial: {piso:.0f}%)."
            )

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
        )
        if not resultado.get("sucesso"):
            raise CommandError(resultado.get("motivo") or "Canário não foi publicado.")
        self.stdout.write(self.style.SUCCESS(
            f"Canário enviado; operation_id={resultado.get('operation_id', '')}."
        ))
