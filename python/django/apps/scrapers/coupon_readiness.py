"""Projeção durável e tenant-aware do funil de cupons.

Esta camada não torna um cupom elegível. Ela explica, com categorias estáveis, o
resultado dos gates já usados pelo preparo e pelo envio.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.feature_flags import feature_decision
from apps.accounts.models import organization_for_user
from apps.scrapers.coupon_products import (
    ERRO_CAPACIDADE_BROWSER, ERRO_CONTAINER_EMPTY_PROVEN,
    ERRO_CONTAINER_FETCH_FAILED, ERRO_MINIMUM_NOT_MET, ERRO_SESSAO_ML,
    mapa_relacoes_prontas,
)
from apps.scrapers.coupon_rules import (
    codigo_publicavel, cupom_publicavel, listagem_publica_ml, regras_do_cupom,
)
from apps.scrapers.maintenance import cupons_frescos_q
from apps.scrapers.models import (
    CupomDisponibilidade, CupomDisponibilidadeEvento, CupomFonteObservacao,
    CupomNormalizado,
    CupomPreparacao, LinkAfiliadoCupomUsuario, LinkAfiliadoUsuario,
)


def _url_publica(url):
    try:
        partes = urlsplit(str(url or ""))
    except ValueError:
        return False
    return partes.scheme in {"http", "https"} and bool(partes.netloc)


def _resultado(stage, category="", reason="", detail="", retry_at=None):
    return {
        "stage": stage, "category": category, "reason_code": reason,
        "safe_detail": detail[:255], "retry_at": retry_at,
    }


def conexao_ml(usuario):
    """Veredito ÚNICO da conexão ML para uma projeção inteira.

    Antes cada cupom lia ``MercadoLivreSession.status`` cru. Isso era uma SEGUNDA
    fonte de verdade ao lado de ``conexoes.estado_ml``, que é a que as telas
    renderizam — e as duas discordam por construção: a coluna guarda o veredito
    bruto da última sonda, enquanto ``estado_ml`` aplica a política de acúmulo
    (``PROBE_FALHAS_PARA_DESCONECTAR``) porque o ML responde 302→login a IP de
    datacenter sem que a sessão tenha morrido. O resultado era o sintoma relatado:
    tela de conexão verde e a esteira inteira parada em "aguardando conexão".

    Resolvido UMA vez por projeção, não por cupom: a leitura antiga era também um
    N+1 (uma consulta de sessão por cupom, ~3.100 por ciclo por usuário).
    """
    from apps.scrapers.conexoes import estado_ml, estado_ml_linkbuilder

    site = estado_ml(usuario)
    if not site.conectado:
        ausente = site.detalhe == "sem_sessao"
        return {
            "ok": False,
            "reason": "ml_session_missing" if ausente else "ml_session_expired",
            "detail": site.motivo or "Reconecte a sessão do Mercado Livre.",
        }
    # Só `login_required` bloqueia: `stale`/`unknown` significam "ninguém abriu o
    # portal ainda", e a própria geração do link revalida em segundos. Tratá-los
    # como desconexão prenderia o funil num veredito que nada promove sozinho.
    if estado_ml_linkbuilder(usuario).detalhe == "login_required":
        return {
            "ok": False,
            "reason": "ml_linkbuilder_login_required",
            "detail": "Reconecte o Link Builder do Mercado Livre.",
        }
    return {"ok": True, "reason": "", "detail": ""}


def _preflight(cupom, usuario):
    agora = timezone.now()
    if cupom.validade and cupom.validade < agora:
        return _resultado("discarded", "rejected", "expired", "Cupom expirado.")
    if cupom.inicio and cupom.inicio > agora:
        return _resultado("collected", "waiting", "not_started", "Vigência ainda não iniciada.")
    if cupom.estado != "ativo":
        categoria = "rejected" if cupom.estado in {"expirado", "inativo"} else "invalid"
        return _resultado("discarded", categoria, f"state_{cupom.estado}",
                          "Cupom fora do catálogo ativo.")
    if cupom.owner_id and cupom.owner_id != usuario.pk:
        return _resultado("discarded", "rejected", "tenant_scope",
                          "Cupom pertence a outra organização.")
    regras = regras_do_cupom(cupom)
    if regras.get("valor_desconto") in (None, "", 0):
        return _resultado("discarded", "invalid", "missing_discount",
                          "A fonte não comprovou o valor do desconto.")
    return None


def _codigo(cupom, usuario, conexao):
    codigo = codigo_publicavel(cupom)
    if not codigo:
        return None
    if cupom.programa and not (
            cupom.programa.habilitado and cupom.programa.status_vinculo == "joined"
            and cupom.programa.link_status == "online"):
        return _resultado("discarded", "rejected", "affiliate_program_unavailable",
                          "Programa de afiliados indisponível.")
    if cupom.integracao and not (
            cupom.integracao.habilitada and cupom.integracao.status == "conectada"):
        return _resultado("eligible", "no_session", "integration_disconnected",
                          "Integração de afiliados desconectada.")

    marketplace = str(cupom.marketplace or "").lower()
    if marketplace == "amazon":
        tag = str(getattr(getattr(usuario, "perfil", None), "afiliado_tag_amazon", "") or "")
        if not tag:
            return _resultado("waiting_link", "no_link", "amazon_tag_missing",
                              "Cadastre a tag Amazon para preparar o link.")
        if not _url_publica(cupom.link):
            return _resultado("discarded", "invalid", "invalid_destination",
                              "Destino público inválido.")
        return _resultado("ready")
    if marketplace == "mercadolivre":
        if not conexao["ok"]:
            return _resultado("waiting_link", "no_session", conexao["reason"],
                              conexao["detail"])
        link = LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, cupom=cupom, afiliado_ok=True,
        ).exclude(link_afiliado="").first()
        if not link:
            return _resultado("waiting_link", "no_link", "affiliate_link_pending",
                              "Código válido; link afiliado ainda não foi preparado.")
        return _resultado("ready")
    if marketplace == "awin":
        return _resultado("ready" if _url_publica(cupom.link) else "waiting_link",
                          "" if _url_publica(cupom.link) else "no_link",
                          "" if _url_publica(cupom.link) else "affiliate_link_pending")
    return _resultado("waiting_link", "no_link", "affiliate_destination_missing",
                      "O destino afiliado ainda não está disponível.")


def _ativacao(cupom, usuario, preparadas, prontas, preparos, conexao):
    marketplace = str(cupom.marketplace or "").lower()
    if marketplace == "amazon":
        tag = str(getattr(getattr(usuario, "perfil", None), "afiliado_tag_amazon", "") or "")
        if not tag:
            return _resultado(
                "waiting_link", "no_link", "amazon_tag_missing",
                "Catálogo público coletado; cadastre a tag Amazon para preparar o link.",
            )
    if marketplace == "mercadolivre":
        enabled, flag_reason = feature_decision("ML_CUPONS_ATIVACAO_ENABLED", usuario)
        if not enabled:
            return _resultado("discarded", "rejected", f"feature_{flag_reason}",
                              "Cupons de ativação não estão liberados para esta organização.")
        if not str(cupom.external_id or "").startswith("campanha:"):
            return _resultado("discarded", "invalid", "campaign_missing",
                              "Campanha de ativação não identificada.")
        if not listagem_publica_ml(cupom):
            return _resultado("discarded", "invalid", "public_container_missing",
                              "Container público não comprovado.")
        if not conexao["ok"]:
            return _resultado(
                "eligible", "no_session", conexao["reason"],
                f"Container público comprovado. {conexao['detail']}",
            )
    if not cupom_publicavel(cupom, usuario=usuario):
        return _resultado("discarded", "invalid", "activation_evidence_incomplete",
                          "Evidência de ativação incompleta.")

    if cupom.pk in prontas:
        return _resultado("ready")
    if cupom.pk in preparadas:
        product_ids = [relation.produto_id for relation in preparadas[cupom.pk]]
        links = list(LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=product_ids,
        ).only(
            "link_afiliado", "verificado_ok", "estado", "proxima_tentativa",
        ))
        if any(link.link_afiliado and link.verificado_ok is None for link in links):
            return _resultado(
                "waiting_link", "no_link", "link_verification_pending",
                "Link afiliado gerado; aguardando verificação do destino.",
            )
        if links and all(
            link.verificado_ok is False
            or link.estado in {"nao_afiliavel", "erro"}
            for link in links
        ):
            retry_at = min(
                (link.proxima_tentativa for link in links if link.proxima_tentativa),
                default=None,
            )
            return _resultado(
                "waiting_link", "no_link", "affiliate_link_rejected",
                "Os links tentados não tiveram destino afiliado aprovado.", retry_at,
            )
        return _resultado("prepared", "no_link", "affiliate_link_pending",
                          "Produtos e preços comprovados; geração do link ainda não iniciada.")
    preparo = preparos.get(cupom.pk)
    if not preparo:
        return _resultado("eligible", "waiting", "preparation_pending",
                          "Aguardando preparo de produtos e preços.")
    if preparo.erro == ERRO_CAPACIDADE_BROWSER:
        # FILA, não avaria: o preparo nem começou porque o único Chromium da
        # máquina estava com outra tarefa. Antes isto caía no `except Exception` do
        # preparo e chegava aqui como `preparation_failed` — 188 cupons em produção
        # com cara de defeito e 30 min de castigo por uma espera de capacidade.
        return _resultado(
            "eligible", "waiting", "browser_capacity_deferred",
            "Aguardando capacidade de navegador; o próximo ciclo retoma.",
            preparo.proxima_tentativa,
        )
    if preparo.status == "erro":
        # Sessão do catálogo caída não é "falha no preparo": nada foi observado
        # sobre este cupom, e a ação que destrava é reconectar o Mercado Livre.
        # Sem separar os dois, a tela dizia "nova tentativa será feita" para um
        # funil que não voltaria sozinho — e o dono não sabia o que fazer.
        if preparo.erro == ERRO_SESSAO_ML:
            return _resultado(
                "eligible", "no_session", "ml_catalog_session_expired",
                "A conexão do Mercado Livre expirou; reconecte para preparar este cupom.",
                preparo.proxima_tentativa,
            )
        if preparo.erro == ERRO_CONTAINER_FETCH_FAILED:
            return _resultado(
                "eligible", "operational_failure", "container_fetch_failed",
                "A lista do container não respondeu; nova tentativa será feita.",
                preparo.proxima_tentativa,
            )
        return _resultado("eligible", "operational_failure", "preparation_failed",
                          "Falha operacional no preparo; nova tentativa será feita.",
                          preparo.proxima_tentativa)
    if preparo.status == "vazio":
        if preparo.erro == ERRO_CONTAINER_EMPTY_PROVEN:
            return _resultado(
                "eligible", "waiting", "container_empty_proven",
                "O container respondeu sem produtos aplicáveis nesta tentativa.",
                preparo.proxima_tentativa,
            )
        if preparo.erro == ERRO_MINIMUM_NOT_MET:
            return _resultado(
                "eligible", "waiting", "minimum_not_met",
                "Os itens encontrados exigem completar o valor mínimo no carrinho.",
                preparo.proxima_tentativa,
            )
        return _resultado("eligible", "waiting", "product_match_pending",
                          "Nenhum produto comprovado nesta tentativa.",
                          preparo.proxima_tentativa)
    return _resultado("eligible", "waiting", "preparation_pending",
                      "Aguardando preparo de produtos e preços.")


def projetar_disponibilidade_cupons(usuario, channel="whatsapp"):
    """Atualiza o funil do usuário e devolve contagens por estágio/motivo."""
    organization = organization_for_user(usuario)
    if organization is None:
        return {"stages": {}, "reasons": {}, "total": 0}
    agora = timezone.now()
    cupons = list(
        CupomNormalizado.objects.select_related("fonte", "programa", "integracao")
        .filter(Q(owner__isnull=True) | Q(owner=usuario))
        .filter(estado="ativo")
        .filter(cupons_frescos_q(agora=agora))
        .order_by("-ultima_observacao")[:5000]
    )
    ativacoes = [c for c in cupons if not codigo_publicavel(c)]
    observations = CupomFonteObservacao.objects.filter(
        cupom_id__in=[c.pk for c in cupons], outcome="accepted",
        health_status__in=("healthy", "ok", "healthy_empty"),
    ).order_by("canonical_key", "precedence", "-observed_at", "pk")
    not_found_coupon_ids = set(CupomFonteObservacao.objects.filter(
        cupom_id__in=[c.pk for c in cupons], outcome="not_found",
        health_status__in=("healthy", "ok", "healthy_empty"),
    ).values_list("cupom_id", flat=True))
    winner_by_key = {}
    loser_coupon_ids = set()
    for observation in observations:
        winner = winner_by_key.setdefault(
            observation.canonical_key, observation.cupom_id,
        )
        if winner != observation.cupom_id:
            loser_coupon_ids.add(observation.cupom_id)
    preparadas, prontas = mapa_relacoes_prontas(usuario, ativacoes)
    preparos = {}
    for preparo in CupomPreparacao.objects.filter(
            cupom_id__in=[c.pk for c in ativacoes],
    ).order_by("cupom_id", "-verificado_em"):
        # O primeiro registro é o mais novo; não deixe um retry antigo substituir
        # o diagnóstico corrente na compreensão do dicionário.
        preparos.setdefault(preparo.cupom_id, preparo)
    conexao = conexao_ml(usuario)
    stages, reasons = {}, {}
    for cupom in cupons:
        use_mode = "code_notice" if codigo_publicavel(cupom) else "product_activation"
        outcome = (
            _resultado(
                "discarded", "not_found", "not_found_healthy_run",
                "Item ausente na última coleta completa e saudável.",
            ) if cupom.pk in not_found_coupon_ids else _resultado(
                "discarded", "rejected", "lower_precedence_duplicate",
                "Outra fonte saudável de maior precedência publicou este cupom.",
            ) if cupom.pk in loser_coupon_ids else _preflight(cupom, usuario)
        )
        if outcome is None:
            outcome = (
                _codigo(cupom, usuario, conexao)
                if use_mode == "code_notice"
                else _ativacao(cupom, usuario, preparadas, prontas, preparos, conexao)
            )
        with transaction.atomic():
            projection, created = CupomDisponibilidade.objects.select_for_update().get_or_create(
                organization=organization, usuario=usuario, cupom=cupom,
                channel=channel, use_mode=use_mode, defaults=outcome,
            )
            changed = created or any(
                getattr(projection, field) != outcome[field]
                for field in ("stage", "category", "reason_code", "safe_detail", "retry_at")
            )
            previous = "" if created else projection.stage
            if changed:
                for field, value in outcome.items():
                    setattr(projection, field, value)
                projection.save()
                CupomDisponibilidadeEvento.objects.create(
                    organization=organization, disponibilidade=projection,
                    from_stage=previous, to_stage=projection.stage,
                    category=projection.category, reason_code=projection.reason_code,
                )
        stages[outcome["stage"]] = stages.get(outcome["stage"], 0) + 1
        reason = outcome["reason_code"] or "none"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {"stages": stages, "reasons": reasons, "total": len(cupons)}


def disponibilidade_resumo(usuario, channel="whatsapp"):
    organization = organization_for_user(usuario)
    if organization is None:
        return {"stages": {}, "reasons": {}, "total": 0}
    rows = CupomDisponibilidade.objects.filter(
        organization=organization, usuario=usuario, channel=channel,
        cupom__estado="ativo",
    ).filter(cupons_frescos_q(prefix="cupom__")).values_list(
        "stage", "reason_code",
    )
    stages, reasons, total = {}, {}, 0
    for stage, reason in rows:
        total += 1
        stages[stage] = stages.get(stage, 0) + 1
        reasons[reason or "none"] = reasons.get(reason or "none", 0) + 1
    return {"stages": stages, "reasons": reasons, "total": total}


def marcar_ausentes_execucao_saudavel(fonte, seen_ids):
    """Marca ``not_found`` apenas após inventário explicitamente saudável/completo."""
    agora = timezone.now()
    with transaction.atomic():
        from apps.scrapers.sources.persistence import record_coupon_observation

        absent_coupons = list(CupomNormalizado.objects.filter(
            fonte=fonte,
        ).exclude(external_id__in=set(seen_ids)))
        verdicts = {}
        # A observação da fonte é a autoridade entre ciclos. Sem isto, a projeção
        # executada logo depois da coleta enxergaria a linha ainda fresca no banco e
        # apagaria ``not_found`` imediatamente. Uma futura persistência do mesmo
        # item atualiza esta observação de volta para ``accepted``.
        for coupon in absent_coupons:
            if (coupon.validade and coupon.validade < agora) \
                    or coupon.estado == "expirado":
                verdict = (
                    "discarded", "rejected", "expired", "Cupom expirado.",
                    "rejected",
                )
            elif coupon.estado != "ativo":
                category = (
                    "rejected" if coupon.estado == "inativo" else "invalid"
                )
                verdict = (
                    "discarded", category, f"state_{coupon.estado}",
                    "Cupom fora do catálogo ativo.",
                    "rejected" if category == "rejected" else "invalid",
                )
            else:
                verdict = (
                    "discarded", "not_found", "not_found_healthy_run",
                    "Item ausente na última coleta completa e saudável.",
                    "not_found",
                )
            verdicts[coupon.pk] = verdict
            record_coupon_observation(
                coupon, source=fonte, health_status="healthy",
                outcome=verdict[4], reason_code=verdict[2],
            )
        projections = list(
            CupomDisponibilidade.objects.select_for_update()
            .filter(cupom_id__in=[coupon.pk for coupon in absent_coupons])
        )
        changed = []
        events = []
        for projection in projections:
            stage, category, reason, detail, _observation = verdicts[projection.cupom_id]
            if (
                projection.stage == stage
                and projection.category == category
                and projection.reason_code == reason
                and projection.safe_detail == detail
                and projection.retry_at is None
            ):
                continue
            previous = projection.stage
            projection.stage = stage
            projection.category = category
            projection.reason_code = reason
            projection.safe_detail = detail
            projection.retry_at = None
            projection.updated_at = agora
            changed.append(projection)
            events.append(CupomDisponibilidadeEvento(
                organization_id=projection.organization_id,
                disponibilidade=projection,
                from_stage=previous,
                to_stage=stage,
                category=category,
                reason_code=reason,
            ))
        if not changed:
            return 0
        CupomDisponibilidade.objects.bulk_update(
            changed,
            [
                "stage", "category", "reason_code", "safe_detail", "retry_at",
                "updated_at",
            ],
        )
        CupomDisponibilidadeEvento.objects.bulk_create(events)
        return len(changed)
