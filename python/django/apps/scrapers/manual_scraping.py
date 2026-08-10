"""Fila durável e executor das raspagens manuais de ofertas e cupons.

O processo web somente cria/consulta ``ExecucaoRaspagem``. O trabalho pesado roda
no processo ``manual`` e adquire um lease observável de ``django_chromium``, sem
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
from django.db.models import Prefetch
from django.conf import settings
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
    "worker_unavailable": (
        "O worker de raspagem manual não respondeu dentro do limite.",
        "Verifique o processo manual no Fly.io e tente novamente.",
    ),
    "queue_timeout": (
        "A raspagem não obteve o recurso de navegador dentro do limite de espera.",
        "Verifique a fila e o consumo de Chromium antes de tentar novamente.",
    ),
    "execution_timeout": (
        "A raspagem excedeu o limite operacional de 45 minutos.",
        "Verifique a origem e o Chromium antes de iniciar uma nova raspagem.",
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
            job = ExecucaoRaspagem.objects.create(
                organization=organization,
                solicitada_por=usuario,
                tipo=tipo,
                etapa="Aguardando o worker",
                motivo_espera="worker_absent",
                espera_iniciada_em=timezone.now(),
                recurso_esperado="django_chromium",
                deadline_em=(
                    timezone.now()
                    + timedelta(seconds=settings.MANUAL_QUEUE_MAX_WAIT_SECONDS)
                ),
            )
            EventoRaspagem.objects.create(
                execucao=job, organization=organization,
                nivel="info", etapa="Fila:worker_absent",
                mensagem="Execução criada; aguardando heartbeat do worker manual.",
                progresso=0,
            )
            return job, True
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
        "fila": {
            "posicao": execucao.posicao_fila,
            "motivo": execucao.motivo_espera,
            "espera_iniciada_em": (
                execucao.espera_iniciada_em.isoformat()
                if execucao.espera_iniciada_em else None
            ),
            "worker": execucao.worker_id or None,
            "recurso": execucao.recurso_esperado or None,
            "lock_owner_tipo": execucao.lock_owner_tipo or None,
            "deadline_em": execucao.deadline_em.isoformat() if execucao.deadline_em else None,
            "eta_min_em": execucao.eta_min_em.isoformat() if execucao.eta_min_em else None,
            "eta_max_em": execucao.eta_max_em.isoformat() if execucao.eta_max_em else None,
            "eta_amostra": execucao.eta_amostra,
        },
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
        # ``contagens`` já é JSON: além dos totais, preserva o diagnóstico por
        # fonte/marketplace sem exigir migração.
        self.counts.update({
            key: value if isinstance(value, (dict, list, str)) else int(value or 0)
            for key, value in values.items()
        })
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


# Teto de tempo da etapa de links. MAX_JOB_SECONDS é 45min e a coleta já consumiu
# parte dele; sem um teto próprio, uma fila de milhares de itens levaria o job
# inteiro a estourar e a coleta que já deu certo seria reportada como falha.
SEGUNDOS_MAX_LINKS = 15 * 60
LOTE_LINKS = 40


def _gerar_links_ate_esvaziar(usuario, *, faixa, reporter):
    """Gera links em lotes até a fila acabar ou o teto de tempo chegar.

    Devolve (gerados, falhas, restantes). O limite fixo de 60 por execução era o
    motivo de a tela mostrar milhares de pendentes com um botão manual ao lado: a
    raspagem nunca alcançava a própria fila.

    Idempotente por construção — `_produtos_sem_link` exclui quem já tem link, então
    repetir a execução não regera nada nem duplica.
    """
    limite = monotonic() + SEGUNDOS_MAX_LINKS
    ini, fim = faixa
    gerados = falhas = 0
    vistos = 0
    while monotonic() < limite:
        pendentes = _db_retry(lambda: _produtos_sem_link(
            usuario, origens=("oferta", "busca"), limite=LOTE_LINKS))
        if not pendentes:
            break
        vistos += len(pendentes)
        reporter.emit(f"Gerando links para {len(pendentes)} item(ns).")
        grupos = {}
        for produto in pendentes:
            grupos.setdefault(produto.marketplace or "mercadolivre", []).append(produto)
        antes = gerados
        for slug, produtos in grupos.items():
            from apps.scrapers.marketplaces.registry import get_marketplace
            ok, erros = get_marketplace(slug).prefetch_links(
                produtos, usuario=usuario, faixa=(ini, fim))
            gerados += ok
            falhas += erros
        if gerados == antes:
            # Nenhum link saiu neste lote: o próximo traria os MESMOS produtos
            # (continuam sem link) e o laço giraria até o teto de tempo sem
            # progredir. Sessão caída e Link Builder desligado caem aqui.
            reporter.warning(
                "A geração de links não avançou neste lote; o restante fica para "
                "a próxima execução.")
            break
    restantes = _db_retry(lambda: Produto.objects.filter(
        Q(owner__isnull=True) | Q(owner=usuario), origem__in=("oferta", "busca"),
    ).exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"]).exclude(
        Exists(LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto=OuterRef("pk")).exclude(link_afiliado=""))
    ).count())
    if not vistos:
        reporter.emit("Todos os itens coletados já possuem link de afiliado.")
    return gerados, falhas, restantes


def _executar_ofertas(job, reporter):
    """Coleta de TODAS as lojas + geração dos links, numa operação só.

    Antes este job era só o feed do Mercado Livre e gerava 60 links; a Amazon
    entrava apenas pelo worker de fundo, e o resto da fila de links dependia de a
    pessoa achar e clicar um segundo botão noutra tela — com milhares pendentes.
    As etapas continuam separadas por dentro (cada uma reporta e falha sozinha),
    mas para quem clicou é um fluxo só.
    """
    from apps.scrapers.marketplaces.base import Marketplace, MarketplaceIndisponivel
    from apps.scrapers.marketplaces.registry import MARKETPLACES

    warnings = []
    coletado = 0
    consultadas = quebradas = 0
    ultimo_erro = None
    # Uma loja por vez, dividindo a faixa de progresso. Falha de uma NÃO derruba as
    # outras: catálogo parcial vale mais que nenhum, e a pessoa precisa saber qual
    # loja ficou de fora e por quê.
    # Loja sem coleta por usuário (Awin vive de feed) fica fora: contá-la como
    # consultada faria uma loja que não fez nada mascarar a quebra total das que
    # fizeram, e o job voltaria "parcial" com o catálogo inteiro no chão.
    lojas = [(slug, mp) for slug, mp in MARKETPLACES.items()
             if type(mp).scrape_para_usuario is not Marketplace.scrape_para_usuario]
    passo = 60 / max(1, len(lojas))
    for i, (slug, mp) in enumerate(lojas):
        inicio = 3 + int(i * passo)
        reporter.step(f"Coletando promoções ({slug})", inicio)
        try:
            with usar_reporter(reporter.callback((inicio, inicio + int(passo)))):
                total = mp.scrape_para_usuario(job.solicitada_por) or 0
            coletado += total
            consultadas += 1
            reporter.count(**{f"ofertas_{slug}": total})
        except MarketplaceIndisponivel as exc:
            # Loja que a pessoa não conectou não é defeito da raspagem: vira nota,
            # não aviso. Marcá-la como aviso deixaria "parcial" toda execução de
            # quem só usa o Mercado Livre. O motivo aparece assim mesmo — "0 itens"
            # escondia conta desconectada atrás do texto de "não há oferta".
            reporter.emit(f"{slug} não foi consultada — {exc}")
        except Exception as exc:
            logger.exception("Coleta de ofertas falhou em %s", slug)
            consultadas += 1
            quebradas += 1
            ultimo_erro = exc
            warnings.append(f"{slug}: {_erro_publico(classificar_erro(exc))[0]}")
            reporter.warning(warnings[-1])
    reporter.count(ofertas=coletado)
    # Toda loja consultada quebrou: isso é falha do job, não resultado parcial.
    # Reportar "parcial" aqui esconderia uma coleta totalmente quebrada atrás da
    # mesma cor de um ciclo que só não achou novidade — e nada chegaria à Saúde
    # da conta. Mesma regra do worker de fundo (automacao._rodar_scrape).
    if consultadas and quebradas == consultadas:
        raise ultimo_erro
    if not coletado:
        warnings.append("Nenhuma loja trouxe promoções; o catálogo anterior foi preservado.")
        reporter.warning(warnings[-1])

    reporter.step("Gerando links de afiliado", 66)
    try:
        with usar_reporter(reporter.emit):
            gerados, falhas, restantes = _gerar_links_ate_esvaziar(
                job.solicitada_por, faixa=(66, 98), reporter=reporter,
            )
        reporter.count(links_gerados=gerados, links_falhos=falhas,
                       links_restantes=restantes)
        if falhas:
            warnings.append(f"{falhas} link(s) não puderam ser gerados.")
            reporter.warning(warnings[-1])
        if restantes:
            # Não é falha: o teto de tempo existe para o job não estourar
            # MAX_JOB_SECONDS. Dizer quantos sobraram evita a impressão de que a
            # fila ficou parada — a próxima execução continua de onde parou.
            reporter.emit(f"{restantes} item(ns) ficaram para a próxima execução.")
    except Exception as exc:
        codigo = classificar_erro(exc)
        warnings.append(f"Links de afiliado: {_erro_publico(codigo)[0]}")
        reporter.warning(warnings[-1])
    reporter.finish("partial" if warnings else "succeeded")


def _executar_cupons(job, reporter):
    from apps.scrapers.conexoes import estado_ml
    from apps.scrapers.coupon_pipeline import executar_pipeline_cupons
    from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
        mapear_cupons_codigo,
    )
    from apps.scrapers.scraper_mercadolivre.scraper import (
        mapear_cupons, projetar_catalogo_cupons,
    )
    warnings = []
    reporter.step("Validando conexão", 2)
    estado = estado_ml(job.solicitada_por)
    campanha = checkout = projetados = 0
    if estado.conectado:
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

        reporter.step("Coletando vitrine de cupons", 38)
        try:
            with usar_reporter(reporter.emit):
                checkout = mapear_cupons_codigo(
                    faixa=(38, 55), usuario=job.solicitada_por,
                )
            reporter.count(produtos_checkout=checkout)
        except Exception as exc:
            warnings.append(_erro_publico(classificar_erro(exc))[0])
            reporter.warning(f"Vitrine: {warnings[-1]}")

        reporter.step("Atualizando catálogo interno", 58)
        try:
            projetados = _db_retry(
                lambda: projetar_catalogo_cupons(faixa=(58, 64)),
            )
            reporter.count(campanhas_projetadas=projetados)
        except Exception as exc:
            warnings.append(_erro_publico(classificar_erro(exc))[0])
            reporter.warning(f"Catálogo: {warnings[-1]}")
    else:
        # A sessão ML afeta só as duas fontes autenticadas. Cupons oficiais,
        # Amazon, Awin, feed e privados continuam avançando.
        warnings.append(
            "Mercado Livre desconectado: campanhas personalizadas e vitrine foram "
            "puladas; as demais fontes continuarão."
        )
        reporter.warning(warnings[-1])

    reporter.step("Coletando e preparando fontes habilitadas", 66)
    try:
        pipeline = _db_retry(lambda: executar_pipeline_cupons(
            usuarios=[job.solicitada_por],
            coletar=True,
            limite_preparo=40,
            limite_links=80,
            permitir_rede_preparo=estado.conectado,
        ))
        reporter.count(
            encontrados=pipeline["encontrados"],
            persistidos=pipeline["persistidos"],
            preparados=pipeline["preparados"],
            vinculados=pipeline["vinculados"],
            links_gerados=pipeline["links_gerados"],
            links_verificados=pipeline["links_verificados"],
            links_reprovados=pipeline["links_reprovados"],
            links_transitorios=pipeline["links_transitorios"],
            links_falhos=pipeline["links_falhos"],
            cupons_prontos=pipeline["prontos"],
            falhos=pipeline["falhos"],
            por_fonte=pipeline["fontes"],
            preparo_por_fonte=pipeline.get("preparo_por_fonte", {}),
        )
        for slug, diagnostico in pipeline["fontes"].items():
            motivo = diagnostico.get("motivo")
            if diagnostico.get("status") == "error":
                warnings.append(f"{slug}: {motivo or 'falha isolada da fonte'}")
                reporter.warning(warnings[-1])
            elif motivo:
                reporter.emit(f"{slug}: {motivo}")
        if pipeline["links_falhos"]:
            warnings.append(
                f"{pipeline['links_falhos']} link(s) de cupom falharam; "
                "o backoff foi preservado."
            )
            reporter.warning(warnings[-1])
    except Exception as exc:
        pipeline = None
        warnings.append(_erro_publico(classificar_erro(exc))[0])
        reporter.warning(f"Pipeline central: {warnings[-1]}")

    reporter.step("Concluindo diagnóstico", 99)
    houve_resultado = any((
        campanha, checkout, projetados,
        (pipeline or {}).get("encontrados", 0),
        (pipeline or {}).get("prontos", 0),
    ))
    if not houve_resultado:
        reporter.warning(
            "Nenhum cupom novo foi encontrado; todos os registros anteriores "
            "foram preservados."
        )
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
        .filter(status="running")
        .filter(
            Q(heartbeat_em__isnull=True) | Q(heartbeat_em__lt=stale)
            | Q(iniciada_em__lte=now - timedelta(seconds=MAX_JOB_SECONDS))
        )
    )
    for job in interrompidos:
        if job.iniciada_em and job.iniciada_em <= now - timedelta(seconds=MAX_JOB_SECONDS):
            message, action = _erro_publico("execution_timeout")
            job.status = "failed"
            job.etapa = "Execução encerrada"
            job.codigo_erro = "execution_timeout"
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
            continue
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


def recuperar_jobs_abandonados():
    with transaction.atomic():
        before = ExecucaoRaspagem.objects.filter(status="running").count()
        _recuperar_interrompidos(timezone.now())
        after = ExecucaoRaspagem.objects.filter(status="running").count()
    return max(0, before - after)


def claim_next_job(worker_id="", lease_token=""):
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
        job.worker_id = str(worker_id or "")[:120]
        job.motivo_espera = ""
        job.lock_owner_tipo = "manual"
        job.lease_token = str(lease_token or "")[:64]
        job.tentativas += 1
        job.codigo_erro = ""
        job.erro_publico = ""
        job.acao_recomendada = ""
        job.save(update_fields=(
            "status", "etapa", "iniciada_em", "heartbeat_em", "tentativas",
            "codigo_erro", "erro_publico", "acao_recomendada",
            "worker_id", "motivo_espera", "lock_owner_tipo", "lease_token",
        ))
        EventoRaspagem.objects.create(
            execucao=job, organization_id=job.organization_id,
            nivel="info", etapa=job.etapa,
            mensagem=f"Raspagem de {job.get_tipo_display().lower()} iniciada.",
            progresso=0,
        )
        return job


def processar_proximo_job(worker_id="", lease_token="") -> bool:
    job = _db_retry(lambda: claim_next_job(worker_id, lease_token))
    if job is None:
        return False
    executar_job(job)
    return True


def existe_job_pendente() -> bool:
    return ExecucaoRaspagem.objects.filter(status__in=ACTIVE).exists()


def _duracoes_recentes(tipo):
    jobs = list(
        ExecucaoRaspagem.objects.filter(
            tipo=tipo, status__in=("succeeded", "partial"),
            iniciada_em__isnull=False, finalizada_em__isnull=False,
        ).order_by("-finalizada_em").values_list("iniciada_em", "finalizada_em")[:30]
    )
    return sorted(max(1, int((fim - inicio).total_seconds())) for inicio, fim in jobs)


def atualizar_diagnostico_fila(*, resource_owner=""):
    """Atualiza posição/deadlines sem transformar ausência operacional em fila infinita."""
    from apps.scrapers.models import ResourceLease, WorkerHeartbeat

    now = timezone.now()
    stale = now - timedelta(seconds=STALE_SECONDS)
    worker = WorkerHeartbeat.objects.filter(
        worker_type="manual", heartbeat_at__gte=stale,
    ).order_by("-heartbeat_at").first()
    if not resource_owner:
        lease = ResourceLease.objects.filter(
            resource_key="django_chromium", expires_at__gt=now,
        ).only("owner_kind").first()
        resource_owner = lease.owner_kind if lease else ""
    queued = list(
        ExecucaoRaspagem.objects.filter(status="queued").order_by("criada_em", "pk")
    )
    for position, job in enumerate(queued, 1):
        waited = (now - job.criada_em).total_seconds()
        code = ""
        if not worker and waited >= settings.MANUAL_QUEUE_NO_WORKER_TIMEOUT_SECONDS:
            code = "worker_unavailable"
        elif job.deadline_em and job.deadline_em <= now:
            code = "queue_timeout"
        if code:
            message, action = _erro_publico(code)
            ExecucaoRaspagem.objects.filter(pk=job.pk, status="queued").update(
                status="failed", etapa="Fila encerrada", codigo_erro=code,
                erro_publico=message, acao_recomendada=action, finalizada_em=now,
                posicao_fila=None,
            )
            EventoRaspagem.objects.create(
                execucao=job, organization_id=job.organization_id,
                nivel="error", etapa="Fila encerrada", mensagem=message,
            )
            continue

        reason = "worker_absent" if not worker else (
            "previous_job" if position > 1 else (
                "resource_busy" if resource_owner else "waiting_worker"
            )
        )
        if job.motivo_espera != reason:
            EventoRaspagem.objects.create(
                execucao=job, organization_id=job.organization_id,
                nivel="info", etapa=f"Fila:{reason}",
                mensagem=(
                    f"Motivo de espera atualizado para {reason}; "
                    f"posição={position}; recurso=django_chromium."
                ),
                progresso=job.progresso,
            )
        durations = _duracoes_recentes(job.tipo)
        eta_min = eta_max = None
        if len(durations) >= 10:
            median = durations[len(durations) // 2]
            p90 = durations[min(len(durations) - 1, int(len(durations) * .9))]
            eta_min = now + timedelta(seconds=median * position)
            eta_max = now + timedelta(seconds=p90 * position)
        ExecucaoRaspagem.objects.filter(pk=job.pk, status="queued").update(
            posicao_fila=position,
            motivo_espera=reason,
            espera_iniciada_em=job.espera_iniciada_em or job.criada_em,
            worker_id=worker.worker_id if worker else "",
            recurso_esperado="django_chromium",
            lock_owner_tipo=str(resource_owner or "")[:40],
            eta_min_em=eta_min, eta_max_em=eta_max, eta_amostra=len(durations),
        )
        if waited >= 5 * 60 and not EventoRaspagem.objects.filter(
                execucao=job, etapa="Espera excedida").exists():
            EventoRaspagem.objects.create(
                execucao=job, organization_id=job.organization_id,
                nivel="warning", etapa="Espera excedida",
                mensagem=(
                    f"Espera superior a 5 minutos; motivo={reason}; "
                    f"recurso=django_chromium; posição={position}."
                ),
            )
    return len(queued)


def queue_wait_metrics(*, since, now=None, usuario=None):
    """Agrega somente intervalos comprovados pelos eventos duráveis da fila."""
    now = now or timezone.now()
    jobs = ExecucaoRaspagem.objects.filter(criada_em__lte=now).filter(
        Q(finalizada_em__isnull=True) | Q(finalizada_em__gte=since),
    )
    if usuario is not None:
        from apps.accounts.models import organization_for_user
        organization = organization_for_user(usuario)
        jobs = jobs.filter(organization=organization) if organization else jobs.none()
    queue_events = EventoRaspagem.objects.filter(
        etapa__startswith="Fila:", criado_em__lte=now,
    ).order_by("criado_em", "id")
    jobs = jobs.prefetch_related(Prefetch("eventos", queryset=queue_events))

    samples = {}
    current = {}
    for job in jobs:
        events = list(job.eventos.all())
        for index, event in enumerate(events):
            reason = event.etapa.split(":", 1)[1]
            start = max(event.criado_em, since)
            if index + 1 < len(events):
                end = events[index + 1].criado_em
            else:
                end = job.iniciada_em or job.finalizada_em or now
            end = min(end, now)
            if end > start:
                samples.setdefault(reason, []).append(int((end - start).total_seconds()))
        if job.status == "queued":
            reason = job.motivo_espera or "unknown"
            current[reason] = current.get(reason, 0) + 1

    by_reason = []
    for reason, values in sorted(samples.items()):
        ordered = sorted(values)
        by_reason.append({
            "reason": reason,
            "samples": len(ordered),
            "total_seconds": sum(ordered),
            "p50_seconds": ordered[len(ordered) // 2],
            "p90_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * .9))],
            "max_seconds": ordered[-1],
            "current": current.get(reason, 0),
        })
    for reason, count in sorted(current.items()):
        if reason not in samples:
            by_reason.append({
                "reason": reason, "samples": 0, "total_seconds": 0,
                "p50_seconds": None, "p90_seconds": None, "max_seconds": None,
                "current": count,
            })
    return {
        "by_reason": by_reason,
        "current_total": sum(current.values()),
        "measured_intervals": sum(len(values) for values in samples.values()),
    }
