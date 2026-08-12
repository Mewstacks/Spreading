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

    return CupomNormalizado.objects.select_related("fonte").filter(
        Q(owner__isnull=True) | Q(owner=usuario),
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
    ja_tem = set(LinkAfiliadoCupomUsuario.objects.filter(
        usuario=usuario, cupom_id__in=[c.pk for c in candidatos], afiliado_ok=True,
    ).exclude(link_afiliado="").values_list("cupom_id", flat=True))
    pendentes = [c for c in candidatos if c.pk not in ja_tem]

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
        "pendentes": max(0, len(pendentes) - gerados),
    }


def afiliar_cupons(usuario, *, limite=80, faixa=None, limite_codigo=8):
    """Gera e verifica links dos produtos realmente ligados por ProdutoCupom."""
    from apps.scrapers.coupon_products import (
        ids_cupons_prontos, mapa_relacoes_prontas,
    )
    from apps.scrapers.coupon_rules import cupom_publicavel
    from apps.scrapers.marketplaces.registry import get_marketplace

    cupons = [
        cupom for cupom in _cupons_visiveis(usuario)
        if cupom_publicavel(cupom, usuario=usuario)
    ]
    preparadas, _prontas = mapa_relacoes_prontas(usuario, cupons)
    produtos = {}
    for relacoes in preparadas.values():
        for relacao in relacoes:
            produtos[relacao.produto_id] = relacao.produto

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

    agora = timezone.now()
    linhas = {
        row.produto_id: row
        for row in LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=produtos,
        )
    }
    candidatos = []
    for produto_id, produto in produtos.items():
        row = linhas.get(produto_id)
        if row and row.verificado_ok is True and row.link_afiliado:
            continue
        if row and (
            row.estado in ("nao_afiliavel", "erro")
            or (row.proxima_tentativa and row.proxima_tentativa > agora)
        ):
            continue
        candidatos.append(produto)
        if len(candidatos) >= limite:
            break

    grupos = defaultdict(list)
    for produto in candidatos:
        grupos[produto.marketplace or "mercadolivre"].append(produto)

    for slug, itens in grupos.items():
        before = set(LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=[p.id for p in itens],
            verificado_ok=True,
        ).values_list("produto_id", flat=True))
        gerados = falhas = 0
        detalhe = {"candidatos": len(itens)}
        try:
            gerados, falhas = get_marketplace(slug).prefetch_links(
                itens, usuario=usuario, faixa=faixa,
            )
            if slug == "mercadolivre":
                from apps.scrapers.scraper_mercadolivre.link import (
                    verificar_links_pendentes,
                )
                verificacao = verificar_links_pendentes(
                    usuario, limite=len(itens), produto_ids=[p.id for p in itens],
                )
                metricas["links_reprovados"] += verificacao["reprovados"]
                metricas["links_transitorios"] += verificacao["transitorios"]
                detalhe.update(verificacao)
        except Exception as exc:
            falhas += len(itens)
            detalhe["erro"] = "Falha operacional ao gerar ou verificar links."
            detalhe["causa"] = type(exc).__name__
            from apps.scrapers.afiliado import registrar_falha
            for produto in itens:
                registrar_falha(
                    usuario, produto,
                    f"Falha operacional de afiliação ({type(exc).__name__}).",
                )
            logger.exception("Afiliação de cupons %s falhou para %s", slug, usuario)
        after = set(LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=[p.id for p in itens],
            verificado_ok=True,
        ).values_list("produto_id", flat=True))
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

    try:
        from apps.scrapers.scraper_mercadolivre.cupons_container import (
            casar_cupons_container,
        )
        resultado["associacoes_container"] = casar_cupons_container()
    except Exception:
        # A associação é uma etapa independente: os vínculos diretos de Amazon,
        # Awin, feed e cupons privados continuam sendo preparados.
        resultado["associacoes_container"] = 0
        resultado["falhos"] += 1
        logger.exception("Casamento de containers de cupons falhou")

    from apps.scrapers.coupon_products import preparar_lote

    preparo = preparar_lote(
        limite=limite_preparo, usuarios=usuarios, detalhado=True,
        permitir_rede=permitir_rede_preparo,
    )
    resultado["preparados"] = preparo["processados"]
    resultado["preparos_prontos"] = preparo["prontos"]
    resultado["preparo_por_fonte"] = preparo.get("por_fonte", {})

    por_usuario = {}
    for usuario in usuarios:
        try:
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
            afiliacao["disponibilidade"] = projetar_disponibilidade_cupons(usuario)
        except Exception:
            resultado["falhos"] += 1
            logger.exception(
                "Projeção de disponibilidade de cupons falhou para usuário %s",
                usuario.pk,
            )
    resultado["usuarios"] = por_usuario
    return resultado
