"""Loops full-time de coleta, cupons, afiliação, envio e relatórios.

Cada modo roda em processo separado. ``cupons`` é deliberadamente independente
do toggle da raspagem geral para renovar a janela segura de preparo.
"""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connections
from django.utils import timezone
from apps.accounts.tenant import system_job

from apps.scrapers import automacao_state as st
from apps.scrapers.eventos import log_event

logger = logging.getLogger("apps.automacao")


ERRO_PUBLICO = "Falha temporária no serviço. Uma nova tentativa será feita no próximo ciclo."
RETRY_MINUTOS = 5
BACKOFF_BANCO_MAX_S = 300


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


def _pausar_por_banco(job, erro, falhas: int):
    """Evita retry em loop quando o Postgres/proxy está indisponível.

    Não gravamos EventoOperacional aqui: ele também depende do mesmo banco. O estado
    do worker fica no volume e permite que a tela de Saúde mostre o ocorrido assim
    que a conexão voltar.
    """
    espera = min(15 * (2 ** max(0, falhas - 1)), BACKOFF_BANCO_MAX_S)
    proximo = timezone.now() + timedelta(seconds=espera)
    connections.close_all()
    logger.warning("%s pausado por banco indisponível; nova tentativa em %ss: %s",
                   job, espera, erro)
    st.write_state(job, fase="aguardando_banco", erro=ERRO_PUBLICO,
                   proximo_ciclo=proximo.isoformat(),
                   ultima_msg=f"Banco indisponível; nova tentativa em {espera}s.")
    return proximo


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
        inicio_loja = timezone.now()
        try:
            mp.scrape_all(termos=termos)
        except Exception as e:
            logger.exception("Scrape '%s' falhou", slug)
            # Por loja: uma fonte quebrada (seletor mudou, bloqueio) não derruba o
            # ciclo, então some do radar. É a falha que envenena o catálogo devagar.
            log_event("scraper", "fonte_falhou", f"A coleta da loja {slug} falhou.",
                      level="error", contexto={"marketplace": slug}, exc=e)
            falhas.append(slug)
            from django.db.models import Q
            from apps.scrapers.models import FonteIngestao
            # SÓ as fontes que ainda não deram veredito próprio neste ciclo. Sem o
            # recorte por `ultima_tentativa`, uma exceção tardia em `scrape_all`
            # rebaixava as três linhas da Amazon de uma vez — inclusive as que
            # tinham acabado de reportar sucesso —, e nada nunca as promovia de
            # volta a não ser um ciclo inteiro sem nenhum defeito.
            FonteIngestao.objects.filter(
                Q(ultima_tentativa__isnull=True) | Q(ultima_tentativa__lt=inicio_loja),
                marketplace=slug, habilitada=True,
            ).update(
                status="degraded", ultima_tentativa=timezone.now(),
                erro_publico="Falha temporária na coleta; dados anteriores preservados.")
            st.write_state("scrape", erro=ERRO_PUBLICO)
    sucessos = len(lojas) - len(falhas)
    if sucessos:
        from apps.scrapers.maintenance import expire_stale
        expire_stale()
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
        slug="mercadolivre-ofertas-flash",
        defaults={"marketplace": "mercadolivre", "nome": "Mercado Livre — ofertas flash"},
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


def _rodar_feed_afiliados():
    """Compatibilidade: a implementação efetiva mora no pipeline central."""
    from apps.scrapers.coupon_pipeline import coletar_feed_licenciado

    return coletar_feed_licenciado()


def _rodar_cupons(lote=40):
    """Mantém coleta, preparo e links mesmo com a raspagem geral desligada."""
    from apps.scrapers.coupon_pipeline import executar_pipeline_cupons

    resultado = executar_pipeline_cupons(
        coletar=True,
        limite_preparo=max(12, lote),
        limite_links=max(1, lote),
    )
    logger.info(
        "CUPONS: %s encontrado(s), %s persistido(s), %s preparado(s), "
        "%s link(s) verificado(s), %s cupom(ns) pronto(s), %s falha(s)",
        resultado["encontrados"], resultado["persistidos"],
        resultado["preparados"], resultado["links_verificados"],
        resultado["prontos"], resultado["falhos"] + resultado["links_falhos"],
    )
    return resultado


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

    agora = timezone.now()
    gerados = falhas = pulados = 0
    for user in get_user_model().objects.filter(is_active=True):
        if not ml_conectado(user):
            # Antes isto era um `continue` mudo: o usuário simplesmente nunca gerava
            # link e nada em lugar nenhum dizia por quê. Agora a Saúde mostra.
            pulados += 1
            _avisar_sem_sessao_ml(user)
            continue
        ja_tem = LinkAfiliadoUsuario.objects.filter(
            usuario=user, produto=OuterRef("pk")).exclude(link_afiliado="")
        # Fora da fila: quem já tem link, quem é terminal (não afiliável / desistimos)
        # e quem está de castigo no backoff. Sem isto, produtos que nunca afiliam
        # ocupavam o lote de 40 a cada ciclo — os mais recentes primeiro — e nenhum
        # outro produto chegava a ser tentado. A pilha de "pendente" não saía nunca.
        fora_da_fila = LinkAfiliadoUsuario.objects.filter(
            usuario=user, produto=OuterRef("pk")).filter(
                Q(estado__in=["nao_afiliavel", "erro"])
                | Q(proxima_tentativa__gt=agora))
        pendentes = list(
            Produto.objects.filter(marketplace="mercadolivre", preco_sem_desconto__gt=0)
            .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
            .filter(Q(owner__isnull=True) | Q(owner=user))
            .exclude(Exists(ja_tem))
            .exclude(Exists(fora_da_fila))
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
    return {"gerados": gerados, "falhas": falhas, "pulados": pulados}


def _rodar_verificacao_links(limite=40):
    """Aprova o DESTINO dos links já gerados — o passo que torna a oferta enviável.

    Era um "passageiro" da geração: ficava DEPOIS do `if not pendentes: continue`
    dentro de _rodar_links, então assim que a fila de geração esvaziava (todo
    produto já com link) a verificação nunca mais rodava. Em homologação isso
    deixou 287 links gerados com apenas 6 verificados — e como a tela só lista item
    com verificado_ok=True, o catálogo inteiro ficava invisível. Agora é uma lane
    própria, que roda tenha ou não link novo para gerar.

    NÃO exige sessão do ML: a verificação abre a página pública do destino
    (validar_sessao=False, e `verificar_link_afiliado` nem usa o usuário). Um
    usuário com a conta desconectada tem centenas de links esperando veredito, e
    exigir sessão aqui os manteria invisíveis sem motivo.
    """
    from django.contrib.auth import get_user_model
    from apps.scrapers.scraper_mercadolivre.link import verificar_links_pendentes

    total = {"aprovados": 0, "reprovados": 0, "transitorios": 0}
    for user in get_user_model().objects.filter(is_active=True):
        try:
            r = verificar_links_pendentes(user, limite=limite)
        except Exception as e:
            logger.warning("Verificação de destino ML falhou para %s: %s", user, e)
            continue
        for chave in total:
            total[chave] += r.get(chave, 0)
    if any(total.values()):
        logger.info("Verificação de destino ML: %s aprovado(s), %s reprovado(s), "
                    "%s transitório(s)", total["aprovados"], total["reprovados"],
                    total["transitorios"])
    return total


def _avisar_sem_sessao_ml(user):
    """Registra que este usuário não gera link por falta de sessão ML — com cooldown.

    Sem cooldown seriam 288 eventos/dia por usuário desconectado (tick de 5min), e a
    tela de Saúde afogaria justamente no aviso que precisa ser lido.
    """
    from django.core.cache import cache

    chave = f"links_sem_sessao:{user.id}"
    if cache.get(chave):
        return
    cache.set(chave, True, timeout=6 * 3600)
    log_event("scraper", "links_sem_sessao",
              f"{user.get_username()} não gera links de afiliado: a sessão do "
              f"Mercado Livre não está conectada.",
              level="warning", usuario=user, contexto={"servico": "Mercado Livre"})


class Command(BaseCommand):
    help = ("Loop de automação: scrape (full) / scrape_rapido (flash) / envio / "
            "cupons / links (afiliação) / relatorios / manual.")

    def add_arguments(self, parser):
        parser.add_argument("--modo",
                            choices=("scrape", "scrape_rapido", "envio", "cupons",
                                     "links", "relatorios", "manual"),
                            required=True,
                            help="scrape = raspagem completa; scrape_rapido = feed flash; "
                                 "envio = envio pelas regras; cupons = manutenção "
                                 "independente; links = pré-gera links "
                                 "de afiliado dos pendentes.")
        parser.add_argument("--tick", type=int, default=5, help="Minutos entre ciclos (envio/flash/links).")
        parser.add_argument("--lote", type=int, default=40, help="Links gerados por ciclo, por usuário.")
        parser.add_argument("--scrape-horas", type=float, default=3.0, help="Horas entre raspagens completas.")

    @system_job
    def handle(self, *args, **opts):
        if opts["modo"] == "scrape":
            self._loop_scrape(opts)
        elif opts["modo"] == "scrape_rapido":
            self._loop_scrape_rapido(opts)
        elif opts["modo"] == "envio":
            self._loop_envio(opts)
        elif opts["modo"] == "cupons":
            self._loop_cupons(opts)
        elif opts["modo"] == "links":
            self._loop_links(opts)
        elif opts["modo"] == "manual":
            self._loop_manual(opts)
        else:
            self._loop_relatorios(opts)

    def _loop_manual(self, opts):
        from apps.scrapers.manual_scraping import (
            atualizar_diagnostico_fila, existe_job_pendente, processar_proximo_job,
        )
        from apps.scrapers.resource_control import (
            leased_resource, pulse_worker, worker_activity, worker_identity,
        )

        poll = 15
        worker_id = worker_identity("manual")
        logger.info("MANUAL worker no ar; consumidor dedicado da fila do painel")
        while True:
            try:
                _renovar_conexoes_db()
                pulse_worker("manual", worker_id=worker_id, state="idle")
                atualizar_diagnostico_fila()
                if not existe_job_pendente():
                    time.sleep(poll)
                    continue
                with leased_resource("django_chromium", owner_kind="manual") as (
                    acquired, detail,
                ):
                    if not acquired:
                        atualizar_diagnostico_fila(
                            resource_owner=detail.get("owner_kind", "scheduled"),
                        )
                        time.sleep(poll)
                        continue
                    with worker_activity("manual", worker_id, "manual_scraping"):
                        processar_proximo_job(
                            worker_id, detail.get("lease_token", ""),
                        )
            except DatabaseError as exc:
                logger.warning("Fila manual aguardando banco: %s", exc)
                connections.close_all()
                time.sleep(poll)
            except Exception:
                logger.exception("Falha no consumidor dedicado da fila manual")
                time.sleep(poll)

    def _loop_cupons(self, opts):
        tick = max(1, opts["tick"])
        lote = max(1, opts["lote"])
        poll = 15
        logger.info(
            "CUPONS worker no ar; ciclo a cada %smin, independente da raspagem geral",
            tick,
        )
        proximo = timezone.now()
        falhas_banco = 0
        while True:
            if timezone.now() < proximo:
                st.write_state("cupons", fase="aguardando")
                time.sleep(poll)
                continue
            agora = timezone.now()
            try:
                st.write_state("cupons", fase="processando", erro="")
                _renovar_conexoes_db()
                # HTTP, parsing e banco não seguram o slot global. Cada adaptador
                # que realmente abre Playwright adquire o lease no ponto de uso.
                with _heartbeat_durante("cupons"):
                    resultado = _rodar_cupons(lote=lote)
                falhas_banco = 0
                proximo = timezone.now() + timedelta(minutes=tick)
                falhas = resultado["falhos"] + resultado["links_falhos"]
                st.write_state(
                    "cupons",
                    fase="degradado" if falhas else "aguardando",
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=proximo.isoformat(),
                    encontrados=resultado["encontrados"],
                    persistidos=resultado["persistidos"],
                    preparados=resultado["preparados"],
                    vinculados=resultado["vinculados"],
                    links_gerados=resultado["links_gerados"],
                    links_verificados=resultado["links_verificados"],
                    prontos=resultado["prontos"],
                    falhas=falhas,
                    fontes=resultado["fontes"],
                    erro="" if not falhas else "Uma ou mais fontes/links falharam.",
                    ultima_msg=(
                        f"{resultado['prontos']} cupom(ns) pronto(s), "
                        f"{resultado['links_verificados']} link(s) verificado(s) "
                        f"às {agora:%H:%M}."
                    ),
                )
            except DatabaseError as exc:
                falhas_banco += 1
                proximo = _pausar_por_banco("cupons", exc, falhas_banco)
            except Exception as exc:
                logger.exception("Erro no ciclo central de cupons")
                log_event(
                    "scraper", "cupons_ciclo_erro",
                    f"Ciclo central de cupons falhou: {exc}",
                    level="error", exc=exc,
                )
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "cupons", fase="aguardando",
                    proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO,
                )

    def _loop_links(self, opts):
        # Gate no MESMO flag "scrape" (igual à lane flash): afiliar é parte do
        # pipeline de catálogo, e não faz sentido gerar link com a coleta desligada.
        tick = max(1, opts["tick"])
        lote = max(1, opts["lote"])
        POLL = 15
        logger.info("LINKS worker no ar; até %s link(s)/usuário a cada %smin quando ligado",
                    lote, tick)
        proximo = timezone.now()
        falhas_banco = 0
        while True:
            if not st.is_enabled("scrape"):
                # A lane de links não tem flag própria; herda a da raspagem. O texto
                # precisa dizer isso: "Desligado" sozinho não explicava por que a tela
                # de Promoções estava cheia de "pendente" com o worker no ar.
                st.write_state("links", fase="desligado",
                               ultima_msg="Parado porque a Raspagem está desligada — "
                                          "ligue na tela Scraper para voltar a gerar links.")
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
                # Geração e verificação adquirem o slot apenas enquanto o Chromium
                # está vivo. As queries que selecionam os lotes ficam fora do lease.
                with _heartbeat_durante("links"):
                    res = _rodar_links(lote=lote)
                st.write_state("links", fase="verificando", erro="")
                with _heartbeat_durante("links"):
                    ver = _rodar_verificacao_links(limite=lote)
                falhas_banco = 0
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "links", fase="aguardando", proximo_ciclo=proximo.isoformat(),
                    gerados=res["gerados"], falhas=res["falhas"], erro="",
                    verificados=ver["aprovados"], reprovados=ver["reprovados"],
                    ultima_msg=(f"{res['gerados']} link(s) gerado(s), "
                                f"{res['falhas']} falha(s), "
                                f"{ver['aprovados']} verificado(s) às {agora:%H:%M}."),
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("links", e, falhas_banco)
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
        falhas_banco = 0
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
                from apps.scrapers.carga import operacao_pesada
                with operacao_pesada(owner_kind="scrape_rapido") as acquired:
                    if not acquired:
                        proximo = timezone.now() + timedelta(seconds=POLL)
                        st.write_state("scrape_rapido", fase="aguardando_capacidade", erro="",
                                       proximo_ciclo=proximo.isoformat(),
                                       ultima_msg="Aguardando outra tarefa pesada terminar.")
                        continue
                    with _heartbeat_durante("scrape_rapido"):
                        _rodar_scrape_rapido()
                falhas_banco = 0
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("scrape_rapido", e, falhas_banco)
                continue
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
        falhas_banco = 0
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
                from apps.scrapers.carga import operacao_pesada
                with operacao_pesada(owner_kind="scrape") as acquired:
                    if not acquired:
                        proximo = timezone.now() + timedelta(seconds=POLL)
                        st.write_state("scrape", fase="aguardando_capacidade", erro="",
                                       proximo_ciclo=proximo.isoformat(),
                                       ultima_msg="Aguardando outra tarefa pesada terminar.")
                        continue
                    with _heartbeat_durante("scrape"):
                        resultado = _rodar_scrape()
                falhas_banco = 0
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
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("scrape", e, falhas_banco)
            except Exception as e:
                logger.exception("Erro no scrape")
                log_event("scraper", "scrape_erro", f"Ciclo de raspagem falhou: {e}",
                          level="error", contexto={"ciclos": ciclos}, exc=e)
                proximo = timezone.now() + timedelta(minutes=RETRY_MINUTOS)
                st.write_state("scrape", fase="aguardando", loja_atual=None,
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

    def _loop_envio(self, opts):
        from django.conf import settings
        from apps.scrapers.ofertas import processar_configs_de_envio

        def _consumir_fila_v2():
            if not settings.SEND_PIPELINE_V2_ENABLED:
                return []
            from apps.scrapers.send_pipeline import process_queued_publications
            return process_queued_publications(limit=20)

        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("ENVIO worker no ar; processa regras a cada %smin quando ligado", tick)
        ticks = 0
        ultima_purga = None  # data da última purga do log (1x/dia, ver abaixo)
        proximo = timezone.now()  # vencido: processa assim que ligarem
        falhas_banco = 0
        while True:
            if not st.is_enabled("envio"):
                st.write_state("envio", fase="desligado",
                               ultima_msg="Desligado — ligue na tela Envios.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                # Heartbeat também entre os ticks. O intervalo normal (~5min) é maior
                # que o TTL de 90s do worker_alive(), então sem renovar aqui um processo
                # vivo aparecia como morto/"Desligado" na tela — igual ao scrape.
                # Só o timestamp: fase/erro/proximo_ciclo já vêm do fim do tick, e
                # reescrevê-los aqui apagaria o erro do último ciclo na hora seguinte.
                st.write_state("envio")
                try:
                    fila = _consumir_fila_v2()
                    if fila:
                        logger.info("Fila de envio v2: %s lote(s) processado(s)", len(fila))
                except Exception:
                    logger.exception("Falha ao consumir fila de envio v2")
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
                    from apps.scrapers.maintenance import (
                        reconciliar_execucoes_ingestao_orfas,
                        reconciliar_publicacoes_orfas,
                    )
                    orfas = reconciliar_publicacoes_orfas()
                    if orfas:
                        logger.warning("%s publicacao(oes) orfa(s) fechada(s) como falha", orfas)
                    ingestoes_orfas = reconciliar_execucoes_ingestao_orfas()
                    if ingestoes_orfas:
                        logger.warning(
                            "%s execução(ões) de ingestão órfã(s) fechada(s)",
                            ingestoes_orfas,
                        )
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
                fila = _consumir_fila_v2()
                res = processar_configs_de_envio()
                falhas_banco = 0
                enviados = sum(1 for r in res if r.get("sucesso"))
                # O watchdog de conexões saiu daqui: virou o processo `monitor` do
                # Procfile. Como este loop é gated pela flag "envio", o watchdog
                # herdava o gate — envio desligado, ninguém via queda nem retomada
                # de conexão, e os incidentes ficavam abertos para sempre.
                ticks += 1
                logger.info("[%s] tick: %s config(s) vencida(s), %s enviada(s)", agora.strftime("%H:%M"), len(res), enviados)
                st.write_state(
                    "envio", fase="aguardando", ticks=ticks,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    vencidas=len(res), enviados=enviados, erro="",
                    fila_v2_processada=len(fila),
                    ultima_msg=f"{enviados} enviada(s) de {len(res)} vencida(s) às {agora:%H:%M}.",
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("envio", e, falhas_banco)
                continue
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
        falhas_banco = 0
        proximo = timezone.now()
        while True:
            if not st.is_enabled("relatorios"):
                st.write_state("relatorios", fase="desligado",
                               ultima_msg="Desligado — ligue quando quiser sync automático.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("relatorios", fase="aguardando_banco")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("relatorios", fase="sincronizando", erro="")
                _renovar_conexoes_db()
                # Cada adapter adquire, em ordem estável, o slot global de Chromium
                # e a sessão exclusiva do portal. Um lock externo aqui causaria
                # auto-contenção ao tentar adquirir `django_chromium` novamente.
                with _heartbeat_durante("relatorios"):
                    resultados = sync_due_reports()
                falhas_banco = 0
                if not resultados:
                    # Nada vencido: não é um ciclo, é silêncio. Não mexe no estado
                    # visível pra não zerar o "última sincronização" da tela.
                    st.write_state("relatorios", fase="aguardando")
                    proximo = timezone.now() + timedelta(seconds=POLL)
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
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("relatorios", e, falhas_banco)
            except Exception:
                logger.exception("Erro no sync de relatórios")
                st.write_state("relatorios", fase="aguardando", erro=ERRO_PUBLICO)
            time.sleep(POLL)
