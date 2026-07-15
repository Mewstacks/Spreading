"""
Loops de automação full-time (sem Redis/Celery). Dois modos independentes:
  - --modo scrape: a cada --scrape-horas, raspa ofertas + cupons + termos das configs.
  - --modo envio:  a cada --tick minutos, processa ConfiguracaoEnvio vencidas (envia).

Cada modo roda em processo separado, ligado/desligado pela sua tela.
Manual:  python manage.py automacao --modo scrape --scrape-horas 3
         python manage.py automacao --modo envio  --tick 5
"""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connections
from django.utils import timezone

from apps.scrapers import automacao_state as st

logger = logging.getLogger("apps.automacao")


ERRO_PUBLICO = "Falha temporária no serviço. Uma nova tentativa será feita no próximo ciclo."
RETRY_MINUTOS = 5


@contextmanager
def _heartbeat_durante(job, intervalo=15):
    """Mantém o estado operacional vivo enquanto uma coleta bloqueante executa."""
    parar = threading.Event()

    def _pulse():
        while not parar.wait(intervalo):
            st.write_state(job)

    thread = threading.Thread(target=_pulse, daemon=True, name=f"heartbeat-{job}")
    thread.start()
    try:
        yield
    finally:
        parar.set()
        thread.join(timeout=1)
        st.write_state(job)


def _renovar_conexoes_db():
    """Descarta conexões herdadas/ociosas antes de cada ciclo do worker.

    Estes comandos vivem por dias e passam horas dormindo. Nesse intervalo o
    Postgres/proxy pode encerrar o socket sem que o Django saiba; reutilizá-lo
    causava ``OperationalError: the connection is closed`` no ciclo seguinte.
    """
    connections.close_all()


def _rodar_scrape():
    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.models import ConfiguracaoEnvio

    termos = list(
        ConfiguracaoEnvio.objects.filter(ativo=True)
        .exclude(termo_busca="").values_list("termo_busca", flat=True)
    )
    lojas = list(MARKETPLACES.items())
    # Agnóstico de loja: cada marketplace raspa suas fontes. Habilitar Amazon/Shopee
    # depois não precisa editar este loop — basta registrar a loja no registry.
    falhas = []
    for i, (slug, mp) in enumerate(lojas):
        msg = f"[{timezone.now():%H:%M}] SCRAPE: {slug}..."
        logger.info(msg)
        st.write_state(
            "scrape", fase="raspando", loja_atual=slug,
            loja_idx=i + 1, lojas_total=len(lojas), ultima_msg=msg,
        )
        try:
            mp.scrape_all(termos=termos)
        except Exception:
            logger.exception("Scrape '%s' falhou", slug)
            falhas.append(slug)
            from apps.scrapers.models import FonteIngestao
            FonteIngestao.objects.filter(marketplace=slug, habilitada=True).update(
                status="degraded", ultima_tentativa=timezone.now(),
                erro_publico="Falha temporária na coleta; dados anteriores preservados.")
            st.write_state("scrape", erro=ERRO_PUBLICO)
    sucessos = len(lojas) - len(falhas)
    if sucessos:
        from apps.scrapers.maintenance import expire_stale
        expire_stale()
    from django.conf import settings
    if getattr(settings, "AFFILIATE_FEED_URL", ""):
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items
        feed = run_source("licensed-affiliate-feed")
        persist_items(feed.get("offers", []) + feed.get("coupons", []))
    if not sucessos:
        raise RuntimeError(f"Todas as fontes falharam: {', '.join(falhas)}")
    if falhas:
        logger.warning("SCRAPE concluído parcialmente; falharam: %s", ", ".join(falhas))
    else:
        logger.info("[%s] SCRAPE concluido", timezone.now().strftime("%H:%M"))
    return {"sucessos": sucessos, "falhas": falhas}


def _rodar_scrape_rapido(paginas=8):
    """LANE RÁPIDA/flash (B3): só o feed /ofertas do ML, poucas páginas, em UPSERT
    (não zera o feed da lane lenta). Pega deals-relâmpago entre as raspagens completas."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    from apps.scrapers.models import FonteIngestao
    logger.info("[%s] SCRAPE-FLASH: feed ML (%s paginas)", timezone.now().strftime("%H:%M"), paginas)
    total = mapear_ofertas(max_paginas=paginas, substituir=False)
    now = timezone.now()
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="mercadolivre-web",
        defaults={"marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"},
    )
    fonte.ultima_tentativa = now
    fonte.ultimo_total = total
    if total:
        fonte.status = "ok"
        fonte.ultimo_sucesso = now
        fonte.falhas_consecutivas = 0
        fonte.erro_publico = ""
    elif not fonte.ultimo_sucesso:
        fonte.status = "degraded"
        fonte.erro_publico = "Coleta vazia; catálogo anterior preservado."
    fonte.save()
    return total


class Command(BaseCommand):
    help = "Loop de automação: scrape (full) / scrape_rapido (flash) / envio / relatorios."

    def add_arguments(self, parser):
        parser.add_argument("--modo", choices=("scrape", "scrape_rapido", "envio", "relatorios"),
                            required=True,
                            help="scrape = raspagem completa; scrape_rapido = feed flash; "
                                 "envio = envio pelas regras.")
        parser.add_argument("--tick", type=int, default=5, help="Minutos entre ciclos (envio/flash).")
        parser.add_argument("--scrape-horas", type=float, default=3.0, help="Horas entre raspagens completas.")

    def handle(self, *args, **opts):
        if opts["modo"] == "scrape":
            self._loop_scrape(opts)
        elif opts["modo"] == "scrape_rapido":
            self._loop_scrape_rapido(opts)
        elif opts["modo"] == "envio":
            self._loop_envio(opts)
        else:
            self._loop_relatorios(opts)

    def _loop_scrape_rapido(self, opts):
        # Lane flash: gate no MESMO flag "scrape" (se a raspagem está ligada, roda).
        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("SCRAPE-FLASH worker no ar; feed a cada %smin quando ligado", tick)
        proximo = timezone.now()
        while True:
            # Heartbeat: marca o worker vivo (evita spawn duplicado em dev; worker_alive).
            if not st.is_enabled("scrape"):
                st.write_state("scrape_rapido", fase="ocioso")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("scrape_rapido", fase="aguardando")
                time.sleep(POLL)
                continue
            st.write_state("scrape_rapido", fase="raspando")
            try:
                _renovar_conexoes_db()
                _rodar_scrape_rapido()
            except Exception:
                logger.exception("Erro no scrape-flash")
            proximo = timezone.now() + timedelta(minutes=tick)
            st.write_state("scrape_rapido", fase="aguardando",
                           proximo=proximo.isoformat())

    def _loop_scrape(self, opts):
        # Processo SEMPRE vivo (honcho). Trabalha só quando o flag "scrape" está
        # ligado (tela Scraper); senão fica ocioso, checando a cada POLL segundos.
        scrape_seg = max(0.1, opts["scrape_horas"]) * 3600
        POLL = 15
        logger.info("SCRAPE worker no ar; raspa a cada %sh quando ligado", opts["scrape_horas"])
        ciclos = 0
        proximo = timezone.now()  # vencido: raspa assim que ligarem
        while True:
            # Heartbeat também durante as horas de espera; sem isto o supervisor
            # considera o processo morto após 90s e pode iniciar workers duplicados.
            st.write_state("scrape")
            if not st.is_enabled("scrape"):
                st.write_state("scrape", fase="desligado", loja_atual=None,
                               ultima_msg="Desligado — ligue na tela Scraper.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                time.sleep(POLL)
                continue
            try:
                st.write_state("scrape", fase="raspando", ciclos=ciclos, erro="")
                _renovar_conexoes_db()
                with _heartbeat_durante("scrape"):
                    resultado = _rodar_scrape()
                ciclos += 1
                fim = timezone.now()
                degradado = bool(resultado["falhas"])
                proximo = fim + (timedelta(minutes=30) if degradado
                                 else timedelta(seconds=scrape_seg))
                erro = ("Falha parcial: " + ", ".join(resultado["falhas"])
                        if degradado else "")
                st.write_state(
                    "scrape", fase="degradado" if degradado else "aguardando", loja_atual=None,
                    ultimo_ciclo_fim=fim.isoformat(), proximo_ciclo=proximo.isoformat(),
                    ciclos=ciclos, erro=erro,
                    ultima_msg=(f"Ciclo {ciclos} parcial; nova tentativa em 30 min."
                                if degradado else f"Ciclo {ciclos} concluído às {fim:%H:%M}."),
                )
            except Exception:
                logger.exception("Erro no scrape")
                proximo = timezone.now() + timedelta(minutes=RETRY_MINUTOS)
                st.write_state("scrape", fase="aguardando", loja_atual=None,
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

    def _loop_envio(self, opts):
        from apps.scrapers.ofertas import processar_configs_de_envio

        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("ENVIO worker no ar; processa regras a cada %smin quando ligado", tick)
        ticks = 0
        proximo = timezone.now()  # vencido: processa assim que ligarem
        while True:
            if not st.is_enabled("envio"):
                st.write_state("envio", fase="desligado",
                               ultima_msg="Desligado — ligue na tela Envios.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("envio", fase="processando", loja_atual=None)
                _renovar_conexoes_db()
                res = processar_configs_de_envio()
                enviados = sum(1 for r in res if r.get("sucesso"))
                # Watchdog de conexões: alerta por e-mail quando WA/ML cai (cooldown interno).
                try:
                    from apps.scrapers.monitor_conexao import verificar_e_notificar
                    verificar_e_notificar()
                except Exception as e:
                    logger.warning("Monitor de conexao falhou: %s", e)
                ticks += 1
                logger.info("[%s] tick: %s config(s) vencida(s), %s enviada(s)", agora.strftime("%H:%M"), len(res), enviados)
                st.write_state(
                    "envio", fase="aguardando", ticks=ticks,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    vencidas=len(res), enviados=enviados, erro="",
                    ultima_msg=f"{enviados} enviada(s) de {len(res)} vencida(s) às {agora:%H:%M}.",
                )
            except Exception:
                logger.exception("Erro no tick de envio")
                st.write_state(
                    "envio", fase="aguardando",
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    erro=ERRO_PUBLICO,
                )
            proximo = timezone.now() + timedelta(minutes=tick)

    def _loop_relatorios(self, opts):
        from apps.scrapers.relatorios import sync_due_reports

        tick = max(30, opts["tick"])
        POLL = 15
        logger.info("RELATORIOS worker no ar; sincroniza a cada %smin quando ligado", tick)
        ciclos = 0
        proximo = timezone.now()
        while True:
            if not st.is_enabled("relatorios"):
                st.write_state("relatorios", fase="desligado",
                               ultima_msg="Desligado — ligue quando quiser sync automático.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("relatorios", fase="sincronizando", erro="")
                _renovar_conexoes_db()
                resultados = sync_due_reports()
                ok = sum(1 for s in resultados if s.status == "ok")
                acao = sum(1 for s in resultados if s.status == "acao")
                erros = sum(1 for s in resultados if s.status == "erro")
                ciclos += 1
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "relatorios", fase="aguardando", ciclos=ciclos,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=proximo.isoformat(), ok=ok, acao=acao,
                    erro_count=erros,
                    ultima_msg=f"{ok} ok, {acao} ação, {erros} erro às {agora:%H:%M}.",
                    erro="",
                )
            except Exception:
                logger.exception("Erro no sync de relatórios")
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state("relatorios", fase="aguardando",
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)
