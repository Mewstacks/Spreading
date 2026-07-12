"""
Loops de automação full-time (sem Redis/Celery). Dois modos independentes:
  - --modo scrape: a cada --scrape-horas, raspa ofertas + cupons + termos das configs.
  - --modo envio:  a cada --tick minutos, processa ConfiguracaoEnvio vencidas (envia).

Cada modo roda em processo separado, ligado/desligado pela sua tela.
Manual:  python manage.py automacao --modo scrape --scrape-horas 3
         python manage.py automacao --modo envio  --tick 5
"""
import logging
import time
import traceback
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.scrapers import automacao_state as st

logger = logging.getLogger("apps.automacao")


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
    for i, (slug, mp) in enumerate(lojas):
        msg = f"[{timezone.now():%H:%M}] SCRAPE: {slug}..."
        logger.info(msg)
        st.write_state(
            "scrape", fase="raspando", loja_atual=slug,
            loja_idx=i + 1, lojas_total=len(lojas), ultima_msg=msg,
        )
        try:
            mp.scrape_all(termos=termos)
        except Exception as e:
            logger.warning("Scrape '%s' falhou: %s", slug, e)
            st.write_state("scrape", erro=f"{slug}: {e}"[:300])
    logger.info("[%s] SCRAPE concluido", timezone.now().strftime("%H:%M"))


def _rodar_scrape_rapido(paginas=8):
    """LANE RÁPIDA/flash (B3): só o feed /ofertas do ML, poucas páginas, em UPSERT
    (não zera o feed da lane lenta). Pega deals-relâmpago entre as raspagens completas."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    logger.info("[%s] SCRAPE-FLASH: feed ML (%s paginas)", timezone.now().strftime("%H:%M"), paginas)
    mapear_ofertas(max_paginas=paginas, substituir=False)


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
                _rodar_scrape()
                ciclos += 1
                fim = timezone.now()
                proximo = fim + timedelta(seconds=scrape_seg)
                st.write_state(
                    "scrape", fase="aguardando", loja_atual=None,
                    ultimo_ciclo_fim=fim.isoformat(), proximo_ciclo=proximo.isoformat(),
                    ciclos=ciclos, erro="",
                    ultima_msg=f"Ciclo {ciclos} concluído às {fim:%H:%M}.",
                )
            except Exception:
                tb = traceback.format_exc()
                logger.exception("Erro no scrape")
                proximo = timezone.now() + timedelta(seconds=scrape_seg)
                st.write_state("scrape", fase="aguardando", loja_atual=None,
                               proximo_ciclo=proximo.isoformat(), erro=tb[-300:])

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
                tb = traceback.format_exc()
                logger.exception("Erro no tick de envio")
                st.write_state(
                    "envio", fase="aguardando",
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    erro=tb[-300:],
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
                tb = traceback.format_exc()
                logger.exception("Erro no sync de relatórios")
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state("relatorios", fase="aguardando",
                               proximo_ciclo=proximo.isoformat(), erro=tb[-300:])
