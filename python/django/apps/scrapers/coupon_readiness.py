"""Projeção durável e tenant-aware do funil de cupons.

Esta camada não torna um cupom elegível. Ela explica, com categorias estáveis, o
resultado dos gates já usados pelo preparo e pelo envio.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from contextlib import contextmanager

from django.db import connection, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.accounts.feature_flags import feature_decision
from apps.accounts.models import organization_for_user
from apps.scrapers.coupon_products import (
    ERRO_CAPACIDADE_BROWSER, ERRO_CONTAINER_EMPTY_PROVEN,
    ERRO_CONTAINER_FETCH_FAILED, ERRO_MINIMUM_NOT_MET, ERRO_SESSAO_ML,
    mapa_relacoes_prontas,
)
from apps.scrapers.coupon_rules import (
    codigo_publicavel, coupon_mode_enabled, cupom_publicavel, forca_evidencia,
    listagem_publica_ml, regras_do_cupom,
)
from apps.scrapers.coupon_links import coupon_link_verified_and_fresh
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


@contextmanager
def _session_statement_timeout(value):
    """Relaxa o timeout da sessão e restaura. `value` é literal PG ('0', '10min')."""
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        previous = cursor.fetchone()[0]
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, false)", [value],
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, false)", [previous],
            )


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
    if getattr(cupom, "audience_scope", "public") == "organization":
        organization = organization_for_user(usuario)
        if not organization or (
                cupom.organization_id and cupom.organization_id != organization.pk):
            return _resultado("discarded", "rejected", "audience_restricted",
                              "Cupom restrito a outra organização.")
    regras = regras_do_cupom(cupom)
    if regras.get("valor_desconto") in (None, "", 0):
        return _resultado("discarded", "invalid", "missing_discount",
                          "A fonte não comprovou o valor do desconto.")
    # Cupom de comunidade é alegação de terceiro — inclusive a leitura por IA de
    # uma mensagem de canal. Ele soma evidência quando confirma o que uma fonte
    # oficial já publicou; sozinho, não manda ninguém anunciar um código.
    from apps.scrapers.coupon_rules import aguarda_corroboracao_oficial

    if aguarda_corroboracao_oficial(cupom):
        return _resultado(
            "collected", "waiting", "community_uncorroborated",
            "Cupom visto só em fonte de comunidade; aguardando confirmação oficial.",
        )
    return None


def _tem_link_de_aviso(usuario, marketplace) -> bool:
    """O usuário já tem ALGUM link de cupom aprovado e fresco nesta loja?

    O aviso de cupons publica uma lista de códigos sob um link só. Basta que exista
    um link válido do usuário nessa loja para a mensagem poder sair; qual cupom o
    originou é indiferente para quem lê e para a atribuição.

    A consulta é barata e limitada: só linhas já aprovadas, ordenadas pela
    verificação mais recente, e para no primeiro que passa no TTL.
    """
    from apps.scrapers.coupon_links import coupon_link_verified_and_fresh

    candidatos = LinkAfiliadoCupomUsuario.objects.filter(
        usuario=usuario, cupom__marketplace=marketplace, verificado_ok=True,
    ).exclude(link_afiliado="").order_by("-verificado_em")[:5]
    return any(coupon_link_verified_and_fresh(linha) for linha in candidatos)


def _codigo(cupom, usuario, conexao):
    codigo = codigo_publicavel(cupom)
    if not codigo:
        return None
    if not coupon_mode_enabled(cupom, use_mode="code_notice"):
        return _resultado(
            "discarded", "rejected", "feature_disabled",
            "Cupons de código estão pausados para este marketplace.",
        )
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
        # Código de agregador nasce sem URL de produto (nunca o redirect deles).
        # Destino comissionado é a home/ASIN com `?tag=` — montado na hora do envio.
        if str(cupom.link or "").strip() and not _url_publica(cupom.link):
            return _resultado("discarded", "invalid", "invalid_destination",
                              "Destino público inválido.")
        return _resultado("ready")
    if marketplace == "mercadolivre":
        link = LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, cupom=cupom,
        ).exclude(link_afiliado="").first()
        if coupon_link_verified_and_fresh(link):
            return _resultado("ready")
        # A mensagem de aviso leva UM link para a lista inteira de códigos — é o
        # formato ("Ative em algum produto do link") e é o que `enviar_aviso_cupons`
        # monta: resolve o link do PRIMEIRO cupom do lote e anuncia todos os códigos
        # sob ele. Exigir link próprio por cupom cobrava do Link Builder um trabalho
        # que a mensagem nunca usa — e cada um custa uma vaga do único Chromium.
        #
        # Era o gargalo medido em produção: `code_not_ready_20m` na casa das
        # centenas e `browser_wait_over_60m` acumulando, enquanto o envio precisava
        # de um link só. O custo cai de (cupons × usuários) para (usuários).
        #
        # A atribuição não muda: o clique sai pelo link do usuário e a comissão
        # segue o clique, não o código digitado no checkout — que é exatamente o que
        # já acontece com os outros códigos do mesmo aviso.
        if _tem_link_de_aviso(usuario, marketplace):
            return _resultado("ready")
        if link and link.verificado_ok is None:
            return _resultado(
                "waiting_link", "no_link", "affiliate_link_unverified",
                "Link afiliado gerado; aguardando verificação do destino.",
                link.proxima_tentativa,
            )
        if link and link.verificado_ok is False:
            return _resultado(
                "waiting_link", "no_link", "affiliate_link_rejected",
                link.verificacao_motivo or "O destino afiliado não foi aprovado.",
                link.proxima_tentativa,
            )
        if link and link.verificado_em:
            return _resultado(
                "waiting_link", "no_link", "affiliate_link_expired",
                "O link afiliado precisa ser reverificado.", link.proxima_tentativa,
            )
        if not conexao["ok"]:
            return _resultado("waiting_link", "no_session", conexao["reason"],
                              conexao["detail"])
        if not link:
            return _resultado("waiting_link", "no_link", "affiliate_link_pending",
                              "Código válido; link afiliado ainda não foi preparado.")
    if marketplace == "awin":
        return _resultado("ready" if _url_publica(cupom.link) else "waiting_link",
                          "" if _url_publica(cupom.link) else "no_link",
                          "" if _url_publica(cupom.link) else "affiliate_link_pending")
    return _resultado("waiting_link", "no_link", "affiliate_destination_missing",
                      "O destino afiliado ainda não está disponível.")


def _ativacao(cupom, usuario, preparadas, prontas, preparos, conexao):
    marketplace = str(cupom.marketplace or "").lower()
    if (marketplace != "mercadolivre"
            and not coupon_mode_enabled(cupom, use_mode="product_activation")):
        return _resultado(
            "discarded", "rejected", "feature_disabled",
            "Cupons de ativação estão pausados para este marketplace.",
        )
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
    if not cupom_publicavel(cupom, usuario=usuario):
        return _resultado("discarded", "invalid", "activation_evidence_incomplete",
                          "Evidência de ativação incompleta.")

    if marketplace == "amazon":
        # Página oficial já provou promo + ASINs + preço. Link é `?tag=`, sem
        # Chromium e sem mapa ProdutoCupom (esse mapa é do ML).
        return _resultado("ready")

    if marketplace == "mercadolivre":
        # Mesma ideia da Amazon: a listagem pública JÁ é o escopo. Carimbar o
        # rastreio de um link ML já verificado nesta conta evita Chromium×campanha
        # (fila de ~2.6k em preparation_pending). Destino continua o container da
        # campanha — não um aviso genérico de outro cupom (MELIPROMO).
        from apps.scrapers.coupon_links import gerar_link_afiliado_listagem_ml
        if gerar_link_afiliado_listagem_ml(cupom, usuario):
            return _resultado("ready")

    if marketplace in {"shopee", "awin"}:
        if cupom.integracao and not (
                cupom.integracao.habilitada and cupom.integracao.status == "conectada"):
            return _resultado("eligible", "no_session", "integration_disconnected",
                              "Integração de afiliados desconectada.")
        return _resultado(
            "ready" if _url_publica(cupom.link) else "waiting_link",
            "" if _url_publica(cupom.link) else "no_link",
            "" if _url_publica(cupom.link) else "affiliate_link_pending",
        )

    if cupom.pk in prontas:
        return _resultado("ready")
    if marketplace == "mercadolivre" and not conexao["ok"]:
        return _resultado(
            "eligible", "no_session", conexao["reason"],
            f"Container público comprovado. {conexao['detail']}",
        )
    if cupom.pk in preparadas:
        relation_ids = [relation.pk for relation in preparadas[cupom.pk]]
        from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario
        links = list(LinkAfiliadoProdutoCupomUsuario.objects.filter(
            usuario=usuario, relacao_id__in=relation_ids,
        ).only(
            "link_afiliado", "url_canonica", "verificado_ok", "verificado_em",
            "estado", "proxima_tentativa",
        ))
        if not links:
            # Compatibilidade da janela de migração: diagnostica cache antigo por
            # produto, mas só o promove a pronto através do gate estrito de
            # ``mapa_relacoes_prontas``.
            product_ids = [relation.produto_id for relation in preparadas[cupom.pk]]
            links = list(LinkAfiliadoUsuario.objects.filter(
                usuario=usuario, produto_id__in=product_ids,
            ).only(
                "link_afiliado", "url_canonica", "verificado_ok", "verificado_em",
                "estado", "proxima_tentativa",
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
        if any(link.verificado_ok is True and not coupon_link_verified_and_fresh(link)
               for link in links):
            return _resultado(
                "waiting_link", "no_link", "affiliate_link_expired",
                "O link afiliado precisa ser reverificado.",
            )
        return _resultado("prepared", "no_link", "affiliate_link_pending",
                          "Produtos e preços comprovados; geração do link ainda não iniciada.")
    preparo = preparos.get(cupom.pk)
    if not preparo:
        return _resultado("eligible", "waiting", "preparation_pending",
                          "Aguardando preparo de produtos e preços.")
    prep_reason = preparo.reason_code or preparo.erro
    if prep_reason in {"capacity_deferred", ERRO_CAPACIDADE_BROWSER}:
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
        if prep_reason in {"ml_session_required_for_preparation", ERRO_SESSAO_ML}:
            return _resultado(
                "eligible", "no_session", "ml_catalog_session_expired",
                "A conexão do Mercado Livre expirou; reconecte para preparar este cupom.",
                preparo.proxima_tentativa,
            )
        if prep_reason == ERRO_CONTAINER_FETCH_FAILED:
            return _resultado(
                "eligible", "operational_failure", "container_fetch_failed",
                "A lista do container não respondeu; nova tentativa será feita.",
                preparo.proxima_tentativa,
            )
        return _resultado("eligible", "operational_failure", "preparation_failed",
                          "Falha operacional no preparo; nova tentativa será feita.",
                          preparo.proxima_tentativa)
    if preparo.status == "vazio":
        if prep_reason == ERRO_CONTAINER_EMPTY_PROVEN:
            return _resultado(
                "eligible", "waiting", "container_empty_proven",
                "O container respondeu sem produtos aplicáveis nesta tentativa.",
                preparo.proxima_tentativa,
            )
        if prep_reason == ERRO_MINIMUM_NOT_MET:
            return _resultado(
                "eligible", "waiting", "minimum_not_met",
                "Os itens encontrados exigem completar o valor mínimo no carrinho.",
                preparo.proxima_tentativa,
            )
        if prep_reason == "price_claim_unproven":
            return _resultado(
                "eligible", "waiting", "price_claim_unproven",
                "A associação existe, mas o preço anunciado não foi comprovado.",
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
    with _session_statement_timeout("0"):
        return _projetar_disponibilidade_cupons(usuario, organization, channel)


def _projetar_disponibilidade_cupons(usuario, organization, channel):
    agora = timezone.now()
    cupons = list(
        CupomNormalizado.objects.select_related("fonte", "programa", "integracao")
        .filter(
            Q(owner=usuario)
            | Q(owner__isnull=True, audience_scope="public")
            | Q(owner__isnull=True, audience_scope="organization",
                organization=organization)
        )
        .filter(estado="ativo")
        .filter(cupons_frescos_q(agora=agora))
        .annotate(_prio=Case(
            When(marketplace="amazon", then=Value(0)),
            When(marketplace="mercadolivre", then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ))
        .order_by("_prio", "-ultima_observacao")[:5000]
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
                    marketplace=cupom.marketplace,
                    source=getattr(cupom.fonte, "slug", ""), use_mode=use_mode,
                    evidence_strength=(forca_evidencia(cupom)
                                       if use_mode == "product_activation" else "official_code"),
                    attempt=getattr(preparos.get(cupom.pk), "tentativas", 0),
                    duration_ms=getattr(preparos.get(cupom.pk), "duracao_ms", 0),
                    source_run_id=getattr(preparos.get(cupom.pk), "source_run_id", ""),
                )
            else:
                # ``updated_at`` representa a última reconciliação, não apenas a
                # última mudança de estado. Sem este toque, um cupom estável em
                # ready disparava falsamente o alerta de projeção atrasada.
                projection.save(update_fields=["updated_at"])
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


def marcar_ausentes_execucao_saudavel(fonte, seen_ids, *, reconcile_catalog=False):
    """Marca ``not_found`` após inventário saudável/completo.

    Fontes declaradas como inventário autoritativo também retiram a linha do
    catálogo ativo. Execuções parciais nunca chamam esta função.
    """
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
        if reconcile_catalog and absent_coupons:
            CupomNormalizado.objects.filter(
                pk__in=[coupon.pk for coupon in absent_coupons], estado="ativo",
            ).update(estado="inativo")
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
                marketplace=projection.cupom.marketplace,
                source=fonte.slug,
                use_mode=projection.use_mode,
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
