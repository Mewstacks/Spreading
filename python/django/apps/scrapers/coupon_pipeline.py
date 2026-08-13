"""Pipeline único de coleta, preparo, afiliação e diagnóstico de cupons."""
from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, IntegracaoAfiliado, LinkAfiliadoUsuario,
)

logger = logging.getLogger(__name__)


def _metricas_vazias():
    return {
        "encontrados": 0,
        "persistidos": 0,
        "preparados": 0,
        "vinculados": 0,
        "links_gerados": 0,
        "links_verificados": 0,
        "links_reprovados": 0,
        "links_transitorios": 0,
        "links_falhos": 0,
        "prontos": 0,
        "falhos": 0,
        "fontes": {},
    }


def _fonte(resultado, slug, *, status, encontrados=0, persistidos=0, motivo=""):
    resultado["encontrados"] += int(encontrados or 0)
    resultado["persistidos"] += int(persistidos or 0)
    if status == "error":
        resultado["falhos"] += 1
    resultado["fontes"][slug] = {
        "status": status,
        "encontrados": int(encontrados or 0),
        "persistidos": int(persistidos or 0),
        "motivo": str(motivo or "")[:300],
    }


def _usuarios_ativos(usuarios=None):
    if usuarios is None:
        return list(get_user_model().objects.filter(is_active=True))
    ids = [getattr(user, "id", user) for user in usuarios]
    return list(get_user_model().objects.filter(is_active=True, id__in=ids))


def _materializar_ausencias_saudaveis(slug, payload, rows, *, owner=None):
    """Projeta ausência somente depois de persistir o inventário correspondente."""
    metrics = payload.get("metrics") or {}
    health = str(payload.get("health") or "")
    if (
        owner is not None
        or not bool(metrics.get("complete"))
        or health not in {"ok", "healthy", "healthy_empty"}
    ):
        return 0
    from apps.scrapers.coupon_readiness import marcar_ausentes_execucao_saudavel

    fonte = FonteIngestao.objects.get(slug=slug)
    seen_coupon_ids = [row.external_id for row in rows if row.kind == "coupon"]
    if slug == "amazon-public-coupons":
        return marcar_ausentes_execucao_saudavel(
            fonte, seen_coupon_ids, reconcile_catalog=True,
        )
    return marcar_ausentes_execucao_saudavel(fonte, seen_coupon_ids)


def _coletar_adaptador(slug, resultado, *, owner=None, items=("coupons",), **kwargs):
    """Executa uma fonte isolada; vazio/bloqueado/desabilitado nunca apaga dados."""
    from apps.scrapers.sources import run_source
    from apps.scrapers.sources.persistence import persist_items

    try:
        payload = run_source(slug, **kwargs)
        status = payload.get("status", "error")
        rows = []
        for kind in items:
            rows.extend(payload.get(kind, []))
        if rows:
            health_kwargs = (
                {"source_health": payload["health"]} if payload.get("health") else {}
            )
            persistidos = (
                persist_items(rows, owner=owner, **health_kwargs)
                if owner is not None else persist_items(
                    rows, **health_kwargs,
                )
            )
        else:
            persistidos = {"offers": 0, "coupons": 0}
        if "coupons" in items:
            # Só um inventário público, saudável e explicitamente completo pode
            # dizer que um item anterior não foi encontrado. Coleta parcial,
            # CAPTCHA, schema ilegível, erro ou zero implícito preservam a projeção.
            _materializar_ausencias_saudaveis(
                slug, payload, rows, owner=owner,
            )
        total_persistido = persistidos["offers"] + persistidos["coupons"]
        _fonte(
            resultado, slug, status=status, encontrados=len(rows),
            persistidos=total_persistido,
            motivo=payload.get("error") or (
                "Fonte desabilitada ou não configurada."
                if status == "disabled" else
                "Coleta parcial/degradada; catálogo anterior preservado."
                if status == "degraded" else
                "Coleta vazia; catálogo anterior preservado."
                if status == "empty" else
                # Contenção de capacidade não é "já está rodando": a fonte NÃO
                # começou, está na fila do único navegador da máquina.
                "Sem navegador disponível agora; a coleta será retomada."
                if payload.get("reason_code") == "capacity_deferred" else
                "Coleta já está em execução."
                if status == "running" else
                "Circuit breaker ativo; catálogo anterior preservado."
                if status == "blocked" else ""
            ),
        )
        return payload
    except Exception as exc:
        logger.error(
            "Fonte de cupons %s falhou isoladamente (%s)", slug, type(exc).__name__,
        )
        public_error = "Falha operacional na fonte; catálogo anterior preservado."
        _fonte(resultado, slug, status="error", motivo=public_error)
        return {
            "status": "error", "offers": [], "coupons": [],
            "error": public_error, "cause": type(exc).__name__,
        }


def coletar_feed_licenciado(resultado=None):
    """Habilita o adaptador quando o secret existe e executa-o pelo pipeline."""
    resultado = resultado if resultado is not None else _metricas_vazias()
    if not getattr(settings, "AFFILIATE_FEED_URL", ""):
        _fonte(
            resultado, "licensed-affiliate-feed", status="skipped",
            motivo="Feed licenciado não configurado.",
        )
        return {"offers": 0, "coupons": 0}
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="licensed-affiliate-feed",
        defaults={
            "marketplace": "multiloja",
            "nome": "Feed licenciado de afiliados",
            "habilitada": True,
        },
    )
    if not fonte.habilitada:
        fonte.habilitada = True
        fonte.status = "degraded"
        fonte.erro_publico = ""
        fonte.save(update_fields=("habilitada", "status", "erro_publico"))
    before_offers = resultado["persistidos"]
    payload = _coletar_adaptador(
        "licensed-affiliate-feed", resultado, items=("offers", "coupons"),
    )
    # Compatibilidade do chamador legado: contagens separadas vêm do payload.
    # O pipeline principal usa a soma normalizada acima.
    found_offers = len(payload.get("offers", []))
    found_coupons = len(payload.get("coupons", []))
    persisted = max(resultado["persistidos"] - before_offers, 0)
    if found_offers + found_coupons == persisted:
        return {"offers": found_offers, "coupons": found_coupons}
    return {
        "offers": min(found_offers, persisted),
        "coupons": max(0, persisted - min(found_offers, persisted)),
    }


def coletar_cupons(*, usuarios=None, incluir_awin=True):
    """Sincroniza somente fontes habilitadas e integrações configuradas."""
    resultado = _metricas_vazias()
    usuarios = _usuarios_ativos(usuarios)

    _coletar_adaptador("ml-cupons-afiliados", resultado)

    if getattr(settings, "AMAZON_GENERAL_COUPONS_URL", ""):
        _coletar_adaptador("amazon-general-coupons", resultado)
    else:
        _fonte(
            resultado, "amazon-general-coupons", status="skipped",
            motivo="Fonte oficial/licenciada de códigos gerais não configurada.",
        )

    # A fonte é pública e existe independentemente de alguma conta já possuir tag.
    # O vínculo de afiliado continua por usuário na etapa de link/envio.
    payload = _coletar_adaptador("amazon-public-coupons", resultado, items=())
    rows = payload.get("offers", []) + payload.get("coupons", [])
    if rows:
        from apps.scrapers.sources.persistence import persist_items
        try:
            health_kwargs = (
                {"source_health": payload["health"]} if payload.get("health") else {}
            )
            counts = persist_items(rows, owner=None, **health_kwargs)
            persistidos = counts["offers"] + counts["coupons"]
            # A Amazon é persistida fora de ``_coletar_adaptador`` porque ofertas
            # e cupons compartilham o mesmo snapshot. Compare ausências somente
            # agora, com todas as linhas do inventário já gravadas.
            _materializar_ausencias_saudaveis(
                "amazon-public-coupons", payload, rows, owner=None,
            )
            info = resultado["fontes"]["amazon-public-coupons"]
            info["encontrados"] = len(rows)
            info["persistidos"] = persistidos
            info["metricas"] = payload.get("metrics", {})
            resultado["encontrados"] += len(rows)
            resultado["persistidos"] += persistidos
        except Exception:
            logger.exception("Persistência do catálogo público Amazon falhou")
            resultado["falhos"] += 1
            resultado["fontes"]["amazon-public-coupons"].update(
                status="error",
                motivo="Falha ao persistir catálogo público; dados anteriores preservados.",
            )

    coletar_feed_licenciado(resultado)

    if not incluir_awin or not getattr(settings, "AWIN_INTEGRATION_ENABLED", False):
        _fonte(
            resultado, "awin", status="skipped",
            motivo="Integração Awin desabilitada.",
        )
    else:
        from apps.scrapers.awin import sincronizar_integracao

        agora = timezone.now()
        integracoes = IntegracaoAfiliado.objects.filter(
            owner__in=usuarios, provedor="awin", habilitada=True,
            status__in=("conectada", "degradada"),
        ).filter(
            Q(proxima_sincronizacao__isnull=True)
            | Q(proxima_sincronizacao__lte=agora)
        ).select_related("owner")
        total = falhas = executadas = 0
        for integracao in integracoes:
            executadas += 1
            try:
                sync = sincronizar_integracao(integracao)
                total += int(sync.get("coupons", 0) or 0)
            except Exception as exc:
                falhas += 1
                logger.warning(
                    "Integração Awin %s falhou (%s)",
                    integracao.pk, type(exc).__name__,
                )
        _fonte(
            resultado, "awin",
            status="error" if falhas else (
                "ok" if total else "empty" if executadas else "skipped"
            ),
            encontrados=total, persistidos=total,
            motivo=(
                f"{falhas} integração(ões) falharam isoladamente."
                if falhas else
                "A coleta não retornou cupons; catálogo anterior preservado."
                if executadas and not total else
                "Nenhuma integração Awin habilitada estava vencida."
                if not total else ""
            ),
        )

    privados = CupomNormalizado.objects.filter(
        owner__in=usuarios, fonte__slug="manual-private", estado="ativo",
    ).count()
    _fonte(
        resultado, "manual-private", status="ok" if privados else "skipped",
        encontrados=privados, persistidos=0,
        motivo="" if privados else "Nenhum cupom privado ativo.",
    )
    return resultado


def _cupons_visiveis(usuario):
    from apps.scrapers.maintenance import cupons_frescos_q
    from apps.accounts.models import organization_for_user

    organization = organization_for_user(usuario)
    return CupomNormalizado.objects.select_related("fonte").filter(
        Q(owner=usuario)
        | Q(owner__isnull=True, audience_scope="public")
        | Q(owner__isnull=True, audience_scope="organization",
            organization=organization),
        estado="ativo",
    ).filter(cupons_frescos_q())


def afiliar_cupons_de_codigo(usuario, cupons, *, limite=8):
    """Pré-gera o link afiliado dos cupons de CÓDIGO do Mercado Livre.

    Sem esta etapa o funil tinha um impasse fechado: ``coupon_readiness._codigo``
    exige um ``LinkAfiliadoCupomUsuario`` verificado para marcar o cupom como
    ``ready``, e a ÚNICA rotina que gravava essa linha era ``enviar_cupom`` — que a
    tela só oferece quando o cupom já está ``ready``. Resultado observado em
    produção: todo cupom de código do ML aparecia listado e permanentemente
    "indisponível / aguardando link", sem nada no sistema capaz de promovê-lo.

    O lote é pequeno de propósito: cada item abre o Link Builder (~5s) e divide o
    Chromium com a raspagem e com os logins interativos. Uma sessão derrubada
    interrompe o lote em vez de gastar as tentativas restantes contra a mesma
    recusa.
    """
    from apps.scrapers.coupon_rules import codigo_publicavel
    from apps.scrapers.models import LinkAfiliadoCupomUsuario
    from apps.scrapers.ofertas import resolver_link_afiliado_cupom

    candidatos = [
        cupom for cupom in cupons
        if str(cupom.marketplace or "").lower() == "mercadolivre"
        and codigo_publicavel(cupom)
    ]
    if not candidatos:
        return {"gerados": 0, "falhas": 0, "pendentes": 0}
    from apps.scrapers.monitor_conexao import ml_conectado
    from apps.scrapers.coupon_links import coupon_link_verified_and_fresh
    caches = {
        link.cupom_id: link for link in LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, cupom_id__in=[c.pk for c in candidatos],
        )
    }
    ja_tem = {
        coupon_id for coupon_id, link in caches.items()
        if coupon_link_verified_and_fresh(link)
    }
    pendentes = [c for c in candidatos if c.pk not in ja_tem]
    total_pendentes = len(pendentes)
    agora = timezone.now()
    pendentes = [
        c for c in pendentes
        if not caches.get(c.pk) or not caches[c.pk].proxima_tentativa
        or caches[c.pk].proxima_tentativa <= agora
    ]

    # Caches existentes são reverificados primeiro sem navegador. Apenas a geração
    # de um link novo depende da sessão ML atual.
    cache_link_ids = {
        c.pk for c in pendentes if getattr(caches.get(c.pk), "link_afiliado", "")
    }
    com_cache = [c for c in pendentes if c.pk in cache_link_ids]
    sem_cache = [c for c in pendentes if c.pk not in cache_link_ids]
    if not ml_conectado(usuario):
        pendentes = com_cache
    else:
        pendentes = com_cache + sem_cache

    gerados = falhas = 0
    for cupom in pendentes[:max(0, limite)]:
        try:
            resolucao = resolver_link_afiliado_cupom(cupom, usuario)
        except Exception:
            falhas += 1
            logger.exception("Link afiliado do cupom %s falhou para %s",
                             cupom.pk, usuario.pk)
            continue
        if resolucao.get("sucesso"):
            gerados += 1
            continue
        falhas += 1
        if resolucao.get("precisa_login_ml"):
            # A sessão caiu: as próximas tentativas dariam a mesma recusa e
            # custariam um Chromium cada. O ciclo seguinte retoma.
            logger.warning(
                "Afiliação de cupons de código interrompida: sessão ML expirada "
                "(usuário %s).", usuario.pk,
            )
            break
    return {
        "gerados": gerados, "falhas": falhas,
        "pendentes": max(0, total_pendentes - gerados),
    }


def _peso_do_cupom(cupom) -> int:
    """Posição na fila de afiliação (menor = antes). Ver EvidenceStrength."""
    from apps.scrapers.coupon_rules import (
        FORCA_EVIDENCIA_ORDEM, codigo_publicavel, forca_evidencia,
    )

    if codigo_publicavel(cupom):
        return 0
    if str(cupom.marketplace or "").lower() != "mercadolivre":
        # Amazon/Awin não usam container: a prova de associação é a própria fonte
        # oficial, então entram logo depois dos códigos.
        return 1
    return 2 + FORCA_EVIDENCIA_ORDEM.get(forca_evidencia(cupom), 3)


def afiliar_cupons(usuario, *, limite=80, faixa=None, limite_codigo=8):
    """Gera e verifica links dos produtos realmente ligados por ProdutoCupom."""
    from apps.scrapers.coupon_products import (
        ids_cupons_prontos, mapa_relacoes_prontas,
    )
    from apps.scrapers.coupon_rules import coupon_mode_enabled, cupom_publicavel
    from apps.scrapers.marketplaces.registry import get_marketplace

    cupons = [
        cupom for cupom in _cupons_visiveis(usuario)
        if cupom_publicavel(cupom, usuario=usuario)
        and coupon_mode_enabled(cupom)
    ]
    preparadas, _prontas = mapa_relacoes_prontas(usuario, cupons)
    from apps.scrapers.coupon_links import coupon_link_verified_and_fresh
    from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario
    agora = timezone.now()
    relation_rows = {
        row.relacao_id: row
        for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
            usuario=usuario,
            relacao_id__in=[r.pk for rows in preparadas.values() for r in rows],
        )
    }
    # PRIORIDADE DA FILA: cupom oficial de código, depois campanha com container
    # publicado, depois segmentação estruturada, e só então o candidato sintético.
    # Sem ordem, o `limite` era consumido pela ordem arbitrária do dicionário e as
    # campanhas com associação comprovada podiam nunca chegar à geração de link.
    ordem_dos_cupons = {
        cupom.pk: _peso_do_cupom(cupom) for cupom in cupons
    }
    produtos = {}
    relacao_por_produto = {}
    for cupom_id in sorted(preparadas, key=lambda pk: ordem_dos_cupons.get(pk, 9)):
        for relacao in preparadas[cupom_id]:
            row = relation_rows.get(relacao.pk)
            if coupon_link_verified_and_fresh(row):
                continue
            if row and (
                row.estado == "nao_afiliavel"
                or (row.proxima_tentativa and row.proxima_tentativa > agora)
            ):
                continue
            # Um produto por ciclo: o cache intermediário legado é por produto.
            # A relação seguinte avança no próximo ciclo sem sobrescrever duas
            # campanhas antes que a primeira seja materializada.
            if relacao.produto_id not in produtos:
                produtos[relacao.produto_id] = relacao.produto
                relacao_por_produto[relacao.produto_id] = relacao

    metricas = {
        "vinculados": len(produtos),
        "links_gerados": 0,
        "links_verificados": 0,
        "links_reprovados": 0,
        "links_transitorios": 0,
        "links_falhos": 0,
        "prontos": 0,
        "por_marketplace": {},
    }
    # Antes do early return de `produtos`: cupom de código não tem produto
    # vinculado por construção, então ficava fora de todo o caminho de afiliação.
    codigo = afiliar_cupons_de_codigo(usuario, cupons, limite=limite_codigo)
    metricas["links_gerados"] += codigo["gerados"]
    metricas["links_falhos"] += codigo["falhas"]
    metricas["cupons_codigo_pendentes"] = codigo["pendentes"]
    if not produtos:
        metricas["prontos"] = len(ids_cupons_prontos(usuario, cupons))
        return metricas

    candidatos = list(produtos.values())[:limite]

    grupos = defaultdict(list)
    for produto in candidatos:
        grupos[produto.marketplace or "mercadolivre"].append(produto)

    for slug, itens in grupos.items():
        target_relation_ids = [relacao_por_produto[p.id].pk for p in itens]
        before = {
            row.relacao_id for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
                usuario=usuario, relacao_id__in=target_relation_ids,
            ) if coupon_link_verified_and_fresh(row)
        }
        gerados = falhas = 0
        detalhe = {"candidatos": len(itens)}
        try:
            marketplace = get_marketplace(slug)
            activation_keys = {
                p.id: relacao_por_produto[p.id].activation_key for p in itens
            }
            try:
                gerados, falhas = marketplace.prefetch_links(
                    itens, usuario=usuario, faixa=faixa,
                    activation_keys=activation_keys,
                )
            except TypeError as exc:
                # Adaptadores de plugin anteriores ao contrato de campanha seguem
                # funcionando enquanto migram a assinatura.
                if "activation_keys" not in str(exc):
                    raise
                gerados, falhas = marketplace.prefetch_links(
                    itens, usuario=usuario, faixa=faixa,
                )
            # Cada loja verifica os PRÓPRIOS links (contrato em Marketplace):
            # chamar o verificador do ML aqui reprovava item Amazon com o motivo
            # do Mercado Livre.
            verificacao = marketplace.verificar_links_pendentes(
                usuario, limite=len(itens), produto_ids=[p.id for p in itens],
            )
            metricas["links_reprovados"] += verificacao.get("reprovados", 0)
            metricas["links_transitorios"] += verificacao.get("transitorios", 0)
            detalhe.update(verificacao)
            # O gerador/verificador existente usa um cache intermediário por
            # produto. Copiamos somente quando a URL isca corresponde à campanha
            # desta relação; o estado final passa a ser muitos-para-muitos.
            all_rows = {
                row.produto_id: row for row in LinkAfiliadoUsuario.objects.filter(
                    usuario=usuario, produto_id__in=[p.id for p in itens],
                ).exclude(link_afiliado="")
            }
            for product in itens:
                relation = relacao_por_produto[product.id]
                row = all_rows.get(product.id)
                activation = str(relation.activation_key or "")
                campaign_matches = bool(row) and not (
                    activation and f"coupon_campaign_id={activation}"
                    not in str(row.url_isca or "")
                )
                if not row or row.verificado_ok is not True or not campaign_matches:
                    current = relation_rows.get(relation.pk)
                    reason = (
                        "O link gerado ainda não foi verificado."
                        if row and row.verificado_ok is None else
                        row.verificacao_motivo
                        if row and row.verificado_ok is False else
                        "O link gerado não corresponde à campanha desta relação."
                        if row and not campaign_matches else
                        "Aguardando geração do link afiliado."
                    )
                    LinkAfiliadoProdutoCupomUsuario.objects.update_or_create(
                        usuario=usuario, relacao=relation,
                        defaults={
                            "url_isca": getattr(row, "url_isca", "") or "",
                            "link_afiliado": getattr(row, "link_afiliado", "") or "",
                            "estado": "pendente",
                            "verificado_ok": getattr(row, "verificado_ok", None),
                            "verificado_em": getattr(row, "verificado_em", None),
                            "url_canonica": "",
                            "verificacao_motivo": str(reason or "")[:300],
                            "tentativas": (getattr(current, "tentativas", 0) or 0) + 1,
                            "ultima_tentativa": agora,
                            "proxima_tentativa": agora + timezone.timedelta(minutes=15),
                        },
                    )
                    continue
                LinkAfiliadoProdutoCupomUsuario.objects.update_or_create(
                    usuario=usuario, relacao=relation,
                    defaults={
                        "url_isca": row.url_isca,
                        "link_afiliado": row.link_afiliado,
                        "estado": "pronto", "verificado_ok": True,
                        "verificado_em": row.verificado_em,
                        "url_canonica": row.url_canonica or row.link_afiliado,
                        "verificacao_motivo": "",
                        "tentativas": row.tentativas,
                        "ultima_tentativa": row.ultima_tentativa,
                        "proxima_tentativa": None,
                    },
                )
        except Exception as exc:
            falhas += len(itens)
            detalhe["erro"] = "Falha operacional ao gerar ou verificar links."
            detalhe["causa"] = type(exc).__name__
            from apps.scrapers.afiliado import causa_de_conta, registrar_falha
            conta = causa_de_conta(exc)
            if conta:
                # Sessão caída, Link Builder recusado ou navegador ocupado: UM
                # bloqueio de conta, não N falhas de produto. Gravar por item
                # empurrava o catálogo inteiro para o backoff e, na oitava rodada,
                # marcava como `nao_afiliavel` produtos que nunca tiveram defeito.
                detalhe["reason_code"] = f"account_blocked:{conta}"
                logger.warning(
                    "Afiliação de cupons %s bloqueada por %s (usuário %s); "
                    "nenhum produto penalizado.", slug, conta, usuario,
                )
                for produto in itens:
                    relation = relacao_por_produto[produto.id]
                    LinkAfiliadoProdutoCupomUsuario.objects.update_or_create(
                        usuario=usuario, relacao=relation,
                        defaults={
                            "estado": "pendente", "verificado_ok": None,
                            "verificacao_motivo": "Sessão necessária para gerar um novo link.",
                            "ultima_tentativa": agora,
                            "proxima_tentativa": agora + timezone.timedelta(minutes=15),
                        },
                    )
            else:
                for produto in itens:
                    registrar_falha(
                        usuario, produto,
                        f"Falha operacional de afiliação ({type(exc).__name__}).",
                    )
                    relation = relacao_por_produto[produto.id]
                    current = relation_rows.get(relation.pk)
                    LinkAfiliadoProdutoCupomUsuario.objects.update_or_create(
                        usuario=usuario, relacao=relation,
                        defaults={
                            "estado": "erro", "verificado_ok": None,
                            "verificacao_motivo": "Falha operacional ao gerar o link.",
                            "tentativas": (getattr(current, "tentativas", 0) or 0) + 1,
                            "ultima_tentativa": agora,
                            "proxima_tentativa": agora + timezone.timedelta(minutes=30),
                        },
                    )
                logger.exception("Afiliação de cupons %s falhou para %s", slug, usuario)
        after = {
            row.relacao_id for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
                usuario=usuario, relacao_id__in=target_relation_ids,
            ) if coupon_link_verified_and_fresh(row)
        }
        verificados = len(after - before)
        metricas["links_gerados"] += gerados
        metricas["links_falhos"] += falhas
        metricas["links_verificados"] += verificados
        detalhe.update({
            "gerados": gerados, "verificados": verificados, "falhos": falhas,
        })
        metricas["por_marketplace"][slug] = detalhe

    metricas["prontos"] = len(ids_cupons_prontos(usuario, cupons))
    return metricas


def executar_pipeline_cupons(
    *, usuarios=None, coletar=True, limite_preparo=12, limite_links=80,
    permitir_rede_preparo=True,
):
    """Executa um ciclo completo sem permitir que uma fonte derrube as demais."""
    usuarios = _usuarios_ativos(usuarios)
    resultado = coletar_cupons(usuarios=usuarios) if coletar else _metricas_vazias()

    # Associação e preparação agora compartilham a mesma fila justa e idempotente.
    # O antigo ``casar_cupons_container`` abria o mesmo container imediatamente
    # antes de ``preparar_lote`` e fazia HTTP/Chromium, sessão e capacidade serem
    # consumidos duas vezes. A função legada continua disponível para diagnóstico
    # e comandos manuais, mas o pipeline produtivo tem uma única autoridade.
    resultado["associacoes_container"] = 0
    resultado["associacao_modo"] = "preparation_queue"

    from apps.scrapers.coupon_products import (
        PREPARO_LOTE_HTTP_POR_CICLO, preparar_lote,
    )

    # O teto grande é da varredura por GET; o Chromium continua no teto pequeno.
    # É aqui que a cadência é conhecida (o worker roda a cada 15 min), então é
    # aqui que a escolha mora — `preparar_lote` sozinho não sabe disso.
    preparo = preparar_lote(
        limite=limite_preparo, usuarios=usuarios, detalhado=True,
        permitir_rede=permitir_rede_preparo,
        limite_http=PREPARO_LOTE_HTTP_POR_CICLO,
    )
    resultado["preparados"] = preparo["processados"]
    resultado["preparos_prontos"] = preparo["prontos"]
    resultado["preparo_por_fonte"] = preparo.get("por_fonte", {})
    resultado["preparos_adiados"] = preparo.get("adiados_sem_browser", 0)

    por_usuario = {}
    for usuario in usuarios:
        try:
            # `limite_codigo` fica DELIBERADAMENTE pequeno. A fila de ~2.400
            # cupons de código pede mais, mas o Link Builder é a superfície mais
            # frágil do sistema: subir este teto de 8 para 40 derrubou a sessão do
            # ML em produção em menos de uma hora (`lb_readiness=login_required`,
            # sessão `suspect`), e sessão caída para o funil INTEIRO — não só os
            # códigos. Vazão aqui se ganha com ciclos, não com lote.
            afiliacao = afiliar_cupons(usuario, limite=limite_links)
        except Exception as exc:
            logger.exception("Pipeline de cupons falhou para usuário %s", usuario.pk)
            afiliacao = {
                "links_falhos": 1,
                "erro": "Falha operacional no preparo de links.",
                "causa": type(exc).__name__,
            }
            resultado["falhos"] += 1
        por_usuario[str(usuario.pk)] = afiliacao
        for key in (
            "vinculados", "links_gerados", "links_verificados",
            "links_reprovados", "links_transitorios", "links_falhos", "prontos",
        ):
            resultado[key] += int(afiliacao.get(key, 0) or 0)
        try:
            from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
            por_canal = {
                channel: projetar_disponibilidade_cupons(usuario, channel=channel)
                for channel in ("whatsapp", "telegram")
            }
            afiliacao["disponibilidade"] = por_canal["whatsapp"]
            afiliacao["disponibilidade_por_canal"] = por_canal
        except Exception:
            resultado["falhos"] += 1
            logger.exception(
                "Projeção de disponibilidade de cupons falhou para usuário %s",
                usuario.pk,
            )
    resultado["usuarios"] = por_usuario
    return resultado
