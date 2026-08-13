"""Watchdog de conexões: checa WhatsApp + ML, registra transições, alerta por e-mail.

    python manage.py monitorar            # uma passada e sai (debug/manual)
    python manage.py monitorar --tick 5   # loop contínuo — é assim que o Procfile roda

Este é um processo PRÓPRIO (Procfile: `monitor`), e não mais um passageiro do tick
de envio. Antes `verificar_e_notificar()` só rodava dentro do _loop_envio, então com
o envio desligado ninguém monitorava nada: `conexao_voltou` nunca era emitido e os
incidentes de conexão ficavam abertos para sempre — a Saúde vermelha ao lado de um
dashboard verde. Monitorar conexão não pode depender de a automação estar ligada,
por isso este loop não tem flag de liga/desliga.

Também reconcilia os eventos ainda sem incidente projetado. Isso era feito DENTRO do
GET da tela de Saúde (escrita em request de leitura), o que impedia o auto-refresh:
com polling, cada carregamento inflaria as ocorrências.

E abriga o vigia externo do worker WhatsApp (wa_supervisor): a cada POLL de 15s ele
sonda o /health do spreading-wa e reinicia a máquina pela API do Fly se ela parar de
responder — o Fly não reinicia máquina por health check critical, então sem alguém de
fora uma VM travada ficava assim para sempre (incidente de 08/08).
"""
import logging
import time
from apps.accounts.tenant import system_job

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connections

from apps.scrapers import automacao_state as st
from apps.scrapers import wa_supervisor
from apps.scrapers.monitor_conexao import verificar_e_notificar

logger = logging.getLogger(__name__)

JOB = "monitor"


class Command(BaseCommand):
    help = "Verifica conexões (WhatsApp/ML), registra transições e envia alertas."

    def add_arguments(self, parser):
        parser.add_argument("--tick", type=int, default=0,
                            help="Minutos entre checagens. 0 (default) = uma passada e sai.")

    @system_job
    def handle(self, *args, **opts):
        tick = max(0, opts["tick"])
        if not tick:
            r = self._ciclo()
            self.stdout.write(
                f"Checados {r['checados']} perfil(s); {r['alertas_enviados']} alerta(s) "
                f"enviado(s); {r['reconciliados']} evento(s) reconciliado(s); "
                f"{r['conexoes_fechadas']} incidente(s) de conexão fechado(s)."
            )
            return

        logger.info("MONITOR worker no ar; checagem de conexões a cada %smin", tick)
        POLL = 15
        proximo = time.monotonic()
        falhas_banco = 0
        while True:
            # Vigia externo do worker WhatsApp: na cadência do POLL (15s), não no
            # tick — com 6 falhas a 15s a recuperação leva ~90s em vez de ~30min.
            # verificar() nunca levanta; o vigia não pode derrubar o monitor.
            wa_supervisor.verificar()
            if time.monotonic() < proximo:
                st.write_state(JOB, fase="aguardando")
                time.sleep(POLL)
                continue
            try:
                st.write_state(JOB, fase="checando", erro="")
                # Este processo vive por dias e passa horas ocioso: o Postgres pode
                # ter encerrado o socket nesse meio-tempo (mesma razão de
                # _renovar_conexoes_db em automacao.py).
                connections.close_all()
                r = self._ciclo()
                if falhas_banco >= 2:
                    # Só é seguro gravar o evento depois que o banco voltou.
                    from apps.scrapers.eventos import log_event
                    log_event("infraestrutura", "banco_recuperado",
                              "Conexão com o banco foi restabelecida.", level="info",
                              contexto={"falhas_consecutivas": falhas_banco})
                falhas_banco = 0
                st.write_state(
                    JOB, fase="aguardando", erro="",
                    ultima_msg=(f"{r['checados']} conta(s) checada(s), "
                                f"{r['alertas_enviados']} alerta(s)."),
                )
            except DatabaseError as e:
                falhas_banco += 1
                connections.close_all()
                alerta = falhas_banco >= 2
                if alerta:
                    logger.error("ALERTA_BANCO: %s falhas consecutivas: %s", falhas_banco, e)
                else:
                    logger.warning("Banco indisponível no monitor: %s", e)
                st.write_state(
                    JOB, fase="aguardando_banco",
                    erro="Banco temporariamente indisponível; monitorando recuperação.",
                    falhas_banco=falhas_banco,
                    ultima_msg=("Alerta: banco indisponível em duas checagens consecutivas."
                                if alerta else "Banco indisponível; nova checagem agendada."),
                )
            except Exception as e:
                logger.exception("Erro no ciclo do monitor de conexões")
                st.write_state(JOB, fase="aguardando",
                               erro="Falha ao checar conexões; tentando de novo.")
                # O watchdog é quem detecta queda de conexão; se ele morre, o sistema
                # fica cego justamente para o que mais importa.
                try:
                    from apps.scrapers.eventos import log_event
                    log_event("conexao", "watchdog_erro",
                              f"O monitor de conexões falhou: {e}", level="error", exc=e)
                except Exception:
                    pass
            proximo = time.monotonic() + tick * 60

    def _ciclo(self) -> dict:
        from apps.scrapers.incidentes_saude import (
            fechar_conexoes_restabelecidas, reconciliar_pendentes,
        )
        from apps.scrapers.maintenance import (
            diagnosticar_alertas_pipeline_cupons, expire_stale,
            purgar_eventos_cupons_antigos,
        )

        r = verificar_e_notificar()
        from apps.scrapers.manual_scraping import (
            atualizar_diagnostico_fila, recuperar_jobs_abandonados,
        )
        r["raspagens_recuperadas"] = recuperar_jobs_abandonados()
        r["raspagens_na_fila"] = atualizar_diagnostico_fila()
        # A expiração não pode depender do scraper estar ligado ou de uma coleta
        # ter terminado com sucesso. Se a fonte parar, é justamente quando o
        # catálogo antigo precisa deixar de ser publicável.
        expirados = expire_stale()
        from django.core.cache import cache
        if cache.add("coupon-event-retention-v1", "1", timeout=24 * 60 * 60):
            r["eventos_cupons_purgados"] = purgar_eventos_cupons_antigos(90)
        r["alertas_cupons"] = diagnosticar_alertas_pipeline_cupons()
        if any(r["alertas_cupons"].values()) and cache.add(
                "coupon-pipeline-sla-alert-v1", "1", timeout=20 * 60):
            logger.warning("ALERTA_PIPELINE_CUPONS: %s", r["alertas_cupons"])
            from apps.scrapers.eventos import log_event
            log_event(
                "scraper", "coupon_pipeline_sla",
                "O funil de cupons ultrapassou um ou mais SLAs operacionais.",
                level="warning", contexto=r["alertas_cupons"],
            )
        r["produtos_expirados"] = expirados["products"]
        r["cupons_expirados"] = expirados["coupons"]
        if expirados["products"] or expirados["coupons"]:
            logger.info(
                "Validade do catálogo: %s produto(s) e %s cupom(ns) expirado(s)",
                expirados["products"], expirados["coupons"],
            )
        r["reconciliados"] = reconciliar_pendentes()
        # Depois de reconciliar: incidente de conexão órfão (aberto por um watchdog
        # que morreu antes de registrar a queda no Perfil) não tem transição futura
        # para ser fechado por, e ficaria vermelho na Saúde para sempre.
        r["conexoes_fechadas"] = fechar_conexoes_restabelecidas()
        return r
