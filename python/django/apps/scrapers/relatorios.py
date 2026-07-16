"""Sincronização automática de relatórios de comissão.

O usuário não envia CSV. Cada marketplace expõe um adapter que busca/normaliza
linhas de receita a partir da conta conectada. Os adapters foram isolados para que
os seletores/URLs dos portais possam evoluir sem mexer no dashboard ou ranking.
"""
from __future__ import annotations

import hashlib
import math
import re
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


class ReportSyncNaoConfigurado(Exception):
    """A leitura automática deste portal não está disponível.

    Diferente de ReportSyncActionRequired: aqui não há ação do usuário que resolva —
    falta configuração/implementação nossa. Tratar os dois como a mesma coisa mandava
    o usuário "reconectar" uma conta que já estava conectada, para sempre.
    """


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


_SO_NUMERO = re.compile(r"[^\d,.\-]")
_MILHAR_PT = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def _num(value) -> float:
    """Converte uma célula de portal em float. 0.0 quando não há número.

    Os portais são pt-BR e devolvem texto formatado ('R$ 1.234,56', '12,50', '3,2%').
    float() direto engolia tudo isso como 0.0 — e como o sync gravava status "ok" do
    mesmo jeito, o dashboard exibia R$ 0,00 com selo verde de "sincronizado".
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if math.isfinite(v) else 0.0
    texto = _SO_NUMERO.sub("", str(value or "").replace("\xa0", " ")).strip()
    if not texto or texto in {"-", "."}:
        return 0.0
    if "," in texto:
        # pt-BR: '.' é milhar, ',' é decimal.
        texto = texto.replace(".", "").replace(",", ".")
    elif _MILHAR_PT.match(texto):
        # '1.234' sem vírgula: milhar pt-BR, não decimal ('1.234' = mil duzentos e
        # trinta e quatro cliques). '1.5' cai fora daqui e segue sendo decimal.
        texto = texto.replace(".", "")
    try:
        v = float(texto)
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


def resumo_financeiro(usuario) -> dict:
    """Total de receita/comissão a exibir para este usuário.

    Cada sync grava um SNAPSHOT: o portal devolve o acumulado de uma janela (14
    dias), não uma série por dia, e a janela desliza — o sync de hoje e o de ontem
    cobrem quase o mesmo período. Somar os snapshots soma a mesma comissão de novo
    a cada dia (o dashboard fazia Sum sobre 30 dias de acumulados de 14 dias, e
    inflava a receita ~30x). Então lê-se só o snapshot mais recente de cada loja, e
    somam-se as linhas DENTRO dele — que aí sim são fatias distintas (por etiqueta
    ou produto) do mesmo período.
    """
    from django.db.models import Max, Min, Q, Sum

    ultimos = (
        ReceitaAfiliado.objects.filter(usuario=usuario)
        .values("marketplace").annotate(ultima=Max("data"))
    )
    filtro = Q(pk__in=[])
    for linha in ultimos:
        filtro |= Q(marketplace=linha["marketplace"], data=linha["ultima"])

    snapshot = ReceitaAfiliado.objects.filter(usuario=usuario).filter(filtro)
    dados = snapshot.aggregate(
        pedidos=Sum("pedidos"), receita=Sum("receita"), comissao=Sum("comissao"),
        cliques_mkt=Sum("cliques"), conversoes=Sum("conversoes"),
        # Período que os números de fato cobrem — a tela precisa dizer isso, senão
        # o usuário lê um acumulado de 14 dias como se fosse de 30.
        periodo_inicio=Min("periodo_inicio"), periodo_fim=Max("periodo_fim"),
    )
    return dados


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
        if not url:
            raise ReportSyncNaoConfigurado(
                "Leitura automática do relatório do Mercado Livre ainda não "
                "configurada (defina ML_AFFILIATE_REPORT_URL)."
            )
        return _fetch_browser_report(usuario, self.marketplace, url, desde, ate)


class AmazonReportAdapter(BaseReportAdapter):
    marketplace = "amazon"

    def fetch(self, usuario, desde, ate) -> list[ReportRow]:
        perfil = getattr(usuario, "perfil", None)
        if not perfil or not perfil.amazon_conectado():
            raise ReportSyncActionRequired(
                "Conecte a Amazon Associates/Creators para sincronizar relatórios."
            )
        # _fetch_browser_report só sabe montar sessão do ML (auth_{id}.json). Para a
        # Amazon ele abria um contexto ANÔNIMO: o portal redirecionava pro login, o
        # parser via o campo de senha e concluía "sessão expirada, reconecte a conta"
        # — para uma conta conectada, sem nada que o usuário pudesse fazer a respeito.
        # Enquanto não existir sessão de relatórios da Amazon, dizemos a verdade.
        raise ReportSyncNaoConfigurado(
            "A Amazon ainda não tem leitura automática de relatórios: não guardamos "
            "sessão do portal de Associados. Acompanhe a comissão pelo painel da Amazon."
        )


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
            # Data do SNAPSHOT (quando lemos), não do faturamento: o portal devolve o
            # acumulado da janela periodo_inicio..periodo_fim. Ver resumo_financeiro.
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
    # Achamos a tabela mas não entendemos um único número dela: é o parser que está
    # errado, não a conta que faturou zero. Falhar aqui é o que impede o dashboard de
    # exibir R$ 0,00 com selo verde de "sincronizado" — foi assim que um _num que não
    # lia número brasileiro passou despercebido.
    if out and not any(r.cliques or r.pedidos or r.receita or r.comissao for r in out):
        raise ReportSyncError(
            f"{marketplace}: {len(out)} linha(s) lidas e nenhum número reconhecido; "
            "o parser precisa ser ajustado ao formato do portal."
        )
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
    except ReportSyncNaoConfigurado as exc:
        sync.status = "nao_configurado"
        sync.erro = str(exc)[:500]
        sync.ultimo_fim = timezone.now()
        # Sem retry curto: não é falha transitória, é uma feature que não existe.
        sync.proxima_execucao = timezone.now() + timedelta(days=1)
        sync.save()
        log_event("relatorios", "sync_nao_configurado", str(exc), level="info",
                  usuario=usuario, contexto={"marketplace": marketplace})
        return sync
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
    # order_by explícito: o [:limit] fatiava uma ordem indefinida, então acima de
    # `limit` usuários alguns podiam nunca ser sincronizados. select_related evita
    # uma query de perfil por usuário nos adapters.
    usuarios = (
        User.objects.filter(is_active=True, perfil__bloqueado=False)
        .select_related("perfil").order_by("id")[:limit]
    )
    resultados = []
    for usuario in usuarios:
        for marketplace in ADAPTERS:
            sync, _ = RelatorioSync.objects.get_or_create(
                usuario=usuario, marketplace=marketplace)
            if sync.proxima_execucao and sync.proxima_execucao > agora:
                continue
            resultados.append(sync_marketplace(usuario, marketplace))
    return resultados
