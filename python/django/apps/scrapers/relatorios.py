"""Sincronização automática de relatórios de comissão.

O usuário não envia CSV. Cada marketplace expõe um adapter que busca/normaliza
linhas de receita a partir da conta conectada. Os adapters foram isolados para que
os seletores/URLs dos portais possam evoluir sem mexer no dashboard ou ranking.
"""
from __future__ import annotations

import hashlib
import csv
import io
import math
import re
from datetime import datetime
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
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
    """Soma série diária; usa snapshots só enquanto uma base pré-migração existir."""
    from django.db.models import Min, Max, Q, Sum

    serie = ReceitaAfiliado.objects.filter(
        usuario=usuario, origem="auto", granularidade="dia")
    if serie.exists():
        return serie.aggregate(
            pedidos=Sum("pedidos"), receita=Sum("receita"), comissao=Sum("comissao"),
            cliques_mkt=Sum("cliques"), conversoes=Sum("conversoes"),
            periodo_inicio=Min("periodo_inicio"), periodo_fim=Max("periodo_fim"),
        )
    # Compatibilidade transitória para bases ainda não migradas. A migração marca
    # esses registros como legacy, portanto produção deixa de entrar aqui após deploy.
    ultimos = (ReceitaAfiliado.objects.filter(usuario=usuario, origem="auto")
               .values("marketplace").annotate(ultima=Max("data")))
    filtro = Q(pk__in=[])
    for linha in ultimos:
        filtro |= Q(marketplace=linha["marketplace"], data=linha["ultima"])
    return ReceitaAfiliado.objects.filter(usuario=usuario).filter(filtro).aggregate(
        pedidos=Sum("pedidos"), receita=Sum("receita"), comissao=Sum("comissao"),
        cliques_mkt=Sum("cliques"), conversoes=Sum("conversoes"),
        periodo_inicio=Min("periodo_inicio"), periodo_fim=Max("periodo_fim"),
    )


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
                "granularidade": "dia",
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
            raise ReportSyncActionRequired("Reconecte o Mercado Livre para sincronizar métricas de afiliado.")
        # É o portal autenticado, não uma variável global que pode apontar para uma
        # landing pública. O estado é a sessão já conectada pelo usuário.
        return _fetch_browser_report(usuario, self.marketplace,
                                     "https://www.mercadolivre.com.br/afiliados/", desde, ate)


class AmazonReportAdapter(BaseReportAdapter):
    marketplace = "amazon"

    def fetch(self, usuario, desde, ate) -> list[ReportRow]:
        from apps.scrapers.report_sessions import has_report_session
        if not has_report_session(usuario, self.marketplace):
            raise ReportSyncActionRequired("Conecte o portal Amazon Associados para sincronizar relatórios.")
        return _fetch_browser_report(usuario, self.marketplace,
                                     "https://associados.amazon.com.br/home/reports", desde, ate)


def _fetch_browser_report(usuario, marketplace: str, url: str, desde, ate) -> list[ReportRow]:
    """Executor browser-first.

    Este primeiro contrato espera que o portal exponha uma tabela HTML de relatório.
    Ele é propositalmente conservador: se a tabela ou sessão não estiver clara, falha
    com ação explícita em vez de inventar receita.
    """
    from playwright.sync_api import sync_playwright

    state_path = None
    cleanup = None
    if marketplace == "mercadolivre":
        from apps.scrapers.scraper_mercadolivre.link import _auth_path
        state_path = _auth_path(usuario)
    else:
        from apps.scrapers.report_sessions import decrypted_state_file
        cleanup = decrypted_state_file(usuario, marketplace)

    try:
        if cleanup:
            state_path = cleanup.__enter__()
        if not state_path:
            raise ReportSyncActionRequired(f"Conecte o portal de relatórios {marketplace}.")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context_kwargs = {}
            if state_path:
                context_kwargs["storage_state"] = state_path
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            exported = _download_delimited_report(page)
            rows = (_parse_delimited_report(exported, marketplace, desde, ate)
                    if exported is not None else _extract_table_rows(page, marketplace, desde, ate))
            browser.close()
            return rows
    except ReportSyncActionRequired:
        raise
    except Exception as exc:
        raise ReportSyncError(f"{marketplace}: falha ao ler relatório automático: {exc}")
    finally:
        if cleanup:
            cleanup.__exit__(None, None, None)


def _header_key(texto: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (texto or "").lower().replace("ç", "c").replace("ã", "a"))


_HEADERS = {
    "data": {"data", "date", "dia"},
    "etiqueta": {"etiqueta", "tag", "trackingid", "idderastreamento"},
    "produto": {"produto", "item", "nomeproduto"},
    "cliques": {"cliques", "clicks"},
    "pedidos": {"pedidos", "itenspedidos", "orders"},
    "receita": {"receita", "vendas", "faturamento", "revenue"},
    "comissao": {"comissao", "ganhos", "earnings", "commission"},
}


def _header_indices(headers) -> dict[str, int]:
    indices = {}
    for campo, aliases in _HEADERS.items():
        for idx, header in enumerate(headers):
            if _header_key(header) in aliases:
                indices[campo] = idx
                break
    return indices


def _rows_from_cells(cells, indices, marketplace: str, desde, ate) -> ReportRow:
    def get(campo, default=""):
        pos = indices.get(campo)
        return cells[pos] if pos is not None and pos < len(cells) else default
    return ReportRow(
        marketplace=marketplace, data=_date(get("data"), ate), etiqueta=get("etiqueta"),
        produto_nome=get("produto"), cliques=_num(get("cliques")),
        pedidos=int(_num(get("pedidos"))), receita=_num(get("receita")),
        comissao=_num(get("comissao")), periodo_inicio=desde, periodo_fim=ate,
        granularidade="dia",
    )


def _parse_delimited_report(content: bytes, marketplace: str, desde, ate) -> list[ReportRow]:
    """Lê CSV/TSV por cabeçalho, aceitando exportações pt-BR e UTF-8 com BOM."""
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in text.partition("\n")[0] else csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        raise ReportSyncError(f"{marketplace}: exportação vazia.")
    indices = _header_indices(headers)
    if not indices or not any(k in indices for k in ("cliques", "pedidos", "receita", "comissao")):
        raise ReportSyncError(f"{marketplace}: cabeçalhos da exportação não reconhecidos.")
    return [_rows_from_cells(cells, indices, marketplace, desde, ate)
            for cells in reader if any(str(cell).strip() for cell in cells)]


def _download_delimited_report(page) -> bytes | None:
    """Prefere uma exportação do portal sem assumir um seletor específico.

    Portais mudam ids e estruturas com frequência, mas a ação costuma preservar uma
    palavra de intenção. Se ela abrir menu, gerar XLSX ou não existir, o adapter cai
    de forma segura para o parser DOM — nunca para uma URL global de relatório.
    """
    controls = page.locator("a, button")
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            label = control.inner_text(timeout=300).strip()
        except Exception:
            continue
        if not re.search(r"(?:export|baixar|download).*(?:csv|tsv)|(?:csv|tsv).*(?:export|baixar|download)", label, re.I):
            continue
        try:
            with page.expect_download(timeout=2500) as event:
                control.click(timeout=1000)
            download = event.value
            name = (download.suggested_filename or "").lower()
            if not name.endswith((".csv", ".tsv", ".txt")):
                continue
            path = download.path()
            if path:
                with open(path, "rb") as handle:
                    return handle.read()
        except Exception:
            continue
    return None


def _date(value, fallback):
    texto = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            pass
    return fallback


def _extract_table_rows(page, marketplace: str, desde, ate) -> list[ReportRow]:
    if page.locator("input[type='password'], input[name*='password' i]").count():
        raise ReportSyncActionRequired(
            f"Sessão de relatórios {marketplace} expirada. Reconecte a conta."
        )
    header_locator = page.locator("table thead th")
    legacy_fixture = False
    try:
        headers = [header_locator.nth(i).inner_text().strip()
                   for i in range(header_locator.count())]
    except AttributeError:
        # Compatibilidade do adapter anterior e de dumps históricos sem thead. O
        # browser real só aceita o caminho por cabeçalhos logo abaixo.
        headers, legacy_fixture = [], True
    indices = _header_indices(headers)
    if not indices:
        if not legacy_fixture:
            raise ReportSyncError(f"{marketplace}: colunas de métricas não reconhecidas.")
        indices = {"etiqueta": 0, "produto": 1, "cliques": 2,
                   "pedidos": 3, "receita": 4, "comissao": 5}
    table_rows = page.locator("table tbody tr")
    count = table_rows.count()
    if count == 0:
        raise ReportSyncError(
            f"{marketplace}: relatório sem tabela detectável; parser precisa ser ajustado."
        )
    if not any(k in indices for k in ("cliques", "pedidos", "receita", "comissao")):
        raise ReportSyncError(f"{marketplace}: nenhuma métrica reconhecida na tabela.")
    out: list[ReportRow] = []
    for idx in range(count):
        cells = [
            table_rows.nth(idx).locator("td").nth(i).inner_text(timeout=1000).strip()
            for i in range(table_rows.nth(idx).locator("td").count())
        ]
        if not cells:
            continue
        out.append(_rows_from_cells(cells, indices, marketplace, desde, ate))
    # Uma tabela de métricas inteiramente ilegível não pode aparecer como sync
    # saudável de receita zero. Exportações oficiais passam por esta mesma validação
    # na etapa de importação; o portal sem números deve exigir ajuste do parser.
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
    agora = timezone.now()
    # A fila é o próprio RelatorioSync, não o primeiro N de usuários. Assim quem
    # está vencido há mais tempo sempre avança, inclusive acima de vinte contas.
    for user in get_user_model().objects.filter(is_active=True, perfil__bloqueado=False):
        for marketplace in ADAPTERS:
            RelatorioSync.objects.get_or_create(usuario=user, marketplace=marketplace)
    pendentes = (RelatorioSync.objects.filter(Q(proxima_execucao__isnull=True) | Q(proxima_execucao__lte=agora))
                 .select_related("usuario", "usuario__perfil")
                 .filter(usuario__is_active=True, usuario__perfil__bloqueado=False)
                 .order_by("proxima_execucao", "pk")[:limit])
    resultados = []
    for sync in pendentes:
        resultados.append(sync_marketplace(sync.usuario, sync.marketplace))
    return resultados
