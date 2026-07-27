"""
Tasks Celery do pipeline. Cada uma é um wrapper fino sobre código que também
roda via management command / função direta, com retry/backoff para a UI flaky
do Mercado Livre.
"""
from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def task_scrape(self):
    """Pipeline diário: raspa todas as lojas registradas (mesma lógica do loop automacao).
    Fonte única em management/commands/automacao.py p/ não divergir."""
    from apps.scrapers.management.commands.automacao import _rodar_scrape
    try:
        _rodar_scrape()
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
    logger.info("Tick de envios: %s config(s) vencida(s), %s enviada(s)", len(resultados), enviados)
    return resultados
