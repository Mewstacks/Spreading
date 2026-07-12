"""Sincronização automática de relatórios de comissão.

O usuário não envia CSV. Cada marketplace expõe um adapter que busca/normaliza
linhas de receita a partir da conta conectada. Os adapters foram isolados para que
os seletores/URLs dos portais possam evoluir sem mexer no dashboard ou ranking.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.scrapers.models import ReceitaAfiliado, RelatorioSync
from apps.scrapers.eventos import log_event


class ReportSyncActionRequired(Exception):
    """A conta precisa ser conectada/reconectada pelo usuário."""


class ReportSyncError(Exception):
    """Falha operacional do sync."""


@dataclass
class ReportRow:
    marketplace: str
    data: object
    etiqueta: str = ""
    produto_nome: str = ""
    cliques: int = 0
    conversoes: int = 0
    pedidos: int = 0
    receita: float = 0.0
    comissao: float = 0.0
    periodo_inicio: object | None = None
    periodo_fim: object | None = None
    granularidade: str = "dia"


def _num(value) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _digest(usuario, row: ReportRow) -> str:
    raw = "|".join([
        str(usuario.id), row.marketplace, str(row.data), row.etiqueta,
        row.produto_nome, row.granularidade,
        str(row.periodo_inicio or ""), str(row.periodo_fim or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _upsert_rows(usuario, rows: list[ReportRow]) -> tuple[int, int]:
    criadas = atualizadas = 0
    with transaction.atomic():
        for row in rows:
            defaults = {
                "usuario": usuario,
                "marketplace": row.marketplace,
                "data": row.data,
                "etiqueta": row.etiqueta[:120],
                "produto_nome": row.produto_nome[:255],
                "cliques": max(0, int(_num(row.cliques))),
                "conversoes": max(0, int(_num(row.conversoes))),
                "pedidos": max(0, int(_num(row.pedidos))),
                "receita": _num(row.receita),
                "comissao": _num(row.comissao),
                "periodo_inicio": row.periodo_inicio,
                "periodo_fim": row.periodo_fim,
                "origem": "auto",
                "granularidade": row.granularidade[:20],
            }
            _, created = ReceitaAfiliado.objects.update_or_create(
                hash_origem=_digest(usuario, row),
                defaults=defaults,
            )
            criadas += int(created)
            atualizadas += int(not created)
    return criadas, atualizadas


class BaseReportAdapter:
    marketplace = ""

    def fetch(self, usuario, desde, ate) -> list[ReportRow]:
        raise NotImplementedError


class MercadoLivreReportAdapter(BaseReportAdapter):
    marketplace = "mercadolivre"

    def fetch(self, usuario, desde, ate) -> list[ReportRow]:
        from apps.scrapers.monitor_conexao import ml_conectado

        if not ml_conectado(usuario):
            raise ReportSyncActionRequired(
                "Reconecte o Mercado Livre para sincronizar métricas de afiliado."
            )
        url = getattr(settings, "ML_AFFILIATE_REPORT_URL", "")
        return _fetch_browser_report(usuario, self.marketplace, url, desde, ate)


class AmazonReportAdapter(BaseReportAdapter):
    marketplace = "amazon"

    def fetch(self, usuario, desde, ate) -> list[ReportRow]:
        perfil = getattr(usuario, "perfil", None)
        if not perfil or not perfil.amazon_conectado():
            raise ReportSyncActionRequired(
                "Conecte a Amazon Associates/Creators para sincronizar relatórios."
            )
        url = getattr(settings, "AMAZON_ASSOCIATES_REPORT_URL", "")
        return _fetch_browser_report(usuario, self.marketplace, url, desde, ate)


def _fetch_browser_report(usuario, marketplace: str, url: str, desde, ate) -> list[ReportRow]:
    """Executor browser-first.

    Este primeiro contrato espera que o portal exponha uma tabela HTML de relatório.
    Ele é propositalmente conservador: se a tabela ou sessão não estiver clara, falha
    com ação explícita em vez de inventar receita.
    """
    from playwright.sync_api import sync_playwright

    state_path = None
    if marketplace == "mercadolivre":
        from apps.scrapers.scraper_mercadolivre.link import _auth_path
        state_path = _auth_path(usuario)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context_kwargs = {}
            if state_path:
                context_kwargs["storage_state"] = state_path
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            rows = _extract_table_rows(page, marketplace, desde, ate)
            browser.close()
            return rows
    except ReportSyncActionRequired:
        raise
    except Exception as exc:
        raise ReportSyncError(f"{marketplace}: falha ao ler relatório automático: {exc}")


def _extract_table_rows(page, marketplace: str, desde, ate) -> list[ReportRow]:
    if page.locator("input[type='password'], input[name*='password' i]").count():
        raise ReportSyncActionRequired(
            f"Sessão de relatórios {marketplace} expirada. Reconecte a conta."
        )
    table_rows = page.locator("table tbody tr")
    count = table_rows.count()
    if count == 0:
        raise ReportSyncError(
            f"{marketplace}: relatório sem tabela detectável; parser precisa ser ajustado."
        )
    hoje = timezone.localdate()
    out: list[ReportRow] = []
    for idx in range(count):
        cells = [
            table_rows.nth(idx).locator("td").nth(i).inner_text(timeout=1000).strip()
            for i in range(table_rows.nth(idx).locator("td").count())
        ]
        if not cells:
            continue
        out.append(ReportRow(
            marketplace=marketplace,
            data=hoje,
            etiqueta=cells[0] if cells else "",
            produto_nome=cells[1] if len(cells) > 1 else "",
            cliques=_num(cells[2]) if len(cells) > 2 else 0,
            pedidos=int(_num(cells[3])) if len(cells) > 3 else 0,
            receita=_num(cells[4]) if len(cells) > 4 else 0,
            comissao=_num(cells[5]) if len(cells) > 5 else 0,
            periodo_inicio=desde,
            periodo_fim=ate,
            granularidade="etiqueta",
        ))
    return out


ADAPTERS = {
    "mercadolivre": MercadoLivreReportAdapter(),
    "amazon": AmazonReportAdapter(),
}


def sync_marketplace(usuario, marketplace: str, dias: int = 14) -> RelatorioSync:
    marketplace = (marketplace or "").lower()
    if marketplace not in ADAPTERS:
        raise ReportSyncError(f"Marketplace inválido: {marketplace}")
    agora = timezone.now()
    ate = timezone.localdate()
    desde = ate - timedelta(days=max(1, dias))
    sync, _ = RelatorioSync.objects.get_or_create(
        usuario=usuario, marketplace=marketplace)
    log_event("relatorios", "sync_started", f"Iniciando sync {marketplace}.",
              usuario=usuario, contexto={"marketplace": marketplace, "dias": dias})
    sync.status = "rodando"
    sync.ultimo_inicio = agora
    sync.erro = ""
    sync.save(update_fields=["status", "ultimo_inicio", "erro", "atualizado_em"])
    try:
        rows = ADAPTERS[marketplace].fetch(usuario, desde, ate)
        criadas, atualizadas = _upsert_rows(usuario, rows)
    except ReportSyncActionRequired as exc:
        sync.status = "acao"
        sync.erro = str(exc)[:500]
        sync.ultimo_fim = timezone.now()
        sync.proxima_execucao = timezone.now() + timedelta(hours=6)
        sync.save()
        log_event("relatorios", "sync_action_required", str(exc), level="warning",
                  usuario=usuario, contexto={"marketplace": marketplace})
        return sync
    except Exception as exc:
        sync.status = "erro"
        sync.erro = str(exc)[:500]
        sync.ultimo_fim = timezone.now()
        sync.proxima_execucao = timezone.now() + timedelta(hours=6)
        sync.save()
        log_event("relatorios", "sync_failed", str(exc), level="error",
                  usuario=usuario, contexto={"marketplace": marketplace}, exc=exc)
        return sync

    sync.status = "ok"
    sync.ultimo_fim = timezone.now()
    sync.ultimo_sucesso = sync.ultimo_fim
    sync.proxima_execucao = timezone.now() + timedelta(hours=6)
    sync.registros_criados = criadas
    sync.registros_atualizados = atualizadas
    sync.erro = ""
    sync.save()
    log_event(
        "relatorios", "sync_ok", f"{marketplace}: sync concluído.",
        usuario=usuario,
        contexto={"marketplace": marketplace, "criadas": criadas, "atualizadas": atualizadas},
    )
    return sync


def sync_user_reports(usuario, marketplace: str | None = None) -> list[RelatorioSync]:
    marketplaces = [marketplace] if marketplace else list(ADAPTERS)
    return [sync_marketplace(usuario, m) for m in marketplaces]


def sync_due_reports(limit: int = 20) -> list[RelatorioSync]:
    User = get_user_model()
    agora = timezone.now()
    usuarios = User.objects.filter(is_active=True, perfil__bloqueado=False)[:limit]
    resultados = []
    for usuario in usuarios:
        for marketplace in ADAPTERS:
            sync, _ = RelatorioSync.objects.get_or_create(
                usuario=usuario, marketplace=marketplace)
            if sync.proxima_execucao and sync.proxima_execucao > agora:
                continue
            resultados.append(sync_marketplace(usuario, marketplace))
    return resultados
