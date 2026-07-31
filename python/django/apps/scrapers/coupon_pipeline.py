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
            persistidos = (
                persist_items(rows, owner=owner)
                if owner is not None else persist_items(rows)
            )
        else:
            persistidos = {"offers": 0, "coupons": 0}
        total_persistido = persistidos["offers"] + persistidos["coupons"]
        _fonte(
            resultado, slug, status=status, encontrados=len(rows),
            persistidos=total_persistido,
            motivo=payload.get("error") or (
                "Fonte desabilitada ou não configurada."
                if status == "disabled" else
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
        logger.exception("Fonte de cupons %s falhou isoladamente", slug)
        _fonte(resultado, slug, status="error", motivo=str(exc))
        return {"status": "error", "offers": [], "coupons": [], "error": str(exc)}


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

    from apps.scrapers.afiliado import tag_amazon

    amazon = [usuario for usuario in usuarios if tag_amazon(usuario)]
    if amazon:
        payload = _coletar_adaptador(
            "amazon-public-coupons", resultado, items=(),
        )
        rows = payload.get("offers", []) + payload.get("coupons", [])
        persistidos = 0
        falhas_amazon = 0
        if rows:
            from apps.scrapers.sources.persistence import persist_items

            for usuario in amazon:
                try:
                    counts = persist_items(rows, owner=usuario)
                    persistidos += counts["offers"] + counts["coupons"]
                except Exception:
                    falhas_amazon += 1
                    logger.exception(
                        "Persistência Amazon falhou isoladamente para usuário %s",
                        usuario.pk,
                    )
            info = resultado["fontes"]["amazon-public-coupons"]
            info["encontrados"] = len(rows)
            info["persistidos"] = persistidos
            if falhas_amazon:
                info["status"] = "error"
                info["motivo"] = (
                    f"Persistência falhou para {falhas_amazon} conta(s); "
                    "as demais foram preservadas."
                )
                resultado["falhos"] += falhas_amazon
            resultado["encontrados"] += len(rows)
            resultado["persistidos"] += persistidos
    else:
        _fonte(
            resultado, "amazon-public-coupons", status="skipped",
            motivo="Nenhuma conta ativa possui tag Amazon configurada.",
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
                logger.warning("Integração Awin %s falhou: %s", integracao.pk, exc)
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
    return CupomNormalizado.objects.select_related("fonte").filter(
        Q(owner__isnull=True) | Q(owner=usuario),
        estado="ativo",
    ).filter(
        Q(validade__isnull=True) | Q(validade__gte=timezone.now())
    )


def afiliar_cupons(usuario, *, limite=80, faixa=None):
    """Gera e verifica links dos produtos realmente ligados por ProdutoCupom."""
    from apps.scrapers.coupon_products import (
        ids_cupons_prontos, relacoes_preparadas_para_envio,
    )
    from apps.scrapers.coupon_rules import cupom_publicavel
    from apps.scrapers.marketplaces.registry import get_marketplace

    cupons = [cupom for cupom in _cupons_visiveis(usuario) if cupom_publicavel(cupom)]
    produtos = {}
    for cupom in cupons:
        for relacao in relacoes_preparadas_para_envio(cupom, usuario):
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
    if not produtos:
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
            detalhe["erro"] = str(exc)[:300]
            from apps.scrapers.afiliado import registrar_falha
            for produto in itens:
                registrar_falha(usuario, produto, str(exc))
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
    except Exception as exc:
        # A associação é uma etapa independente: os vínculos diretos de Amazon,
        # Awin, feed e cupons privados continuam sendo preparados.
        resultado["associacoes_container"] = 0
        resultado["falhos"] += 1
        logger.exception("Casamento de containers de cupons falhou: %s", exc)

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
            afiliacao = {"links_falhos": 1, "erro": str(exc)[:300]}
            resultado["falhos"] += 1
        por_usuario[str(usuario.pk)] = afiliacao
        for key in (
            "vinculados", "links_gerados", "links_verificados",
            "links_reprovados", "links_transitorios", "links_falhos", "prontos",
        ):
            resultado[key] += int(afiliacao.get(key, 0) or 0)
    resultado["usuarios"] = por_usuario
    return resultado
