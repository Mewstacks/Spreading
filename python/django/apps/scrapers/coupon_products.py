"""Descoberta estrita e cache de produtos aplicaveis a cupons.

Nao existe fallback por categoria: um item so entra quando a fonte fornece uma
relacao verificavel (container/campanha, id/link direto, promocao com o codigo ou
escopo explicitamente site-wide).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import CupomPreparacao, Produto, ProdutoCupom

logger = logging.getLogger(__name__)

CACHE_HORAS = 3
MAX_CANDIDATOS = 36
_CENT = Decimal("0.01")


class _MLCardsHTMLParser(HTMLParser):
    """Extrai os cards SSR do container sem subir um navegador."""

    def __init__(self, limite=9):
        super().__init__(convert_charrefs=True)
        self.limite = limite
        self.rows = []
        self.card = None
        self.div_depth = 0
        self.in_title = False
        self.in_previous = False
        self.current_depth = None
        self.capture = None
        self.buffer = []

    @staticmethod
    def _classes(attrs):
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)
        if tag == "div":
            if self.card is None and "poly-card" in classes and len(self.rows) < self.limite:
                self.card = {
                    "nome_produto": "", "link_produto": "", "imagem_url": "",
                    "previous_fraction": "", "previous_cents": "",
                    "current_fraction": "", "current_cents": "", "frete_full": False,
                }
                self.div_depth = 1
            elif self.card is not None:
                self.div_depth += 1
            if self.card is not None and "poly-price__current" in classes:
                self.current_depth = self.div_depth
        if self.card is None:
            return
        if tag == "a" and "poly-component__title" in classes:
            self.in_title = True
            self.buffer = []
            self.card["link_produto"] = html.unescape(attrs_dict.get("href", "")).split("#")[0]
        elif tag == "img" and (
            "poly-component__picture" in classes or not self.card["imagem_url"]
        ):
            image = attrs_dict.get("data-src") or attrs_dict.get("src") or ""
            if image and not image.startswith("data:"):
                self.card["imagem_url"] = html.unescape(image).split("?", 1)[0]
        elif tag == "s" and "andes-money-amount--previous" in classes:
            self.in_previous = True
        elif tag == "span" and "andes-money-amount__fraction" in classes:
            mode = "previous" if self.in_previous else (
                "current" if self.current_depth is not None else "")
            if mode:
                self.capture, self.buffer = f"{mode}_fraction", []
        elif tag == "span" and "andes-money-amount__cents" in classes:
            mode = "previous" if self.in_previous else (
                "current" if self.current_depth is not None else "")
            if mode:
                self.capture, self.buffer = f"{mode}_cents", []
        if "full" in (attrs_dict.get("aria-label") or "").casefold():
            self.card["frete_full"] = True

    def handle_data(self, data):
        if self.card is not None and (self.in_title or self.capture):
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.card is None:
            return
        if tag == "span" and self.capture:
            self.card[self.capture] = "".join(self.buffer).strip()
            self.capture, self.buffer = None, []
        elif tag == "a" and self.in_title:
            self.card["nome_produto"] = " ".join("".join(self.buffer).split())[:255]
            self.in_title, self.buffer = False, []
        elif tag == "s":
            self.in_previous = False
        if tag == "div":
            if self.current_depth == self.div_depth:
                self.current_depth = None
            if self.div_depth == 1:
                self._finish_card()
                self.card = None
                self.div_depth = 0
            else:
                self.div_depth -= 1

    @staticmethod
    def _price(fraction, cents):
        raw = str(fraction or "").replace(".", "").replace(" ", "")
        try:
            return float(f"{raw}.{str(cents or '0').strip().zfill(2)}")
        except ValueError:
            return 0.0

    def _finish_card(self):
        row = self.card
        current = self._price(row["current_fraction"], row["current_cents"])
        previous = self._price(row["previous_fraction"], row["previous_cents"]) or current
        if not row["nome_produto"] or not row["link_produto"] or not row["imagem_url"]:
            return
        if current <= 0 or previous < current:
            return
        row["preco_original_sem_desconto"] = f"{previous:.2f}"
        row["preco_vitrine_atual"] = f"{current:.2f}"
        row.pop("previous_fraction", None)
        row.pop("previous_cents", None)
        row.pop("current_fraction", None)
        row.pop("current_cents", None)
        self.rows.append(row)


def _produtos_ml_do_html(html_text, limite=9):
    parser = _MLCardsHTMLParser(limite=limite)
    parser.feed(html_text or "")
    return parser.rows


def _decimal(value):
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _url_canonica(url):
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))


def chave_produtos_cupom(cupom) -> str:
    """Fingerprint dos campos que alteram escopo ou calculo do cupom."""
    payload = {
        "marketplace": str(getattr(cupom, "marketplace", "") or "").lower(),
        "codigo": str(getattr(cupom, "codigo", "") or "").strip().upper(),
        "link": _url_canonica(getattr(cupom, "link", "")),
        "regras": getattr(cupom, "regras", {}) or {},
        "evidencia": {
            key: (getattr(cupom, "evidencia", {}) or {}).get(key)
            for key in ("product_ids", "asins", "item_ids", "association")
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def atualizar_chave_cupom(cupom, *, salvar=True) -> str:
    chave = chave_produtos_cupom(cupom)
    if getattr(cupom, "produtos_chave", "") != chave:
        cupom.produtos_chave = chave
        if salvar and getattr(cupom, "pk", None):
            type(cupom).objects.filter(pk=cupom.pk).update(produtos_chave=chave)
    return chave


def _usuario_do_preparo(cupom, usuario):
    # Somente cupons publicos do ML compartilham catalogo. Um cupom privado pode
    # apontar para uma campanha particular e continua isolado pelo dono.
    if (str(cupom.marketplace).lower() == "mercadolivre"
            and getattr(cupom, "owner_id", None) is None):
        return None
    return usuario or getattr(cupom, "owner", None)


def _site_inteiro(cupom):
    regras = getattr(cupom, "regras", {}) or {}
    if regras.get("is_mar_aberto") or regras.get("site_wide") is True:
        return True
    escopo = str(regras.get("escopo") or "").strip().casefold()
    return escopo in {"site inteiro", "todo o site", "todos os produtos", "loja inteira"}


def _ids_explicitos(cupom):
    evidence = getattr(cupom, "evidencia", {}) or {}
    out = set()
    for key in ("product_ids", "asins", "item_ids"):
        value = evidence.get(key) or []
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        if isinstance(value, (list, tuple, set)):
            out.update(str(x).strip().upper() for x in value if str(x).strip())
    return out


def _produto_direto(cupom, produto):
    cupom_url = _url_canonica(getattr(cupom, "link", ""))
    produto_url = _url_canonica(getattr(produto, "link_produto", ""))
    if cupom_url and produto_url and cupom_url == produto_url:
        return True
    ids = _ids_explicitos(cupom)
    if not ids:
        return False
    asin = str(getattr(produto, "asin", "") or "").upper()
    if asin and asin in ids:
        return True
    evidencia = getattr(produto, "evidencia", {}) or {}
    pid = str(evidencia.get("product_id") or evidencia.get("item_id") or "").upper()
    return bool(pid and pid in ids)


def _promocao_confirma_codigo(cupom, produto):
    codigo = str(getattr(cupom, "codigo", "") or "").strip().casefold()
    if not codigo:
        return False
    ev = getattr(produto, "evidencia", {}) or {}
    promo = ev.get("promotion") or {}
    textos = [
        ev.get("promotional_text"), ev.get("promotion_text"),
        promo.get("label") if isinstance(promo, dict) else "",
        promo.get("code") if isinstance(promo, dict) else "",
    ]
    return any(codigo in str(texto or "").casefold() for texto in textos)


def _base_produtos(cupom, usuario):
    mkt = str(cupom.marketplace or "").lower()
    qs = Produto.objects.filter(marketplace=mkt).exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).exclude(imagem_url="").filter(preco_com_cupom__gt=0)
    if mkt in ("amazon", "awin"):
        qs = qs.filter(owner=usuario)
    else:
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))

    confirmados = ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=qs,
    ).values_list("produto_id", flat=True)
    ids_confirmados = set(confirmados)

    external = str(cupom.external_id or "")
    campanha = external.split(":", 1)[1] if external.startswith("campanha:") else ""
    programa_id = str(getattr(getattr(cupom, "programa", None), "external_id", "") or "")

    candidatos = []
    for produto in qs.order_by("-ultima_observacao")[:500]:
        provado = produto.id in ids_confirmados
        if not provado and campanha:
            provado = bool(produto.campanha_id == campanha)
        if not provado and _produto_direto(cupom, produto):
            provado = True
        if not provado and _promocao_confirma_codigo(cupom, produto):
            provado = True
        if not provado and _site_inteiro(cupom):
            if mkt == "awin":
                ev = produto.evidencia or {}
                provado = bool(programa_id and str(ev.get("advertiser_id") or "") == programa_id)
            else:
                provado = True
        if provado:
            candidatos.append(produto)
            if len(candidatos) >= MAX_CANDIDATOS:
                break
    return candidatos


def calcular_precos(cupom, produto):
    """(original, atual, final) ou None; todos Decimal e especificos do cupom."""
    from apps.scrapers.coupon_rules import regras_do_cupom

    regras = regras_do_cupom(cupom)
    atual = _decimal(getattr(produto, "preco_com_cupom", None))
    original = _decimal(getattr(produto, "preco_sem_desconto", None))
    if atual is None or atual <= 0:
        return None
    if original is None or original < atual or original > atual * 10:
        original = atual
    minimo = _decimal(regras.get("valor_minimo")) or Decimal("0")
    if minimo and atual < minimo:
        return None

    ev = getattr(produto, "evidencia", {}) or {}
    final_explicito = _decimal(ev.get("coupon_final_price"))
    if final_explicito is not None:
        final = final_explicito
    else:
        valor = _decimal(regras.get("valor_desconto"))
        tipo = str(regras.get("tipo_desconto") or "").lower()
        if valor is None or valor <= 0:
            return None
        desconto = (atual * valor / Decimal("100")
                    if tipo in ("porcentagem", "percentual") else valor
                    if tipo == "fixo" else None)
        if desconto is None:
            return None
        teto = _decimal(regras.get("desconto_maximo"))
        if teto and desconto > teto:
            desconto = teto
        final = (atual - desconto).quantize(_CENT, rounding=ROUND_HALF_UP)

    if final <= 0 or final >= atual:
        return None
    if (original - final) / original >= Decimal("0.90"):
        return None
    return original, atual, final


def _coletar_ml_remoto(cupom):
    """Materializa a listagem oficial do ML quando ela ainda nao esta no banco."""
    link = str((cupom.regras or {}).get("container_url") or cupom.link or "").strip()
    try:
        host = (urlsplit(link).hostname or "").casefold().rstrip(".")
    except ValueError:
        host = ""
    # A coleta abre um navegador autenticado. Nunca permita que um cupom manual
    # transforme esse caminho em um navegador/SSRF para um host arbitrario.
    if not link or not (host == "mercadolivre.com.br"
                        or host.endswith(".mercadolivre.com.br")):
        return 0
    from apps.scrapers.auxiliar import iniciar_browser
    from apps.scrapers.session_paths import ml_auth_path
    from apps.scrapers.scraper_mercadolivre.scraper import (
        _ml_http_session, listar_itens_por_cupom,
    )

    payload = {
        "campaignId": (str(cupom.external_id).split(":", 1)[1]
                       if str(cupom.external_id).startswith("campanha:") else f"norm-{cupom.id}"),
        "title": cupom.titulo, "link_produtos": link,
        "desconto": {"tipo": (cupom.regras or {}).get("tipo_desconto"),
                     "valor": (cupom.regras or {}).get("valor_desconto")},
        "valor_minimo": (cupom.regras or {}).get("valor_minimo") or 0,
        "desconto_maximo": (cupom.regras or {}).get("desconto_maximo"),
    }
    # Os containers são SSR: um GET autenticado já contém nome, anúncio, imagem e
    # preços. Isso prepara todos os cupons oficiais em segundos, em vez de manter um
    # Chromium aberto por vários minutos. Browser fica como fallback para challenge.
    resultado = None
    try:
        response = _ml_http_session(ml_auth_path()).get(link, timeout=25)
        response.raise_for_status()
        rows = _produtos_ml_do_html(response.text, limite=9)
        if rows:
            resultado = {**payload, "produtos_aplicaveis": rows}
    except Exception as exc:
        logger.info("Container ML via HTTP falhou para %s: %s", cupom.pk, exc)
    if resultado is None:
        with iniciar_browser(
            auth_path=ml_auth_path(), headless=True, validar_sessao=False,
        ) as (page, _context):
            resultado = listar_itens_por_cupom(payload, page, max_paginas=2)
    total = 0
    for row in (resultado or {}).get("produtos_aplicaveis", []):
        link_produto = str(row.get("link_produto") or "")[:1000]
        imagem = str(row.get("imagem_url") or "")[:1000]
        if not link_produto or not imagem:
            continue
        produto = Produto.objects.filter(
            marketplace="mercadolivre", owner__isnull=True,
            link_produto=link_produto).first()
        defaults = {
            "campanha_id": payload["campaignId"], "origem": "cupom",
            "fonte": "mercadolivre-cupom", "nome": str(row["nome_produto"])[:255],
            "preco_sem_desconto": float(row["preco_original_sem_desconto"]),
            "preco_com_cupom": float(row["preco_vitrine_atual"]),
            "preco_fonte": float(row["preco_vitrine_atual"]),
            "preco_efetivo": float(row["preco_vitrine_atual"]),
            "link_produto": link_produto, "imagem_url": imagem,
            "estado": "ativo", "ultima_verificacao": timezone.now(),
        }
        if produto:
            for key, value in defaults.items():
                setattr(produto, key, value)
            produto.save(update_fields=list(defaults))
        else:
            produto = Produto.objects.create(marketplace="mercadolivre", **defaults)
        ProdutoCupom.objects.update_or_create(
            produto=produto, cupom=cupom,
            defaults={"status": "confirmado", "verificado_em": timezone.now(),
                      "evidencia": {"regra": "pagina_oficial", "url": link}},
        )
        total += 1
    return total


def preparar_cupom(cupom, usuario=None, *, force=False, permitir_rede=True):
    """Prepara e devolve ProdutoCupom confirmados, ou [] sem fallback inseguro."""
    from apps.scrapers.coupon_rules import cupom_publicavel

    contexto = _usuario_do_preparo(cupom, usuario)
    chave = atualizar_chave_cupom(cupom)
    preparo, _ = CupomPreparacao.objects.get_or_create(cupom=cupom, usuario=contexto)
    fresco_desde = timezone.now() - timedelta(hours=CACHE_HORAS)
    if (not force and preparo.status == "pronto" and preparo.produtos_chave == chave
            and preparo.verificado_em and preparo.verificado_em >= fresco_desde):
        cached = list(ProdutoCupom.objects.filter(
            cupom=cupom, status="confirmado", preco_final__isnull=False,
        ).select_related("produto"))
        cached.sort(key=lambda r: (
            (r.preco_original - r.preco_final) / r.preco_original,
            r.preco_original - r.preco_final,
        ), reverse=True)
        return cached[:9]

    if not cupom_publicavel(cupom):
        CupomPreparacao.objects.filter(pk=preparo.pk).update(
            status="vazio", produtos_chave=chave, verificado_em=timezone.now(),
            proxima_tentativa=None,
            erro="Cupom sem código público ou ativação comprovada.")
        return []

    try:
        candidatos = _base_produtos(cupom, contexto)
        if (not candidatos and permitir_rede
                and str(cupom.marketplace).lower() == "mercadolivre"):
            _coletar_ml_remoto(cupom)
            candidatos = _base_produtos(cupom, contexto)

        validos = []
        vistos = set()
        agora = timezone.now()
        for produto in candidatos:
            identidade = _url_canonica(produto.link_produto) or f"id:{produto.id}"
            if identidade in vistos:
                continue
            precos = calcular_precos(cupom, produto)
            if not precos:
                continue
            original, atual, final = precos
            relacao, _ = ProdutoCupom.objects.update_or_create(
                produto=produto, cupom=cupom,
                defaults={"status": "confirmado", "verificado_em": agora,
                          "preco_original": original, "preco_atual": atual,
                          "preco_final": final,
                          "evidencia": {"regra": "associacao_comprovada",
                                        "produtos_chave": chave}},
            )
            validos.append(relacao)
            vistos.add(identidade)
        ids = [r.id for r in validos]
        with transaction.atomic():
            ProdutoCupom.objects.filter(cupom=cupom, status="confirmado").exclude(
                id__in=ids).update(status="expirado")
            CupomPreparacao.objects.filter(pk=preparo.pk).update(
                status="pronto" if validos else "vazio", produtos_chave=chave,
                verificado_em=agora, proxima_tentativa=None,
                erro="" if validos else "Nenhum produto comprovadamente aplicavel.")
        validos.sort(key=lambda r: (
            (r.preco_original - r.preco_final) / r.preco_original,
            r.preco_original - r.preco_final,
        ), reverse=True)
        return validos[:9]
    except Exception as exc:
        logger.exception("Preparacao do cupom %s falhou", cupom.pk)
        CupomPreparacao.objects.filter(pk=preparo.pk).update(
            status="erro", produtos_chave=chave, verificado_em=timezone.now(),
            proxima_tentativa=timezone.now() + timedelta(minutes=30),
            erro=str(exc)[:500])
        return []


def cupom_pronto_para_usuario(cupom, usuario) -> bool:
    contexto = _usuario_do_preparo(cupom, usuario)
    # Recalcula em vez de confiar no campo denormalizado: uma alteração feita por
    # admin/script também deve invalidar imediatamente um preparo antigo.
    chave = chave_produtos_cupom(cupom)
    return CupomPreparacao.objects.filter(
        cupom=cupom, usuario=contexto, status="pronto", produtos_chave=chave,
    ).exists()


def ids_cupons_prontos(usuario, cupons):
    cupons = list(cupons)
    if not cupons:
        return set()
    ids = [c.id for c in cupons]
    rows = CupomPreparacao.objects.filter(
        cupom_id__in=ids, status="pronto",
    ).filter(Q(usuario__isnull=True) | Q(usuario=usuario)).values_list(
        "cupom_id", "usuario_id", "produtos_chave")
    por_contexto = {(cid, uid): chave for cid, uid, chave in rows}
    prontos = set()
    for cupom in cupons:
        uid = getattr(usuario, "id", None)
        if (str(cupom.marketplace).lower() == "mercadolivre"
                and cupom.owner_id is None):
            uid = None
        chave = chave_produtos_cupom(cupom)
        if por_contexto.get((cupom.id, uid)) == chave:
            prontos.add(cupom.id)
    return prontos


def preparar_lote(limite=8):
    """Prepara cupons ativos em pequenos lotes; chamado pelo worker de catalogo."""
    from django.contrib.auth import get_user_model
    from apps.scrapers.coupon_rules import cupom_publicavel
    from apps.scrapers.models import CupomNormalizado

    agora = timezone.now()
    # Filtra as duas origens publicáveis antes do recorte. As milhares de campanhas
    # personalizadas do ML não podem ocupar o lote; os cupons oficiais Amazon são
    # de ativação e, portanto, não possuem código digitável.
    cupons = list(CupomNormalizado.objects.filter(estado="ativo").filter(
        Q(codigo__gt="") | Q(fonte__slug="amazon-public-coupons")
    ).filter(
        Q(validade__isnull=True) | Q(validade__gte=agora)
    ).order_by("-ultima_observacao")[:200])
    feitos = prontos = 0
    usuarios = list(get_user_model().objects.filter(is_active=True))
    for cupom in cupons:
        if feitos >= limite or not cupom_publicavel(cupom):
            continue
        contextos = [None] if _usuario_do_preparo(cupom, None) is None else (
            [cupom.owner] if cupom.owner_id else usuarios)
        for usuario in contextos:
            if feitos >= limite:
                break
            contexto = _usuario_do_preparo(cupom, usuario)
            chave = atualizar_chave_cupom(cupom)
            prep = CupomPreparacao.objects.filter(cupom=cupom, usuario=contexto).first()
            if prep and prep.produtos_chave == chave and prep.verificado_em:
                if prep.status == "pronto" and prep.verificado_em >= agora - timedelta(hours=CACHE_HORAS):
                    continue
                if prep.proxima_tentativa and prep.proxima_tentativa > agora:
                    continue
            feitos += 1
            if preparar_cupom(cupom, usuario=usuario, force=True):
                prontos += 1
    return {"processados": feitos, "prontos": prontos}
