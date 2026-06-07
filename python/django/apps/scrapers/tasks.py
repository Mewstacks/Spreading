"""
Tasks Celery do pipeline. Cada uma é um wrapper fino sobre código que também
roda via management command / função direta, com retry/backoff para a UI flaky
do Mercado Livre.
"""
from celery import shared_task
from django.core.management import call_command


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def task_scrape(self):
    """Pipeline diário: raspa as melhores OFERTAS do ML (de/por). Cupons saíram da rotina."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    try:
        mapear_ofertas(max_paginas=25)
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task
def task_tick_envios():
    """
    Tick periódico: processa todas as ConfiguracaoEnvio ativas e dispara as que
    estão vencidas (respeita intervalo + cooldown por config). Roda a cada poucos
    minutos via beat.
    """
    from apps.scrapers.ofertas import processar_configs_de_envio
    resultados = processar_configs_de_envio()
    enviados = sum(1 for r in resultados if r.get("sucesso"))
    print(f"[tick] {len(resultados)} config(s) vencida(s), {enviados} enviada(s).")
    return resultados
