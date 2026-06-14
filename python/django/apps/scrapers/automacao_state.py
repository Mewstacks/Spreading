"""
Estado dos processos de automação (loops em background), sem Redis/Celery.
Dois jobs independentes, cada um com seu PID file:
  - "scrape": raspagem periódica 24/7 (tela Scraper).
  - "envio":  envio pelas regras de ConfiguracaoEnvio (tela Envios).
Um não afeta o outro. Controle via PID file: start (Popen destacado), status, stop.
"""
import json
import os
import subprocess
import time

from django.conf import settings

_DIR = os.path.join(settings.BASE_DIR, ".automacao")
os.makedirs(_DIR, exist_ok=True)

JOBS = ("scrape", "envio")


def pidfile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.pid")


def logfile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.log")


def statefile(job: str) -> str:
    return os.path.join(_DIR, f"{job}.state.json")


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


def get_pid(job: str):
    try:
        with open(pidfile(job)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_pid(job: str, pid: int):
    with open(pidfile(job), "w") as f:
        f.write(str(pid))


def clear_pid(job: str):
    try:
        os.remove(pidfile(job))
    except OSError:
        pass


def is_running(job: str) -> bool:
    pid = get_pid(job)
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return str(pid) in out.stdout
    except Exception:
        return False


def parar(job: str) -> bool:
    """Mata o processo do job (e filhos)."""
    pid = get_pid(job)
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    clear_pid(job)
    clear_state(job)
    return True
