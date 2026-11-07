"""Backup do Postgres local: `python tools/backup.py` grava backups/spreading-AAAAMMDD-HHMM.dump e
mantém os 14 mais novos. Restaurar:

    docker compose exec -T db pg_restore -U spreading -d spreading --clean --no-owner < arquivo.dump

A sessão do WhatsApp não é backup restaurável (o estado muda a cada mensagem): perdeu, re-pareia.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP = 14


def main() -> int:
    out_dir = ROOT / "backups"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"spreading-{datetime.now():%Y%m%d-%H%M}.dump"
    with open(target, "wb") as handle:
        code = subprocess.call(
            ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "spreading", "-Fc",
             "spreading"], cwd=ROOT, stdout=handle)
    if code or target.stat().st_size < 1000:
        target.unlink(missing_ok=True)
        print("backup falhou")
        return 1
    for old in sorted(out_dir.glob("spreading-*.dump"))[:-KEEP]:
        old.unlink()
    print(f"{target} ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
