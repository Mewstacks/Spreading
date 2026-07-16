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
from apps.scrapers.eventos import log_event

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
        except Exception as e:
            logger.exception("Scrape '%s' falhou", slug)
            # Por loja: uma fonte quebrada (seletor mudou, bloqueio) não derruba o
            # ciclo, então some do radar. É a falha que envenena o catálogo devagar.
            log_event("scraper", "fonte_falhou", f"A coleta da loja {slug} falhou.",
                      level="error", contexto={"marketplace": slug}, exc=e)
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


def _rodar_links(lote=40):
    """Pré-gera links de afiliado dos produtos pendentes — um lote por ciclo.

    Sem isto nada em produção gerava link: o scrape só cria Produto (com link vazio),
    e cada raspagem só aumentava a pilha de "pendente" na tela de Promoções.

    Por usuário, porque o link carrega a conta de afiliado de quem envia: quem não
    tem sessão ML válida é pulado (gerar exigiria o Link Builder logado). O lote é
    pequeno de propósito — cada item custa uma ida ao Link Builder (~5s), e este
    processo divide o Chromium e a CPU com a raspagem e o painel.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Exists, OuterRef, Q

    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.models import LinkAfiliadoUsuario, Produto
    from apps.scrapers.monitor_conexao import ml_conectado

    gerados = falhas = 0
    for user in get_user_model().objects.filter(is_active=True):
        if not ml_conectado(user):
            continue
        ja_tem = LinkAfiliadoUsuario.objects.filter(
            usuario=user, produto=OuterRef("pk")).exclude(link_afiliado="")
        pendentes = list(
            Produto.objects.filter(marketplace="mercadolivre", preco_sem_desconto__gt=0)
            .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
            .filter(Q(owner__isnull=True) | Q(owner=user))
            .exclude(Exists(ja_tem))
            .order_by("-ultima_observacao")[:lote]
        )
        if not pendentes:
            continue
        try:
            g, f = get_marketplace("mercadolivre").prefetch_links(pendentes, usuario=user)
        except Exception as e:
            # Sessão expirada/queda do Link Builder é de UM usuário: não pode
            # impedir que os outros gerem os deles.
            logger.warning("Geração de links falhou para %s: %s", user, e)
            log_event("scraper", "links_erro",
                      f"Não foi possível gerar links de afiliado: {e}",
                      level="warning", usuario=user, exc=e)
            continue
        gerados += g
        falhas += f
        logger.info("Links ML p/ %s: %s gerado(s), %s falha(s) de %s pendente(s)",
                    user, g, f, len(pendentes))
    return {"gerados": gerados, "falhas": falhas}


class Command(BaseCommand):
    help = ("Loop de automação: scrape (full) / scrape_rapido (flash) / envio / "
            "links (afiliação) / relatorios.")

    def add_arguments(self, parser):
        parser.add_argument("--modo",
                            choices=("scrape", "scrape_rapido", "envio", "links", "relatorios"),
                            required=True,
                            help="scrape = raspagem completa; scrape_rapido = feed flash; "
                                 "envio = envio pelas regras; links = pré-gera links "
                                 "de afiliado dos pendentes.")
        parser.add_argument("--tick", type=int, default=5, help="Minutos entre ciclos (envio/flash/links).")
        parser.add_argument("--lote", type=int, default=40, help="Links gerados por ciclo, por usuário.")
        parser.add_argument("--scrape-horas", type=float, default=3.0, help="Horas entre raspagens completas.")

    def handle(self, *args, **opts):
        if opts["modo"] == "scrape":
            self._loop_scrape(opts)
        elif opts["modo"] == "scrape_rapido":
            self._loop_scrape_rapido(opts)
        elif opts["modo"] == "envio":
            self._loop_envio(opts)
        elif opts["modo"] == "links":
            self._loop_links(opts)
        else:
            self._loop_relatorios(opts)

    def _loop_links(self, opts):
        # Gate no MESMO flag "scrape" (igual à lane flash): afiliar é parte do
        # pipeline de catálogo, e não faz sentido gerar link com a coleta desligada.
        tick = max(1, opts["tick"])
        lote = max(1, opts["lote"])
        POLL = 15
        logger.info("LINKS worker no ar; até %s link(s)/usuário a cada %smin quando ligado",
                    lote, tick)
        proximo = timezone.now()
        while True:
            if not st.is_enabled("scrape"):
                st.write_state("links", fase="desligado",
                               ultima_msg="Desligado — ligue na tela Scraper.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("links", fase="aguardando")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("links", fase="gerando", erro="")
                _renovar_conexoes_db()
                with _heartbeat_durante("links"):
                    res = _rodar_links(lote=lote)
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "links", fase="aguardando", proximo_ciclo=proximo.isoformat(),
                    gerados=res["gerados"], falhas=res["falhas"], erro="",
                    ultima_msg=(f"{res['gerados']} link(s) gerado(s), "
                                f"{res['falhas']} falha(s) às {agora:%H:%M}."),
                )
            except Exception as e:
                logger.exception("Erro no ciclo de links")
                log_event("scraper", "links_ciclo_erro",
                          f"Ciclo de geração de links falhou: {e}", level="error", exc=e)
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state("links", fase="aguardando",
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

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
            except Exception as e:
                logger.exception("Erro no scrape-flash")
                log_event("scraper", "flash_erro", f"Ciclo do feed rápido falhou: {e}",
                          level="error", exc=e)
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
            except Exception as e:
                logger.exception("Erro no scrape")
                log_event("scraper", "scrape_erro", f"Ciclo de raspagem falhou: {e}",
                          level="error", contexto={"ciclos": ciclos}, exc=e)
                proximo = timezone.now() + timedelta(minutes=RETRY_MINUTOS)
                st.write_state("scrape", fase="aguardando", loja_atual=None,
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

    def _loop_envio(self, opts):
        from apps.scrapers.ofertas import processar_configs_de_envio

        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("ENVIO worker no ar; processa regras a cada %smin quando ligado", tick)
        ticks = 0
        ultima_purga = None  # data da última purga do log (1x/dia, ver abaixo)
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
                # Faxina antes do tick: fecha publicações que ficaram 'pendente' porque
                # o worker morreu no meio de um envio (deploy/crash). Nunca derruba o
                # tick — envio é o que importa aqui.
                try:
                    from apps.scrapers.maintenance import reconciliar_publicacoes_orfas
                    orfas = reconciliar_publicacoes_orfas()
                    if orfas:
                        logger.warning("%s publicacao(oes) orfa(s) fechada(s) como falha", orfas)
                except Exception as e:
                    logger.warning("Reconciliacao de publicacoes falhou: %s", e)
                # Purga do log 1x/dia. Mora neste loop porque é o único ligado o dia
                # todo em produção; se o envio estiver desligado nada gera evento, então
                # não purgar também não é problema. Nunca derruba o tick.
                hoje_purga = timezone.localdate()
                if ultima_purga != hoje_purga:
                    try:
                        from apps.scrapers.maintenance import purgar_eventos_antigos
                        apagados = purgar_eventos_antigos()
                        ultima_purga = hoje_purga
                        if apagados:
                            logger.info("Purga de eventos: %s linha(s) removida(s)", apagados)
                    except Exception as e:
                        logger.warning("Purga de eventos falhou: %s", e)
                res = processar_configs_de_envio()
                enviados = sum(1 for r in res if r.get("sucesso"))
                # Watchdog de conexões: alerta por e-mail quando WA/ML cai (cooldown interno).
                try:
                    from apps.scrapers.monitor_conexao import verificar_e_notificar
                    verificar_e_notificar()
                except Exception as e:
                    logger.warning("Monitor de conexao falhou: %s", e)
                    # O watchdog é quem detecta queda de conexão; se ele morre, o
                    # sistema fica cego justamente para o que mais importa.
                    log_event("conexao", "watchdog_erro",
                              f"O monitor de conexões falhou: {e}",
                              level="error", exc=e)
                ticks += 1
                logger.info("[%s] tick: %s config(s) vencida(s), %s enviada(s)", agora.strftime("%H:%M"), len(res), enviados)
                st.write_state(
                    "envio", fase="aguardando", ticks=ticks,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    vencidas=len(res), enviados=enviados, erro="",
                    ultima_msg=f"{enviados} enviada(s) de {len(res)} vencida(s) às {agora:%H:%M}.",
                )
            except Exception as e:
                logger.exception("Erro no tick de envio")
                # Tick inteiro morto = nenhum usuário recebe oferta neste ciclo.
                log_event("publicacao", "tick_erro", f"Ciclo de envio falhou: {e}",
                          level="error", contexto={"ticks": ticks}, exc=e)
                st.write_state(
                    "envio", fase="aguardando",
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    erro=ERRO_PUBLICO,
                )
            proximo = timezone.now() + timedelta(minutes=tick)

    def _loop_relatorios(self, opts):
        from apps.scrapers.relatorios import sync_due_reports

        # Quem decide a cadência é o proxima_execucao de cada RelatorioSync (6h após
        # cada sync), e sync_due_reports já respeita isso — este loop só precisa
        # perguntar de vez em quando. O --tick de 360min era um segundo agendador por
        # cima do primeiro, e fazia o botão "Sincronizar" da tela esperar até 6h.
        POLL = 60
        logger.info("RELATORIOS worker no ar; checa vencidos a cada %ss quando ligado", POLL)
        ciclos = 0
        while True:
            if not st.is_enabled("relatorios"):
                st.write_state("relatorios", fase="desligado",
                               ultima_msg="Desligado — ligue quando quiser sync automático.")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("relatorios", fase="sincronizando", erro="")
                _renovar_conexoes_db()
                resultados = sync_due_reports()
                if not resultados:
                    # Nada vencido: não é um ciclo, é silêncio. Não mexe no estado
                    # visível pra não zerar o "última sincronização" da tela.
                    st.write_state("relatorios", fase="aguardando")
                    time.sleep(POLL)
                    continue
                ok = sum(1 for s in resultados if s.status == "ok")
                acao = sum(1 for s in resultados if s.status == "acao")
                erros = sum(1 for s in resultados if s.status == "erro")
                ciclos += 1
                proximo = timezone.now() + timedelta(seconds=POLL)
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
                st.write_state("relatorios", fase="aguardando", erro=ERRO_PUBLICO)
            time.sleep(POLL)
