"""
Loops de automação full-time (sem Redis/Celery). Dois modos independentes:
  - --modo scrape: a cada --scrape-horas, raspa ofertas + cupons + termos das configs.
  - --modo envio:  a cada --tick minutos, processa ConfiguracaoEnvio vencidas (envia).

Cada modo roda em processo separado, ligado/desligado pela sua tela.
Manual:  python manage.py automacao --modo scrape --scrape-horas 3
         python manage.py automacao --modo envio  --tick 5
"""
import time
import traceback
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.scrapers import automacao_state as st


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
        print(msg)
        st.write_state(
            "scrape", fase="raspando", loja_atual=slug,
            loja_idx=i + 1, lojas_total=len(lojas), ultima_msg=msg,
        )
        try:
            mp.scrape_all(termos=termos)
        except Exception as e:
            print(f"  scrape '{slug}' falhou: {e}")
            st.write_state("scrape", erro=f"{slug}: {e}"[:300])
    print(f"[{timezone.now():%H:%M}] SCRAPE concluído.")


class Command(BaseCommand):
    help = "Loop de automação: --modo scrape (raspagem) ou --modo envio (envio por config)."

    def add_arguments(self, parser):
        parser.add_argument("--modo", choices=("scrape", "envio"), required=True,
                            help="scrape = raspagem periódica; envio = envio pelas regras.")
        parser.add_argument("--tick", type=int, default=5, help="Minutos entre ciclos de envio.")
        parser.add_argument("--scrape-horas", type=float, default=3.0, help="Horas entre raspagens.")

    def handle(self, *args, **opts):
        if opts["modo"] == "scrape":
            self._loop_scrape(opts)
        else:
            self._loop_envio(opts)

    def _loop_scrape(self, opts):
        scrape_seg = max(0.1, opts["scrape_horas"]) * 3600
        print(f"SCRAPE LIGADO — raspa a cada {opts['scrape_horas']}h.")
        st.write_state("scrape", fase="iniciando", intervalo_horas=opts["scrape_horas"],
                       iniciado_em=timezone.now().isoformat(), ciclos=0, erro="")
        ciclos = 0
        while True:
            inicio = timezone.now()
            try:
                _rodar_scrape()
                ciclos += 1
                fim = timezone.now()
                st.write_state(
                    "scrape", fase="aguardando", loja_atual=None,
                    ultimo_ciclo_fim=fim.isoformat(),
                    proximo_ciclo=(fim + timedelta(seconds=scrape_seg)).isoformat(),
                    ciclos=ciclos, erro="",
                    ultima_msg=f"Ciclo {ciclos} concluído às {fim:%H:%M}.",
                )
            except Exception:
                tb = traceback.format_exc()
                print("Erro no scrape:\n" + tb)
                st.write_state(
                    "scrape", fase="aguardando", loja_atual=None,
                    proximo_ciclo=(timezone.now() + timedelta(seconds=scrape_seg)).isoformat(),
                    erro=tb[-300:],
                )
            time.sleep(scrape_seg)

    def _loop_envio(self, opts):
        from apps.scrapers.ofertas import processar_configs_de_envio

        tick = max(1, opts["tick"])
        print(f"ENVIO LIGADO — processa regras a cada {tick}min.")
        st.write_state("envio", fase="aguardando", tick_min=tick,
                       iniciado_em=timezone.now().isoformat(), ticks=0, erro="")
        ticks = 0
        while True:
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
                    print(f"  monitor conexao falhou: {e}")
                ticks += 1
                print(f"[{agora:%H:%M}] tick: {len(res)} config(s) vencida(s), {enviados} enviada(s).")
                st.write_state(
                    "envio", fase="aguardando", ticks=ticks,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    vencidas=len(res), enviados=enviados, erro="",
                    ultima_msg=f"{enviados} enviada(s) de {len(res)} vencida(s) às {agora:%H:%M}.",
                )
            except Exception:
                tb = traceback.format_exc()
                print("Erro no tick de envio:\n" + tb)
                st.write_state(
                    "envio", fase="aguardando",
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    erro=tb[-300:],
                )
            time.sleep(tick * 60)
