"""Relatório de saúde legível — o que quebrou nas últimas horas e o que fazer.

Existe para inverter a origem do diagnóstico: antes, quem descobria que o produto
tinha parado era a cliente testando na mão e avisando a gente. Os eventos já eram
gravados (EventoOperacional), mas ninguém lê uma lista crua de 30 linhas todo dia.

Duas decisões dão a forma deste módulo:

1. AGRUPAR, não listar. 40 falhas do mesmo grupo apagado são UM problema, não 40
   linhas. A tela mostra ocorrências agregadas por (pipeline, evento) com contagem,
   quem foi afetado e quando aconteceu por último.

2. TRADUZIR, não despejar. Cada evento tem uma entrada no CATALOGO com o que
   significa e o que fazer. "send_failed x12" não diz nada às 8h da manhã; "as
   ofertas não estão saindo para o grupo X — o WhatsApp dele caiu" diz.

O silêncio também é resposta: `sinais` conta os sucessos do período. Zero erro com
zero envio não é saúde, é worker desligado — e é isso que a seção de workers mostra.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone

from apps.scrapers import automacao_state as st
from apps.scrapers.models import EventoOperacional


# Cada entrada responde às duas únicas perguntas que importam num relatório diário:
# "isso é grave?" e "o que eu faço?". Evento sem entrada aqui ainda aparece na tela
# (com fallback), então esquecer de catalogar degrada a leitura, não esconde o erro.
CATALOGO = {
    # ── Publicação: o produto em si ──
    "config_pausada": {
        "titulo": "Automação pausada sozinha",
        "significa": "A regra de envio bateu o limite de falhas seguidas e se desligou. "
                     "O usuário parou de receber ofertas e não foi avisado.",
        "acao": "Veja o motivo abaixo. Resolvido, reative a regra na tela de Envios do usuário.",
    },
    "send_failed": {
        "titulo": "Oferta não foi entregue",
        "significa": "Uma publicação falhou. Falhas transitórias (worker piscou, timeout) "
                     "não contam contra a regra; as permanentes pausam a automação.",
        "acao": "Se repete no mesmo destino, confira se o grupo ainda existe e se o "
                "WhatsApp do dono está conectado.",
    },
    "tick_erro": {
        "titulo": "Ciclo de envio quebrou",
        "significa": "O worker de envio falhou no ciclo inteiro — nenhum usuário recebeu "
                     "oferta nessa rodada.",
        "acao": "Erro nosso, não do usuário. Veja o traceback e corrija no código.",
    },
    # ── Conexão: a causa raiz mais comum ──
    "conexao_caiu": {
        "titulo": "Conexão caiu",
        "significa": "O WhatsApp ou o Mercado Livre do usuário saiu do ar. Enquanto isso, "
                     "as ofertas dele não saem.",
        "acao": "O usuário precisa reconectar (parear o WhatsApp de novo ou refazer o "
                "login do ML). Cheque se o alerta por e-mail chegou nele.",
    },
    "conexao_voltou": {
        "titulo": "Conexão restabelecida",
        "significa": "A conexão voltou sozinha ou o usuário reconectou. Não é problema — "
                     "aparece para você ver quanto tempo ficou fora.",
        "acao": "Nenhuma.",
    },
    "watchdog_erro": {
        "titulo": "O monitor de conexões falhou",
        "significa": "Grave: é o watchdog que detecta queda de conexão. Com ele quebrado, "
                     "o sistema fica cego justamente para o que mais importa.",
        "acao": "Corrija com prioridade — enquanto isso, este relatório subestima as quedas.",
    },
    # ── Scraper: envenena o catálogo devagar ──
    "scrape_erro": {
        "titulo": "Ciclo de raspagem quebrou",
        "significa": "A coleta de ofertas falhou por inteiro. O catálogo para de receber "
                     "novidades e as ofertas enviadas vão ficando velhas.",
        "acao": "Veja o traceback. Normalmente é seletor que mudou ou bloqueio do site.",
    },
    "fonte_falhou": {
        "titulo": "Uma loja parou de responder",
        "significa": "Só uma fonte quebrou; as outras seguiram. O ciclo não falha, então "
                     "isso passa despercebido enquanto o catálogo daquela loja envelhece.",
        "acao": "Se repetir por dias, o seletor daquela loja provavelmente mudou.",
    },
    "flash_erro": {
        "titulo": "Feed rápido quebrou",
        "significa": "A lane de feed rápido do ML falhou. Impacto menor: a raspagem "
                     "completa ainda alimenta o catálogo.",
        "acao": "Se for isolado, ignore. Se repetir, investigue junto com o scrape.",
    },
    # ── Onboarding: o usuário nem entra ──
    "verificacao_nao_enviada": {
        "titulo": "Conta nova travada na porta",
        "significa": "O usuário se cadastrou mas o e-mail de verificação não saiu. O "
                     "middleware barra quem não verificou, então ele não consegue usar nada.",
        "acao": "Grave. Confira o SMTP (secrets EMAIL_*) e reenvie a verificação.",
    },
    "signup": {
        "titulo": "Conta criada",
        "significa": "Cadastro novo no sistema.",
        "acao": "Nenhuma.",
    },
    # ── Sistema ──
    "email_falhou": {
        "titulo": "E-mail não foi entregue",
        "significa": "O envio de e-mail falhou. Atinge verificação de conta, boas-vindas e "
                     "alerta de conexão — todos silenciosos por natureza.",
        "acao": "Cheque os secrets EMAIL_HOST_USER / EMAIL_HOST_PASSWORD no Fly.",
    },
    "sse_throttled": {
        "titulo": "Usuário limitado por excesso de execuções",
        "significa": "Alguém disparou um endpoint pesado várias vezes por minuto e foi "
                     "barrado. Proteção funcionando.",
        "acao": "Nenhuma, salvo se repetir muito — aí pode ser botão que redispara sozinho.",
    },
    # ── Relatórios de comissão ──
    "sync_failed": {
        "titulo": "Sincronização de comissão falhou",
        "significa": "Não foi possível buscar a receita de afiliado do marketplace. O "
                     "dashboard de ganhos fica desatualizado.",
        "acao": "Veja o traceback; costuma ser sessão do marketplace expirada.",
    },
    "sync_action_required": {
        "titulo": "Conta precisa ser reconectada",
        "significa": "O marketplace pediu login de novo para liberar o relatório de comissão.",
        "acao": "Só o usuário resolve: peça para ele reconectar a conta.",
    },
}

_FALLBACK = {
    "titulo": "",
    "significa": "Evento ainda não catalogado em saude.CATALOGO.",
    "acao": "Veja a mensagem e o traceback abaixo.",
}

# Sucessos que provam que o sistema está vivo. Sem isto, "nenhum erro" é ambíguo:
# pode ser tudo funcionando ou tudo parado.
SINAIS = (
    ("send_ok", "Ofertas publicadas"),
    ("sync_ok", "Relatórios sincronizados"),
    ("signup", "Contas criadas"),
    ("conexao_voltou", "Reconexões"),
)

# Nomes de worker legíveis (JOBS do automacao_state + o que cada um faz).
WORKERS = (
    ("scrape", "Raspagem de ofertas"),
    ("envio", "Envio de ofertas"),
    ("relatorios", "Relatórios de comissão"),
)


def descrever(evento: str) -> dict:
    """Tradução humana de um evento. Nunca levanta: evento novo cai no fallback."""
    info = CATALOGO.get(evento) or _FALLBACK
    return {**_FALLBACK, **info, "titulo": info.get("titulo") or evento}


def _problemas(qs) -> list[dict]:
    """Erros e avisos agrupados por (pipeline, evento), mais grave e mais frequente no topo."""
    grupos = (
        qs.filter(level__in=("error", "warning"))
        .values("pipeline", "evento", "level")
        .annotate(n=Count("id"), ultimo=Max("criado_em"),
                  usuarios=Count("usuario", distinct=True))
    )
    out = []
    for g in grupos:
        # Um exemplo concreto por grupo: a contagem diz o tamanho, a mensagem diz o quê.
        # São poucos grupos (dezenas no pior caso), então N+1 aqui é irrelevante.
        exemplo = (
            qs.filter(pipeline=g["pipeline"], evento=g["evento"], level=g["level"])
            .select_related("usuario").order_by("-criado_em").first()
        )
        afetados = list(
            qs.filter(pipeline=g["pipeline"], evento=g["evento"], level=g["level"],
                      usuario__isnull=False)
            .values_list("usuario__username", flat=True).distinct()[:5]
        )
        out.append({
            **g, **descrever(g["evento"]),
            "exemplo": exemplo,
            "afetados": afetados,
            "critico": g["level"] == "error",
        })
    # Erros antes de avisos; dentro de cada faixa, o mais frequente primeiro.
    out.sort(key=lambda p: (p["level"] != "error", -p["n"]))
    return out


def _workers() -> list[dict]:
    """Estado dos loops. Explica o silêncio: zero erro com worker parado não é saúde."""
    out = []
    for job, nome in WORKERS:
        try:
            ligado = st.is_enabled(job)
            vivo = st.worker_alive(job)
            estado = st.read_state(job) or {}
        except Exception:
            ligado, vivo, estado = False, False, {}
        out.append({
            "job": job, "nome": nome, "ligado": ligado, "vivo": vivo,
            "fase": estado.get("fase", "?"),
            "ultima_msg": estado.get("ultima_msg", ""),
            # Ligado mas sem processo vivo é o pior caso: a tela diz "ligado" e
            # nada roda. É o que o usuário chamaria de "o site parou sozinho".
            "alerta": ligado and not vivo,
        })
    return out


def resumo(horas: int = 24, agora=None) -> dict:
    """Fotografia do período: veredito, problemas agrupados, sinais de vida, workers."""
    agora = agora or timezone.now()
    desde = agora - timedelta(hours=horas)
    qs = EventoOperacional.objects.filter(criado_em__gte=desde)

    problemas = _problemas(qs)
    n_erros = sum(p["n"] for p in problemas if p["critico"])
    n_avisos = sum(p["n"] for p in problemas if not p["critico"])
    grupos_criticos = sum(1 for p in problemas if p["critico"])

    contagens = dict(
        qs.values_list("evento").annotate(n=Count("id")).values_list("evento", "n")
    )
    sinais = [{"nome": nome, "n": contagens.get(ev, 0)} for ev, nome in SINAIS]
    workers = _workers()

    if grupos_criticos:
        estado = "critico"
        texto = (f"{grupos_criticos} problema{'s' if grupos_criticos > 1 else ''} "
                 f"precisa{'m' if grupos_criticos > 1 else ''} de atenção")
    elif any(w["alerta"] for w in workers):
        estado = "critico"
        texto = "Nenhum erro registrado, mas há worker ligado que não está rodando"
    elif problemas:
        estado = "atencao"
        texto = f"{len(problemas)} aviso{'s' if len(problemas) > 1 else ''}, nada crítico"
    else:
        estado = "ok"
        texto = f"Nenhum erro nas últimas {horas}h"

    return {
        "horas": horas, "desde": desde, "agora": agora,
        "estado": estado, "texto": texto,
        "problemas": problemas,
        "n_erros": n_erros, "n_avisos": n_avisos,
        "sinais": sinais, "workers": workers,
        "total": qs.count(),
    }
