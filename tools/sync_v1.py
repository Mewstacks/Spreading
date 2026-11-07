"""Traz do v1 (somente leitura, via fly ssh) o que o v2 ainda não consegue descobrir sozinho:
links `meli.la` verificados, catálogo observado nos últimos 30 dias e pares cupom+produto.
Reimporta no v2 local (idempotente: não desfaz regra do painel). Rode quando quiser oferta e cupom
frescos enquanto o v1 estiver vivo:

    python tools/sync_v1.py

Nada de credencial sai do v1: os três scripts só leem dados de oferta e links publicados.
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".secrets" / "sync"
JOBS = (
    ("export_v1.py", "@@V1JSON@@", "v1_export.json"),
    ("export_catalog_v1.py", "@@V1CAT@@", "v1_catalog.json"),
    ("export_pairs_v1.py", "@@V1PAIRS@@", "v1_pairs.json"),
)


def run_in_v1(script: str, marker: str) -> str:
    payload = base64.b64encode((ROOT / "tools" / script).read_bytes()).decode()
    command = f"sh -c 'echo {payload} | base64 -d | python django/manage.py shell'"
    result = subprocess.run(["fly", "ssh", "console", "-a", "spreading-web", "-C", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = next((x for x in result.stdout.splitlines() if marker in x), "")
    if not line:
        raise RuntimeError(f"{script}: sem resposta do v1 ({result.stderr[-200:].strip()})")
    json.loads(line.split(marker, 1)[1])  # valida antes de gravar
    return line.split(marker, 1)[1]


def compose(*args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, **kwargs)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    for script, marker, name in JOBS:
        print(f"lendo {script} …", flush=True)
        (WORK / name).write_text(run_in_v1(script, marker), encoding="utf-8")
        compose("cp", str(WORK / name), f"web:/tmp/{name}")
    compose("exec", "-T", "web", "python", "manage.py", "import_v1", "/tmp/v1_export.json",
            "--catalog", "/tmp/v1_catalog.json", "--pairs", "/tmp/v1_pairs.json",
            "--org", "lules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
