"""
Estado dos loops de automação (scrape / envio), sem Redis/Celery e SEM depender
de processos Windows.

Modelo novo (portável p/ Linux/Fly): os loops rodam SEMPRE (honcho/Procfile).
Ligar/desligar pela tela apenas alterna um FLAG persistente ("enabled"); o loop
checa o flag a cada ciclo e fica ocioso quando desligado. Assim funciona igual em
Windows (dev) e no container (prod), sem Popen/taskkill.

  - "scrape": raspagem periódica (tela Scraper).
  - "envio":  envio pelas regras de ConfiguracaoEnvio (tela Envios).
  - "links":  geração/verificação de links; instalações antigas herdam "scrape"
    até a primeira configuração explícita.
"""
import json
import os
import sys
import subprocess
import time

from django.conf import settings

# Heartbeat: o loop grava estado a cada ~15s. Se o último estado é recente, existe
# um worker vivo (honcho em prod, ou subprocess destacado em dev). > isto = morto.
HEARTBEAT_STALE = 90

# Em produção o volume monta em /data; guarda o estado lá p/ sobreviver a deploy.
_DIR = os.path.join(getattr(settings, "ML_AUTH_DIR", "") or settings.BASE_DIR, ".automacao")
os.makedirs(_DIR, exist_ok=True)

JOBS = ("scrape", "envio", "links", "relatorios")

# Qual FLAG comanda cada fonte de ingestão. Existe para a tela poder separar
# "a fonte quebrou" de "ninguém mandou esta lane rodar" — dois diagnósticos com
# ações opostas que a faixa de Fontes mostrava com a mesma frase.
#
# Só entram aqui as fontes que dependem de flag. As demais (as de cupom, na lane
# `cupons`) rodam sempre e não têm liga/desliga: ausência = sem lane.
LANE_POR_FONTE = {
    "mercadolivre-web": "scrape",
    "mercadolivre-ofertas-flash": "scrape",
    "amazon-public-web": "scrape",
    "amazon-creators-api": "scrape",
}


def lane_da_fonte(slug: str):
    """Lane que comanda esta fonte, ou None quando ela não depende de flag."""
    return LANE_POR_FONTE.get(str(slug or ""))


def logfile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.log")


def statefile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.state.json")


def enabledfile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.enabled")


def configuredfile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.configured")


def links_herda_scrape() -> bool:
    """Compatibilidade até a lane receber sua primeira escolha explícita."""
    return not os.path.exists(configuredfile("links"))


# ── Flag liga/desliga ─────────────────────────────────────────
def is_enabled(job: str) -> bool:
    """Ligado = arquivo-flag existe. Default DESLIGADO (nada roda até o usuário ligar)."""
    if job == "links" and links_herda_scrape():
        return os.path.exists(enabledfile("scrape"))
    return os.path.exists(enabledfile(job))


def set_enabled(job: str, on: bool):
    if job == "links":
        # Um marcador separado distingue "a flag nova ainda não existe" de
        # "alguém desligou links explicitamente". Sem ele, desligar removeria o
        # arquivo e reativaria a herança imediatamente.
        with open(configuredfile(job), "w") as f:
            f.write(str(time.time()))
    if on:
        with open(enabledfile(job), "w") as f:
            f.write(str(time.time()))
    else:
        try:
            os.remove(enabledfile(job))
        except OSError:
            pass


# UI: "rodando" = ligado pelo usuário (o processo do loop está sempre vivo no honcho).
def is_running(job: str) -> bool:
    return is_enabled(job)


def iniciar(job: str):
    set_enabled(job, True)


def parar(job: str) -> bool:
    set_enabled(job, False)
    write_state(job, fase="desligado", ultima_msg="Desligado pelo usuário.")
    return True


# ── Heartbeat / estado ────────────────────────────────────────
def read_state(job: str) -> dict:
    """Heartbeat do loop: fase atual, último/próximo ciclo, contadores."""
    try:
        with open(statefile(job), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(job: str, **campos) -> dict:
    """Mescla campos no estado e grava atômico. Chamado pelo loop a cada fase."""
    estado = read_state(job)
    estado.update(campos)
    estado["atualizado_em"] = time.time()
    tmp = statefile(job) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(estado, f)
        os.replace(tmp, statefile(job))
    except Exception:
        pass
    return estado


def clear_state(job: str):
    try:
        os.remove(statefile(job))
    except OSError:
        pass


# ── Worker vivo? / spawn em dev ───────────────────────────────
def worker_alive(job: str) -> bool:
    """True se um processo-loop gravou heartbeat recente (honcho prod OU dev)."""
    ts = read_state(job).get("atualizado_em")
    return bool(ts) and (time.time() - ts) < HEARTBEAT_STALE


def _spawn_one(job: str):
    """Sobe UM worker destacado se não houver heartbeat vivo. Cross-platform."""
    if worker_alive(job):
        return
    manage = os.path.join(settings.BASE_DIR, "manage.py")
    args = [sys.executable, manage, "automacao", "--modo", job]
    # scrape = raspagem full (horas); envio = tick curto; relatorios = poucas vezes/dia.
    if job == "scrape":
        args += ["--scrape-horas", "3"]
    elif job == "relatorios":
        args += ["--tick", "360"]
    else:
        args += ["--tick", "5"]
    log = open(logfile(job), "a", encoding="utf-8")
    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
              "cwd": settings.BASE_DIR}
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: sem console, sobrevive ao request.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def spawn_worker(job: str):
    """DEV: sobe o worker do job. Em prod (honcho) o worker já roda e isto é no-op.

    Ligar 'scrape' também sobe a LANE FLASH (scrape_rapido) — em prod o Procfile já a
    roda; em dev ela não existiria sem isto (era o gap de paridade dev/prod)."""
    if settings.APP_ENV in {"staging", "production"}:
        return
    _spawn_one(job)
    if job == "scrape":
        _spawn_one("scrape_rapido")
