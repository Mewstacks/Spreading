from datetime import timedelta
from django.db.models import Exists, OuterRef, Q
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


def freshness_points(observed, *, agora=None, max_age_hours=COUPON_MAX_AGE_HOURS,
                     max_points=10.0):
    """Pontos de recência: 0 depois de `max_age_hours`. Mesma janela do TTL (48h)."""
    agora = agora or timezone.now()
    if not observed:
        return 0.0
    hours = max(0.0, (agora - observed).total_seconds() / 3600)
    return max(
        0.0,
        max_points * (1.0 - min(hours, float(max_age_hours)) / float(max_age_hours)),
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
    from apps.scrapers.coupon_rules import CODIGOS_NAO_PUBLICAVEIS
    from apps.scrapers.models import (
        CupomFonteObservacao, CupomNormalizado, Produto, ProdutoCupom,
    )
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
    placeholder_q = Q()
    for codigo in CODIGOS_NAO_PUBLICAVEIS:
        placeholder_q |= Q(codigo__iexact=codigo)
    invalid_ids = list(
        CupomNormalizado.objects.filter(estado="ativo").filter(
            placeholder_q,
        ).values_list("pk", flat=True)
    )
    invalid_codes = CupomNormalizado.objects.filter(pk__in=invalid_ids).update(
        estado="invalido", confianca="baixa",
    )
    if invalid_ids:
        CupomFonteObservacao.objects.filter(cupom_id__in=invalid_ids).update(
            outcome="invalid", reason_code="invalid_coupon_code",
        )
    ProdutoCupom.objects.filter(cupom__estado="expirado").exclude(
        status="expirado").update(status="expirado")
    return {
        "products": stale_products, "coupons": expired_coupons,
        "invalid_codes": invalid_codes,
    }


def purgar_eventos_cupons_antigos(dias=90):
    from apps.scrapers.models import CupomDisponibilidadeEvento
    cutoff = timezone.now() - timedelta(days=dias)
    apagados, _ = CupomDisponibilidadeEvento.objects.filter(
        created_at__lt=cutoff,
    ).delete()
    return apagados


def diagnosticar_alertas_pipeline_cupons(*, agora=None):
    """Contagens acionáveis do SLA do funil, sem alterar estado de negócio."""
    from django.conf import settings
    from django.db.models import F
    from apps.scrapers.models import (
        CupomDisponibilidade, CupomPreparacao, ExecucaoIngestao,
    )

    agora = agora or timezone.now()
    cutoff_20m = agora - timedelta(minutes=20)
    cutoff_browser = agora - timedelta(minutes=60)
    link_cutoff = agora - timedelta(hours=max(
        1, int(getattr(settings, "COUPON_AFFILIATE_LINK_TTL_HOURS", 168) or 168),
    ))
    active = Q(cupom__estado="ativo") & cupons_frescos_q(
        agora=agora, prefix="cupom__",
    )
    projections = CupomDisponibilidade.objects.filter(active)
    # Estados que dependem de uma nova evidência externa ou de ação explícita do
    # dono da conta não são uma fila travada. Eles continuam visíveis no relatório
    # de abundância, mas não podem acordar a operação a cada 20 minutos. Em
    # produção, sessão ausente e comunidade sem corroboração respondiam por quase
    # seis mil linhas e escondiam o backlog que o worker realmente pode resolver.
    actionable = (
        projections.exclude(stage__in=("ready", "discarded"))
        .exclude(category="no_session")
        .exclude(reason_code__in=(
            "community_uncorroborated",
            # A tag pertence à conta do usuário. O worker não consegue criá-la e
            # repetir a projeção não muda o veredito; contar essas linhas como fila
            # travada gerava exatamente 128 alarmes permanentes em produção nas
            # duas contas sem tag, embora a conta `lules` estivesse saudável.
            "amazon_tag_missing",
        ))
    )
    counts = {
        # Só conta projeção que AINDA DEVE MUDAR. `updated_at` é auto_now, então um
        # cupom que chegou a `ready` (ou foi descartado com veredito) para de ser
        # escrito — e sem esta exclusão ele aparecia como "parado" 20 minutos depois,
        # justamente por estar saudável. Em produção isso inflava o número para a
        # casa dos dez mil e o alerta disparava em todo ciclo, escondendo os três
        # contadores vizinhos, que são reais. Alerta que sempre toca não é alerta.
        "projection_stale": actionable.filter(
            updated_at__lt=cutoff_20m,
        ).count(),
        "code_not_ready_20m": actionable.filter(
            use_mode="code_notice", cupom__primeira_observacao__lt=cutoff_20m,
        ).count(),
        "prepared_verified_not_ready_20m": actionable.filter(
            use_mode="product_activation",
            cupom__produtos__status="confirmado",
            cupom__produtos__links_usuarios__usuario_id=F("usuario_id"),
            cupom__produtos__links_usuarios__verificado_ok=True,
            cupom__produtos__links_usuarios__verificado_em__gte=link_cutoff,
            updated_at__lt=cutoff_20m,
        ).distinct().count(),
        # Um preparo antigo só é backlog se ainda bloquear uma projeção de
        # ATIVAÇÃO que o worker consegue promover. O mesmo cupom também pode ter
        # projeções de aviso de código já prontas/descartadas, ou projeções de
        # ativação retidas por sessão da conta. Contar o CupomPreparacao sozinho
        # acusava 25 itens em produção embora nenhuma entrega acionável dependesse
        # deles (projection_stale=0).
        "browser_wait_over_60m": CupomPreparacao.objects.filter(
            status="pendente", reason_code="capacity_deferred",
            verificado_em__lt=cutoff_browser, cupom__estado="ativo",
        ).filter(
            cupons_frescos_q(agora=agora, prefix="cupom__"),
        ).annotate(
            bloqueia_entrega=Exists(
                projections.filter(
                    cupom_id=OuterRef("cupom_id"),
                    use_mode="product_activation",
                ).exclude(
                    stage__in=("ready", "discarded"),
                ).exclude(category="no_session")
            ),
        ).filter(bloqueia_entrega=True).count(),
    }
    # Fontes que NUNCA podem se declarar completas por construção (vitrine curada,
    # prévia de canal). Cobrar completude delas transforma este contador em ruído
    # permanente — o mesmo defeito que fazia `projection_stale` disparar sempre.
    from apps.scrapers.sources.registry import SOURCES

    parciais = {
        slug for slug, adaptador in SOURCES.items()
        if not getattr(adaptador, "inventario_completo", True)
    }
    incomplete_sources = 0
    source_ids = ExecucaoIngestao.objects.exclude(
        fonte__slug__in=parciais,
    ).values_list("fonte_id", flat=True).distinct()
    for source_id in source_ids:
        recent = list(ExecucaoIngestao.objects.filter(
            fonte_id=source_id,
        ).order_by("-iniciada_em")[:2])
        if len(recent) < 2 or not any(
                "complete" in (run.metricas or {}) for run in recent):
            continue
        if all(
            run.status not in {"ok", "empty"}
            or run.health_status not in {"healthy", "healthy_empty", "ok"}
            or not bool((run.metricas or {}).get("complete"))
            for run in recent
        ):
            incomplete_sources += 1
    counts["source_without_complete_two_cycles"] = incomplete_sources
    return counts


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
