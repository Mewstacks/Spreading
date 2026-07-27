"""Fila durável e executor das raspagens manuais de ofertas e cupons.

O processo web somente cria/consulta ``ExecucaoRaspagem``. O trabalho pesado roda
no processo ``scrape`` (que já possui contexto de sistema e advisory lock), sem
segurar a transação RLS da request durante Playwright.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic, sleep

import requests
from django.db import (
    DatabaseError, IntegrityError, OperationalError, connection, connections,
    transaction,
)
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts.tenant import executar_no_tenant, system_context
from apps.scrapers.models import (
    EventoRaspagem, ExecucaoRaspagem, LinkAfiliadoUsuario, Produto,
)
from apps.scrapers.progresso import usar_reporter


logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
STALE_SECONDS = 90
MAX_ATTEMPTS = 2
MAX_JOB_SECONDS = 45 * 60
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "partial", "failed")

_PROGRESS_PREFIX = re.compile(r"^\[PROGRESSO\]\s*")
_ERRORS = {
    "session_expired": (
        "A sessão do Mercado Livre expirou.",
        "Reconecte o Mercado Livre e inicie uma nova raspagem.",
    ),
    "browser_unavailable": (
        "Não foi possível abrir o navegador de coleta.",
        "Tente novamente; se persistir, verifique o worker e o Chromium.",
    ),
    "source_timeout": (
        "O Mercado Livre demorou demais para responder.",
        "Os dados anteriores foram preservados. Tente novamente em alguns minutos.",
    ),
    "database_unavailable": (
        "O banco de dados ficou temporariamente indisponível.",
        "A execução tentou se recuperar automaticamente. Tente novamente se necessário.",
    ),
    "parser_changed": (
        "O formato da página de origem mudou e a coleta não pôde ser concluída.",
        "Os dados anteriores foram preservados; revise os seletores do scraper.",
    ),
    "worker_interrupted": (
        "O serviço de raspagem foi interrompido duas vezes.",
        "Verifique o worker de raspagem antes de tentar novamente.",
    ),
    "unknown": (
        "Ocorreu uma falha inesperada durante a raspagem.",
        "Os dados anteriores foram preservados. Consulte a Saúde se o erro persistir.",
    ),
}


def classificar_erro(exc: Exception) -> str:
    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
    from apps.scrapers.scraper_mercadolivre.link import AuthError, LoginError

    if isinstance(exc, (LoginError, AuthError, SessaoExpirada)):
        return "session_expired"
    if isinstance(exc, (OperationalError, DatabaseError)):
        return "database_unavailable"
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "source_timeout"
    if isinstance(exc, BrowserError):
        return "browser_unavailable"
    texto = str(exc).lower()
    if any(token in texto for token in ("payload", "seletor", "selector", "nordic", "parser")):
        return "parser_changed"
    if any(token in texto for token in ("timeout", "timed out", "demorou")):
        return "source_timeout"
    return "unknown"


def _erro_publico(codigo: str) -> tuple[str, str]:
    return _ERRORS.get(codigo, _ERRORS["unknown"])


def criar_execucao(*, organization, usuario, tipo: str):
    """Cria um pedido ou devolve o pedido ativo da organização sem duplicá-lo."""
    if tipo not in {"ofertas", "cupons"}:
        raise ValueError("Tipo de raspagem inválido.")
    try:
        with transaction.atomic():
            return ExecucaoRaspagem.objects.create(
                organization=organization,
                solicitada_por=usuario,
                tipo=tipo,
                etapa="Aguardando o worker",
            ), True
    except IntegrityError:
        ativo = ExecucaoRaspagem.objects.filter(
            organization=organization, status__in=ACTIVE,
        ).order_by("-criada_em").first()
        if ativo is None:
            raise
        return ativo, False


def serializar_execucao(execucao: ExecucaoRaspagem, *, after=0) -> dict:
    eventos = list(
        execucao.eventos.filter(id__gt=max(0, int(after or 0)))
        .values("id", "nivel", "etapa", "mensagem", "progresso", "criado_em")[:250]
    )
    return {
        "id": str(execucao.pk),
        "tipo": execucao.tipo,
        "status": execucao.status,
        "etapa": execucao.etapa,
        "progresso": execucao.progresso,
        "contagens": execucao.contagens or {},
        "tentativas": execucao.tentativas,
        "criada_em": execucao.criada_em.isoformat() if execucao.criada_em else None,
        "iniciada_em": execucao.iniciada_em.isoformat() if execucao.iniciada_em else None,
        "finalizada_em": (
            execucao.finalizada_em.isoformat() if execucao.finalizada_em else None
        ),
        "erro": {
            "codigo": execucao.codigo_erro,
            "mensagem": execucao.erro_publico,
            "acao": execucao.acao_recomendada,
        } if execucao.codigo_erro else None,
        "eventos": [
            {
                **evento,
                "criado_em": evento["criado_em"].isoformat(),
            }
            for evento in eventos
        ],
        "ultimo_evento_id": eventos[-1]["id"] if eventos else int(after or 0),
    }


def _db_retry(fn, *, attempts=3):
    """Reabre conexão e transação a cada tentativa; nunca recicla atomic quebrado."""
    ultimo = None
    for index, delay in enumerate((0, 0.5, 1.5)[:attempts]):
        if delay:
            sleep(delay)
        connections.close_all()
        try:
            return fn()
        except (OperationalError, DatabaseError) as exc:
            ultimo = exc
            logger.warning(
                "Banco indisponível na etapa de raspagem (%s/%s): %s",
                index + 1, attempts, exc,
            )
            connections.close_all()
    raise ultimo


class JobReporter:
    def __init__(self, job_id):
        self.job_id = job_id
        self.started = monotonic()
        self.progress = 0
        self.stage = ""
        self.counts = {}

    def _persist(self, *, message=None, level="info", progress=None):
        if monotonic() - self.started > MAX_JOB_SECONDS:
            raise TimeoutError("Prazo total de 45 minutos excedido.")
        if progress is not None:
            self.progress = max(self.progress, min(99, int(progress)))

        def operation():
            job = ExecucaoRaspagem.objects.get(pk=self.job_id)
            updates = {
                "heartbeat_em": timezone.now(),
                "etapa": self.stage,
                "progresso": self.progress,
                "contagens": self.counts,
            }
            ExecucaoRaspagem.objects.filter(pk=self.job_id).update(**updates)
            if message:
                EventoRaspagem.objects.create(
                    execucao=job,
                    organization_id=job.organization_id,
                    nivel=level,
                    etapa=self.stage,
                    mensagem=str(message)[:500],
                    progresso=self.progress if progress is not None else None,
                )

        # Callbacks de progresso podem ser disparados dentro do greenlet usado
        # pelo Playwright sync. Nesse ponto qualquer ORM/close() direto levanta
        # SynchronousOnlyOperation. A ponte executa toda a reconexão + gravação
        # numa thread sem event loop e reinstala explicitamente o contexto de
        # sistema do worker.
        executar_no_tenant(lambda: _db_retry(operation))

    def emit(self, message, *, progresso=None, level="info"):
        texto = _PROGRESS_PREFIX.sub("", str(message or "")).strip()
        if not texto:
            return
        if texto.lower().startswith("aviso:") and level == "info":
            level = "warning"
        self._persist(message=texto, level=level, progress=progresso)

    def callback(self, faixa):
        inicio, fim = faixa

        def report(message, *, progresso=None, level="info"):
            mapped = None
            if progresso is not None:
                mapped = int(inicio + (fim - inicio) * max(0, min(100, progresso)) / 100)
            self.emit(message, progresso=mapped, level=level)

        return report

    def step(self, stage, progress, message=None):
        self.stage = stage
        self._persist(
            message=message or stage,
            progress=progress,
        )

    def count(self, **values):
        self.counts.update({k: int(v or 0) for k, v in values.items()})
        self._persist()

    def warning(self, message):
        self.emit(message, level="warning")

    def finish(self, status, *, code="", message=""):
        now = timezone.now()
        public, action = _erro_publico(code) if code else ("", "")
        if message and not code:
            public = message

        def operation():
            ExecucaoRaspagem.objects.filter(pk=self.job_id).update(
                status=status,
                etapa={
                    "succeeded": "Concluída",
                    "partial": "Concluída com avisos",
                }.get(status, self.stage or "Processamento"),
                progresso=100 if status == "succeeded" else self.progress,
                contagens=self.counts,
                codigo_erro=code,
                erro_publico=public,
                acao_recomendada=action,
                heartbeat_em=now,
                finalizada_em=now,
            )
            job = ExecucaoRaspagem.objects.get(pk=self.job_id)
            EventoRaspagem.objects.create(
                execucao=job,
                organization_id=job.organization_id,
                nivel=("success" if status == "succeeded" else
                       "warning" if status == "partial" else "error"),
                etapa=job.etapa,
                mensagem=(
                    "Raspagem concluída."
                    if status == "succeeded"
                    else public or "Raspagem concluída com avisos."
                ),
                progresso=100 if status == "succeeded" else self.progress,
            )

        _db_retry(operation)


@contextmanager
def _heartbeat(job_id):
    stop = threading.Event()

    def pulse():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with system_context():
                    _db_retry(lambda: ExecucaoRaspagem.objects.filter(
                        pk=job_id, status="running",
                    ).update(heartbeat_em=timezone.now()))
            except Exception:
                logger.exception("Falha ao atualizar heartbeat da raspagem %s", job_id)

    thread = threading.Thread(
        target=pulse, daemon=True, name=f"heartbeat-raspagem-{job_id}",
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def _produtos_sem_link(usuario, *, origens, limite):
    ja_tem = LinkAfiliadoUsuario.objects.filter(
        usuario=usuario, produto=OuterRef("pk"),
    ).exclude(link_afiliado="")
    return list(
        Produto.objects
        .filter(Q(owner__isnull=True) | Q(owner=usuario), origem__in=origens)
        .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
        .exclude(Exists(ja_tem))
        .order_by("-ultima_observacao")[:limite]
    )


def _gerar_links(usuario, *, origens, limite, faixa, reporter):
    from apps.scrapers.marketplaces.registry import get_marketplace

    pendentes = _db_retry(
        lambda: _produtos_sem_link(usuario, origens=origens, limite=limite),
    )
    if not pendentes:
        reporter.emit("Todos os itens coletados já possuem link de afiliado.")
        return 0, 0
    reporter.emit(f"Gerando links para {len(pendentes)} item(ns).")
    gerados = falhas = 0
    grupos = {}
    for produto in pendentes:
        grupos.setdefault(produto.marketplace or "mercadolivre", []).append(produto)
    for slug, produtos in grupos.items():
        ok, erros = get_marketplace(slug).prefetch_links(
            produtos, usuario=usuario, faixa=faixa,
        )
        gerados += ok
        falhas += erros
    return gerados, falhas


def _executar_ofertas(job, reporter):
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas

    warnings = []
    reporter.step("Coletando promoções", 3)
    with usar_reporter(reporter.callback((3, 70))):
        total = mapear_ofertas(
            max_paginas=40, usuario=job.solicitada_por,
        )
    reporter.count(ofertas=total)
    if not total:
        warnings.append("A fonte não trouxe promoções; o catálogo anterior foi preservado.")
        reporter.warning(warnings[-1])

    reporter.step("Gerando links de afiliado", 72)
    try:
        with usar_reporter(reporter.emit):
            gerados, falhas = _gerar_links(
                job.solicitada_por,
                origens=("oferta",),
                limite=60,
                faixa=(72, 98),
                reporter=reporter,
            )
        reporter.count(links_gerados=gerados, links_falhos=falhas)
        if falhas:
            warnings.append(f"{falhas} link(s) não puderam ser gerados.")
            reporter.warning(warnings[-1])
    except Exception as exc:
        codigo = classificar_erro(exc)
        warnings.append(f"Links de afiliado: {_erro_publico(codigo)[0]}")
        reporter.warning(warnings[-1])
    reporter.finish("partial" if warnings else "succeeded")


def _executar_cupons(job, reporter):
    from apps.scrapers.conexoes import estado_ml
    from apps.scrapers.coupon_products import preparar_lote
    from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
        mapear_cupons_codigo,
    )
    from apps.scrapers.scraper_mercadolivre.scraper import (
        mapear_cupons, projetar_catalogo_cupons,
    )
    from apps.scrapers.sources import run_source
    from apps.scrapers.sources.persistence import persist_items

    warnings = []
    reporter.step("Validando conexão", 2)
    estado = estado_ml(job.solicitada_por)
    if not estado.conectado:
        reporter.finish("failed", code="session_expired")
        return

    campanha = checkout = oficiais = 0
    reporter.step("Coletando campanhas", 5)
    try:
        with usar_reporter(reporter.emit):
            campanha = mapear_cupons(
                faixa=(5, 35), usuario=job.solicitada_por,
            )
        reporter.count(campanhas=campanha)
    except Exception as exc:
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Campanhas: {warnings[-1]}")

    reporter.step("Coletando cupons de checkout", 38)
    try:
        with usar_reporter(reporter.emit):
            checkout = mapear_cupons_codigo(
                faixa=(38, 55), usuario=job.solicitada_por,
            )
        reporter.count(produtos_checkout=checkout)
    except Exception as exc:
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Checkout: {warnings[-1]}")

    reporter.step("Atualizando catálogo de cupons", 58)
    try:
        projetados = _db_retry(lambda: projetar_catalogo_cupons(faixa=(58, 65)))
        reporter.count(campanhas_projetadas=projetados)
    except Exception as exc:
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Catálogo: {warnings[-1]}")

    reporter.step("Consultando cupons oficiais", 67)
    try:
        fonte = run_source("ml-cupons-afiliados")
        if fonte.get("status") == "error":
            raise RuntimeError(
                fonte.get("error") or "A fonte oficial de cupons falhou."
            )
        persistidos = _db_retry(
            lambda: persist_items(fonte.get("coupons", [])),
        )
        oficiais = persistidos["coupons"]
        reporter.count(cupons_oficiais=oficiais)
    except Exception as exc:
        warnings.append(f"Fonte oficial: {_erro_publico(classificar_erro(exc))[0]}")
        reporter.warning(warnings[-1])

    reporter.step("Preparando produtos dos cupons", 75)
    try:
        preparo = _db_retry(lambda: preparar_lote(limite=max(12, oficiais)))
        reporter.count(
            cupons_processados=preparo["processados"],
            cupons_prontos=preparo["prontos"],
        )
    except Exception as exc:
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Preparação: {warnings[-1]}")

    reporter.step("Gerando links de afiliado", 84)
    try:
        with usar_reporter(reporter.emit):
            gerados, falhas = _gerar_links(
                job.solicitada_por,
                origens=("cupom", "cupom_codigo"),
                limite=80,
                faixa=(84, 98),
                reporter=reporter,
            )
        reporter.count(links_gerados=gerados, links_falhos=falhas)
        if falhas:
            warnings.append(f"{falhas} link(s) de cupom não puderam ser gerados.")
            reporter.warning(warnings[-1])
    except Exception as exc:
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Links: {warnings[-1]}")

    if not any((campanha, checkout, oficiais)) and warnings:
        reporter.finish("failed", code="unknown")
    else:
        if not any((campanha, checkout, oficiais)):
            warnings.append("Nenhum cupom foi encontrado; dados anteriores preservados.")
            reporter.warning(warnings[-1])
        reporter.finish("partial" if warnings else "succeeded")


def executar_job(job: ExecucaoRaspagem):
    # O executor é uma operação cross-tenant auditável. O management command já
    # instala este contexto, mas mantê-lo como invariante da função evita que uma
    # chamada direta (teste, comando de recuperação ou manutenção) deixe o
    # reporter sem escopo ao atravessar a ponte para fora do Playwright.
    with system_context():
        reporter = JobReporter(job.pk)
        try:
            if job.solicitada_por_id is None:
                raise RuntimeError("Usuário solicitante não existe mais.")
            with _heartbeat(job.pk):
                if job.tipo == "ofertas":
                    _executar_ofertas(job, reporter)
                else:
                    _executar_cupons(job, reporter)
        except Exception as exc:
            codigo = classificar_erro(exc)
            logger.exception(
                "Raspagem manual %s (%s) falhou", job.pk, job.tipo,
            )
            try:
                from apps.scrapers.eventos import log_event

                mensagem_publica, _acao = _erro_publico(codigo)
                log_event(
                    "scraper",
                    "scrape_erro",
                    mensagem_publica,
                    level="error",
                    usuario=job.solicitada_por,
                    contexto={
                        "marketplace": "mercadolivre",
                        "tipo_raspagem": job.tipo,
                        "codigo": codigo,
                        "execucao_id": str(job.pk),
                    },
                    exc=exc,
                )
            except Exception:
                logger.exception(
                    "Não foi possível registrar a raspagem %s na Saúde", job.pk,
                )
            try:
                reporter.finish("failed", code=codigo)
            except Exception:
                logger.exception(
                    "Não foi possível persistir a falha da raspagem %s", job.pk,
                )


def _recuperar_interrompidos(now):
    stale = now - timedelta(seconds=STALE_SECONDS)
    interrompidos = list(
        ExecucaoRaspagem.objects.select_for_update()
        .filter(status="running", heartbeat_em__lt=stale)
    )
    for job in interrompidos:
        if job.tentativas < MAX_ATTEMPTS:
            job.status = "queued"
            job.etapa = "Retomando após interrupção"
            job.heartbeat_em = None
            job.save(update_fields=("status", "etapa", "heartbeat_em"))
            EventoRaspagem.objects.create(
                execucao=job, organization_id=job.organization_id,
                nivel="warning", etapa=job.etapa,
                mensagem="O worker foi interrompido; a raspagem será retomada.",
            )
        else:
            message, action = _erro_publico("worker_interrupted")
            job.status = "failed"
            job.etapa = "Retomada após interrupção"
            job.codigo_erro = "worker_interrupted"
            job.erro_publico = message
            job.acao_recomendada = action
            job.finalizada_em = now
            job.save(update_fields=(
                "status", "etapa", "codigo_erro", "erro_publico",
                "acao_recomendada", "finalizada_em",
            ))
            EventoRaspagem.objects.create(
                execucao=job, organization_id=job.organization_id,
                nivel="error", etapa=job.etapa, mensagem=message,
                progresso=job.progresso,
            )


def claim_next_job():
    now = timezone.now()
    with transaction.atomic():
        _recuperar_interrompidos(now)
        queryset = ExecucaoRaspagem.objects.filter(
            status="queued",
        ).order_by("criada_em")
        if connection.features.has_select_for_update_skip_locked:
            queryset = queryset.select_for_update(skip_locked=True)
        else:
            queryset = queryset.select_for_update()
        # ``solicitada_por`` aceita NULL. Em PostgreSQL, combinar o LEFT OUTER
        # JOIN produzido por select_related() com SELECT FOR UPDATE tenta
        # bloquear também o lado anulável e falha com:
        # "FOR UPDATE cannot be applied to the nullable side of an outer join".
        # O claim só precisa bloquear a linha do job; o usuário é carregado
        # normalmente depois que esta transação curta termina.
        job = queryset.first()
        if job is None:
            return None
        job.status = "running"
        job.etapa = "Iniciando"
        job.iniciada_em = job.iniciada_em or now
        job.heartbeat_em = now
        job.tentativas += 1
        job.codigo_erro = ""
        job.erro_publico = ""
        job.acao_recomendada = ""
        job.save(update_fields=(
            "status", "etapa", "iniciada_em", "heartbeat_em", "tentativas",
            "codigo_erro", "erro_publico", "acao_recomendada",
        ))
        EventoRaspagem.objects.create(
            execucao=job, organization_id=job.organization_id,
            nivel="info", etapa=job.etapa,
            mensagem=f"Raspagem de {job.get_tipo_display().lower()} iniciada.",
            progresso=0,
        )
        return job


def processar_proximo_job() -> bool:
    job = _db_retry(claim_next_job)
    if job is None:
        return False
    executar_job(job)
    return True


def existe_job_pendente() -> bool:
    return ExecucaoRaspagem.objects.filter(status__in=ACTIVE).exists()
