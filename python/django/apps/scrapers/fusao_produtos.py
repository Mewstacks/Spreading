"""Canonicaliza links e funde `Produto` duplicado, preservando o que custa dinheiro.

`Produto` nunca teve constraint de unicidade. O mesmo anúncio entrou várias
vezes pelo catálogo porque writers diferentes gravavam formas diferentes da
mesma URL. Medido em produção em 02/09/2026: 68.360 produtos, 5.527 grupos
duplicados, 7.192 linhas excedentes (5.446 grupos no ML, 81 na Shopee) depois
de canonicalizar.

Duas coisas acontecem aqui, nesta ordem, e a ordem importa:

1. **Fundir** cada grupo num vencedor e apagar os perdedores. `planejar()`
   agrupa pela chave canônica calculada em memória, então não depende de o
   banco já estar canonicalizado.
2. **Canonicalizar** o `link_produto` das linhas que sobraram.

Fundir primeiro não é detalhe: canonicalizar antes faria duas linhas
convergirem para o mesmo valor, e com a constraint de unicidade já instalada
esse UPDATE estouraria `IntegrityError`. Depois da fusão não existe mais para
onde colidir.

O que a fusão protege, em ordem de custo:

* `LinkAfiliadoProdutoCupomUsuario` — link verificado que custou Playwright.
  Tem FK `CASCADE` para `ProdutoCupom`: apagar uma associação perdedora sem
  tratar isto ANTES come o link junto. É a linha mais cara do banco.
* `LinkAfiliadoUsuario` — mesmo caso, `unique_together (usuario, produto)`.
  Um `verificado_ok=True` nunca perde para um vazio.
* `HistoricoEnvio` — perder isto faz o sistema reenviar oferta já enviada,
  que é a falha que o usuário final enxerga.
* `Publicacao` — registro de auditoria imutável; `SET_NULL`, nunca apagar.

Idempotente: rodar de novo sobre um catálogo já fundido não muda nada.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce

from apps.scrapers.identidade_produto import link_canonico

logger = logging.getLogger(__name__)

# Força do veredito de uma associação produto–cupom, do mais forte ao mais fraco.
_FORCA_STATUS = {"confirmado": 3, "provavel": 2, "nao_aplicavel": 1, "expirado": 0}


def _chave(marketplace, owner_id, asin, link):
    """Mesma regra de identidade que os writers usam (identidade_produto)."""
    if (asin or "").strip():
        return (marketplace, owner_id, "asin", asin.strip())
    canon = link_canonico(marketplace, link or "")
    if not canon:
        return None
    return (marketplace, owner_id, "link", canon)


def canonicalizar_links(*, lote=1000, dry_run=False):
    """Reescreve `link_produto` na forma canônica. Devolve quantas linhas mudam."""
    from apps.scrapers.models import Produto

    mudadas = 0
    pendentes = []
    campos = ("id", "marketplace", "link_produto")
    for pk, marketplace, link in (
        Produto.objects.exclude(link_produto="")
        .values_list(*campos).iterator(chunk_size=2000)
    ):
        canon = link_canonico(marketplace, link or "")
        # String vazia significa "não consigo identificar": manter o valor
        # original é melhor que apagar a única pista que a linha tem.
        if not canon or canon == (link or ""):
            continue
        pendentes.append((pk, canon))
        mudadas += 1
        if not dry_run and len(pendentes) >= lote:
            _gravar_links(pendentes)
            pendentes = []
    if not dry_run and pendentes:
        _gravar_links(pendentes)
    return mudadas


def _gravar_links(pendentes):
    from apps.scrapers.models import Produto

    with transaction.atomic():
        for pk, canon in pendentes:
            Produto.objects.filter(pk=pk).update(link_produto=canon)


def planejar():
    """Grupos duplicados por chave natural, já com vencedor escolhido.

    Vencedor = observação mais recente, com o pk como desempate — o mesmo
    critério que `_upsert_resiliente` já usa em produção quando encontra
    duplicata, para os dois caminhos não discordarem sobre quem é o bom.
    """
    from apps.scrapers.models import Produto

    grupos = defaultdict(list)
    campos = ("id", "marketplace", "owner_id", "asin", "link_produto")
    ordem = Coalesce("ultima_observacao", "primeira_observacao")
    for pk, marketplace, owner_id, asin, link in (
        Produto.objects.annotate(_ordem=ordem)
        .order_by("-_ordem", "-id")
        .values_list(*campos).iterator(chunk_size=2000)
    ):
        chave = _chave(marketplace, owner_id, asin, link)
        if chave is None:
            continue
        grupos[chave].append(pk)
    # A ordenação acima já põe o vencedor em primeiro dentro de cada grupo.
    return {k: v for k, v in grupos.items() if len(v) > 1}


def executar(*, lote=200, dry_run=False):
    """Funde todos os grupos duplicados. Devolve o resumo do que foi feito."""
    grupos = planejar()
    resumo = {
        "grupos": len(grupos),
        "perdedores": sum(len(v) - 1 for v in grupos.values()),
        "produto_cupom_repontado": 0,
        "produto_cupom_fundido": 0,
        "link_produto_cupom_repontado": 0,
        "link_produto_cupom_descartado": 0,
        "link_usuario_repontado": 0,
        "link_usuario_descartado": 0,
        "historico_repontado": 0,
        "publicacao_repontada": 0,
        "produtos_apagados": 0,
    }
    if dry_run:
        return resumo

    pendentes = list(grupos.values())
    for inicio in range(0, len(pendentes), lote):
        fatia = pendentes[inicio:inicio + lote]
        with transaction.atomic():
            for pks in fatia:
                _fundir_grupo(pks[0], pks[1:], resumo)
    return resumo


def _fundir_grupo(vencedor_pk, perdedores_pks, resumo):
    from apps.scrapers.models import (
        HistoricoEnvio, LinkAfiliadoProdutoCupomUsuario, LinkAfiliadoUsuario,
        Produto, ProdutoCupom, Publicacao,
    )

    vencedor = Produto.objects.filter(pk=vencedor_pk).first()
    if vencedor is None:
        return
    perdedores = list(Produto.objects.filter(pk__in=perdedores_pks))
    if not perdedores:
        return

    _absorver_campos(vencedor, perdedores)

    for perdedor in perdedores:
        _mover_associacoes_de_cupom(vencedor, perdedor, resumo)
        _mover_links_do_usuario(vencedor, perdedor, resumo)
        resumo["historico_repontado"] += HistoricoEnvio.objects.filter(
            produto=perdedor).update(produto=vencedor)
        resumo["publicacao_repontada"] += Publicacao.objects.filter(
            produto=perdedor).update(produto=vencedor)

    # Apagar por último: uma queda no meio deixa duplicata (recuperável, o
    # comando é idempotente) em vez de FK órfã (perdida).
    apagados, _ = Produto.objects.filter(
        pk__in=[p.pk for p in perdedores]).delete()
    resumo["produtos_apagados"] += len(perdedores)


def _absorver_campos(vencedor, perdedores):
    """Traz do perdedor o que o vencedor não tem. Nunca sobrescreve o que existe."""
    from apps.scrapers.sources.persistence import evidencia_com_cupom_preservado

    mudou = []
    for perdedor in perdedores:
        for campo in ("frase_llm", "nome_llm", "imagem_url", "categoria",
                      "macro_categoria"):
            atual = getattr(vencedor, campo, None)
            vindo = getattr(perdedor, campo, None)
            # "DESCONHECIDO" é o valor honesto para "ninguém classificou", e
            # não deve bloquear uma classificação real vinda do perdedor.
            vazio = not atual or str(atual).strip().upper() == "DESCONHECIDO"
            if vazio and vindo and str(vindo).strip().upper() != "DESCONHECIDO":
                setattr(vencedor, campo, vindo)
                mudou.append(campo)
        nova = evidencia_com_cupom_preservado(
            vencedor.evidencia or {}, perdedor.evidencia or {})
        if nova != (vencedor.evidencia or {}):
            vencedor.evidencia = nova
            mudou.append("evidencia")
    if mudou:
        vencedor.save(update_fields=sorted(set(mudou)))


def _mover_associacoes_de_cupom(vencedor, perdedor, resumo):
    from apps.scrapers.models import ProdutoCupom

    do_vencedor = {
        rel.cupom_id: rel
        for rel in ProdutoCupom.objects.filter(produto=vencedor)
    }
    for rel in ProdutoCupom.objects.filter(produto=perdedor):
        gemea = do_vencedor.get(rel.cupom_id)
        if gemea is None:
            ProdutoCupom.objects.filter(pk=rel.pk).update(produto=vencedor)
            do_vencedor[rel.cupom_id] = rel
            resumo["produto_cupom_repontado"] += 1
            continue
        _fundir_associacao(gemea, rel, resumo)
        resumo["produto_cupom_fundido"] += 1


def _fundir_associacao(gemea, rel, resumo):
    """Colisão em unique_together (produto, cupom): fica o veredito mais forte.

    Os links de afiliado da associação perdedora migram ANTES do delete — o FK
    é CASCADE e apagar primeiro levaria junto link verificado que custou
    Playwright para existir.
    """
    from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario, ProdutoCupom

    campos = []
    if _FORCA_STATUS.get(rel.status, -1) > _FORCA_STATUS.get(gemea.status, -1):
        gemea.status = rel.status
        campos.append("status")
    if rel.verificado_em and (
        not gemea.verificado_em or rel.verificado_em > gemea.verificado_em
    ):
        gemea.verificado_em = rel.verificado_em
        campos.append("verificado_em")
    for campo in ("preco_original", "preco_atual", "preco_final", "activation_key"):
        if not getattr(gemea, campo, None) and getattr(rel, campo, None):
            setattr(gemea, campo, getattr(rel, campo))
            campos.append(campo)
    if campos:
        gemea.save(update_fields=sorted(set(campos)))

    existentes = {
        link.usuario_id: link
        for link in LinkAfiliadoProdutoCupomUsuario.objects.filter(relacao=gemea)
    }
    for link in LinkAfiliadoProdutoCupomUsuario.objects.filter(relacao=rel):
        rival = existentes.get(link.usuario_id)
        if rival is None:
            LinkAfiliadoProdutoCupomUsuario.objects.filter(pk=link.pk).update(
                relacao=gemea)
            existentes[link.usuario_id] = link
            resumo["link_produto_cupom_repontado"] += 1
            continue
        if _link_melhor(link, rival):
            LinkAfiliadoProdutoCupomUsuario.objects.filter(pk=rival.pk).delete()
            LinkAfiliadoProdutoCupomUsuario.objects.filter(pk=link.pk).update(
                relacao=gemea)
            existentes[link.usuario_id] = link
            resumo["link_produto_cupom_repontado"] += 1
        else:
            LinkAfiliadoProdutoCupomUsuario.objects.filter(pk=link.pk).delete()
        resumo["link_produto_cupom_descartado"] += 1
    ProdutoCupom.objects.filter(pk=rel.pk).delete()


def _mover_links_do_usuario(vencedor, perdedor, resumo):
    """unique_together (usuario, produto): link verificado nunca perde pra vazio."""
    from apps.scrapers.models import LinkAfiliadoUsuario

    existentes = {
        link.usuario_id: link
        for link in LinkAfiliadoUsuario.objects.filter(produto=vencedor)
    }
    for link in LinkAfiliadoUsuario.objects.filter(produto=perdedor):
        rival = existentes.get(link.usuario_id)
        if rival is None:
            LinkAfiliadoUsuario.objects.filter(pk=link.pk).update(produto=vencedor)
            existentes[link.usuario_id] = link
            resumo["link_usuario_repontado"] += 1
            continue
        if _link_melhor(link, rival):
            LinkAfiliadoUsuario.objects.filter(pk=rival.pk).delete()
            LinkAfiliadoUsuario.objects.filter(pk=link.pk).update(produto=vencedor)
            existentes[link.usuario_id] = link
            resumo["link_usuario_repontado"] += 1
        else:
            LinkAfiliadoUsuario.objects.filter(pk=link.pk).delete()
        resumo["link_usuario_descartado"] += 1


def _link_melhor(candidato, atual) -> bool:
    """Verificado ganha de não-verificado; depois, o mais recente; depois, o pk."""
    def peso(link):
        return (
            1 if getattr(link, "verificado_ok", None) else 0,
            getattr(link, "verificado_em", None) or getattr(link, "ultima_tentativa", None),
        )

    c_ok, c_data = peso(candidato)
    a_ok, a_data = peso(atual)
    if c_ok != a_ok:
        return c_ok > a_ok
    if c_data and a_data and c_data != a_data:
        return c_data > a_data
    if c_data and not a_data:
        return True
    if a_data and not c_data:
        return False
    return candidato.pk > atual.pk
