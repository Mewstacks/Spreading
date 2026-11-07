"""Verificação do repo: ruff + pytest. Usa o .venv local se existir."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _python() -> str:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = ROOT / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


def main() -> int:
    py = _python()
    steps = [
        [py, "-m", "ruff", "check", "."],
        [py, "-m", "pytest"],
    ]
    for cmd in steps:
        print("$", " ".join(cmd), flush=True)
        code = subprocess.call(cmd, cwd=ROOT)
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
