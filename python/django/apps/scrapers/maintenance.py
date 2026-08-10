from datetime import timedelta
from django.db.models import Q
from django.utils import timezone


PRODUCT_MAX_AGE_HOURS = 48
COUPON_MAX_AGE_HOURS = 48


def produtos_frescos_q(*, agora=None, max_age_hours=PRODUCT_MAX_AGE_HOURS,
                       prefix=""):
    """Predicado de leitura que não depende da faxina ter rodado.

    `expire_stale` materializa o estado para diagnóstico, mas telas e envios não
    podem voltar a oferecer preço velho se o worker de coleta estiver desligado.
    """
    agora = agora or timezone.now()
    cutoff = agora - timedelta(hours=max_age_hours)
    return (
        Q(**{f"{prefix}ultima_observacao__gte": cutoff})
        & (
            Q(**{f"{prefix}valido_ate__isnull": True})
            | Q(**{f"{prefix}valido_ate__gte": agora})
        )
    )


def cupons_frescos_q(*, agora=None, max_age_hours=COUPON_MAX_AGE_HOURS,
                     prefix=""):
    """Cupom com validade explícita vale até ela; sem validade exige recência.

    Cupons privados são exceção: foram cadastrados pelo usuário e permanecem
    ativos até ele desativá-los ou informar uma validade.
    """
    agora = agora or timezone.now()
    cutoff = agora - timedelta(hours=max_age_hours)
    return (
        Q(**{f"{prefix}validade__gte": agora})
        | (
            Q(**{f"{prefix}validade__isnull": True})
            & (
                Q(**{f"{prefix}fonte__slug": "manual-private"})
                | Q(**{f"{prefix}ultima_observacao__gte": cutoff})
            )
        )
    )


def expire_stale(max_age_hours=PRODUCT_MAX_AGE_HOURS):
    """Expiração gradual; não remove linhas nem histórico."""
    from apps.scrapers.models import Produto, CupomNormalizado, ProdutoCupom
    now = timezone.now()
    cutoff = now - timedelta(hours=max_age_hours)
    stale_products = Produto.objects.filter(
        ultima_observacao__lt=cutoff, estado="ativo"
    ).update(estado="stale", falha_verificacao="Fonte sem confirmar a oferta há 48h")
    expired_coupons = CupomNormalizado.objects.filter(estado="ativo").filter(
        Q(validade__lt=now)
        | (
            Q(validade__isnull=True, ultima_observacao__lt=cutoff)
            & ~Q(fonte__slug="manual-private")
        )
    ).update(estado="expirado")
    ProdutoCupom.objects.filter(cupom__estado="expirado").exclude(
        status="expirado").update(status="expirado")
    return {"products": stale_products, "coupons": expired_coupons}


def purgar_eventos_antigos(dias=30):
    """Apaga EventoOperacional velho. Sem isto a tabela de log cresce para sempre.

    30 dias porque o relatório de saúde olha no máximo 7 e o resto serve para
    comparar com o mês anterior; guardar mais só paga armazenamento para responder
    pergunta que ninguém faz. Erros que importam viram correção no código, não
    arquivo histórico.
    """
    from apps.scrapers.models import EventoOperacional
    cutoff = timezone.now() - timedelta(days=dias)
    apagados, _ = EventoOperacional.objects.filter(criado_em__lt=cutoff).delete()
    return apagados


def reconciliar_publicacoes_orfas(max_age_minutes=30):
    """Fecha Publicacao presas em 'pendente' — o worker morreu no meio do envio.

    A linha nasce 'pendente' antes do trabalho e todo erro previsto já a marca
    'falhou'; sobra o processo morto (deploy/crash) entre o create e o desfecho, que
    nenhum except captura. Um envio real leva no máximo dezenas de segundos (Playwright
    do ML), então 'pendente' há 30min não é um envio em curso — é uma órfã.

    O desfecho depende de haver quem retome: só a fila v2, ligada para a organização
    da linha, tem consumidor. Sem ela, a órfã é fechada como sempre foi, em vez de
    ficar 'pendente' para sempre reciclando eventos a cada ciclo.
    """
    from django.conf import settings
    from apps.accounts.feature_flags import send_pipeline_v2_enabled
    from apps.scrapers.models import Publicacao, PublicacaoEvento
    from apps.scrapers.send_pipeline import QUEUE_STATES, TERMINALS
    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    reconciled = 0
    # A decisão é por organização, mas a flag é resolvida a partir do usuário. Um lote
    # órfão costuma pertencer a poucos donos; sem este cache o ciclo faria duas queries
    # por linha só para reler a mesma resposta.
    rollout_por_usuario = {}

    def _tem_consumidor(publication):
        # Reagendar só é honesto quando existe consumidor: quem drena a fila v2 é
        # `process_queued_publications`, e ele só roda com o rollout ligado PARA A
        # ORGANIZAÇÃO da linha. Com a flag no default (e no estado de rollback), uma
        # órfã devolvida para 'transport_queued' continuava 'pendente' para sempre,
        # voltava a casar com este filtro a cada ciclo e gravava um evento novo a
        # cada 30 min, sem teto. Sem consumidor, o desfecho é terminal — como era
        # antes da fila v2 existir.
        if publication.transport_state not in QUEUE_STATES:
            return False
        if publication.usuario_id not in rollout_por_usuario:
            rollout_por_usuario[publication.usuario_id] = send_pipeline_v2_enabled(
                publication.usuario,
            )
        return rollout_por_usuario[publication.usuario_id]

    for publication in Publicacao.objects.select_related("usuario").filter(
            status="pendente", criada_em__lt=cutoff).order_by("pk")[:1000]:
        if publication.stage in {"transport_started", "confirmation_pending"}:
            publication.status = "incerto"
            publication.stage = "uncertain"
            publication.transport_state = "uncertain_after_restart"
            publication.next_retry_at = None
            publication.erro = (
                "Transporte iniciado antes do reinício; reenvio automático bloqueado."
            )
            reason = "restart_after_transport"
        elif not _tem_consumidor(publication):
            publication.status = "falhou"
            publication.stage = "cancelled"
            publication.transport_state = "no_queue_consumer"
            publication.next_retry_at = None
            publication.erro = (
                "Envio interrompido antes de concluir e sem fila que possa retomá-lo "
                "(worker reiniciado)."
            )
            reason = "restart_without_queue_consumer"
        elif publication.attempt_count < int(settings.SEND_MAX_ATTEMPTS):
            # A tentativa perdida precisa ser contada aqui: sem isso o teto de
            # SEND_MAX_ATTEMPTS nunca era alcançado por este caminho e o ramo
            # `retry_exhausted` abaixo era inalcançável.
            publication.attempt_count += 1
            publication.stage = "transport_queued"
            publication.transport_state = "retry_after_restart"
            publication.next_retry_at = timezone.now()
            publication.erro = "Processamento interrompido antes do transporte; retomada agendada."
            reason = "restart_before_transport"
        else:
            publication.status = "falhou"
            publication.stage = "cancelled"
            publication.transport_state = "retry_exhausted"
            publication.next_retry_at = None
            publication.erro = "Tentativas esgotadas após reinício do worker."
            reason = "restart_retry_exhausted"
        campos = [
            "attempt_count", "status", "stage", "transport_state", "next_retry_at",
            "erro",
        ]
        if publication.stage in TERMINALS:
            # Terminal não volta ao transporte: a imagem enfileirada (até 16 MiB por
            # linha, replicada no lote) deixa de ter uso e não pode ficar residente
            # no Postgres. O caminho feliz já limpa em send_pipeline; este era o
            # único desfecho que guardava os bytes indefinidamente.
            publication.queued_media = None
            publication.queued_media_mime = ""
            campos += ["queued_media", "queued_media_mime"]
        publication.save(update_fields=campos)
        PublicacaoEvento.objects.create(
            organization_id=publication.organization_id, publicacao=publication,
            stage=publication.stage, reason_code=reason,
            safe_detail=publication.erro[:255],
        )
        reconciled += 1
    return reconciled


def reconciliar_execucoes_ingestao_orfas(max_age_hours=2):
    """Fecha execuções técnicas abandonadas por crash/deploy do worker."""
    from apps.scrapers.models import ExecucaoIngestao

    agora = timezone.now()
    cutoff = agora - timedelta(hours=max_age_hours)
    return ExecucaoIngestao.objects.filter(
        status="running", iniciada_em__lt=cutoff,
    ).update(
        status="error",
        finalizada_em=agora,
        erro_publico="Coleta interrompida antes de concluir; dados anteriores preservados.",
    )
