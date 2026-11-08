"""Teste de restauração: `python tools/restore_check.py`.

Faz o dump do Postgres do compose, restaura num Postgres 17 descartável e compara a contagem de
linhas de cada tabela. Backup que nunca foi restaurado não é backup: rode depois de mudar o schema e
de tempos em tempos. Sai com 0 se tudo bateu, 1 se faltou tabela ou linha.

O worker continua escrevendo durante o teste, então uma tabela viva pode ter ganho linhas depois do
dump: vale diferença de até 2% (mínimo 5 linhas). A sessão do WhatsApp não entra no backup (o estado
muda a cada mensagem): perdeu, re-pareia.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "spreading-restore-check"
TOLERANCE = 0.02
MIN_SLACK = 5


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, **kwargs)


def live(sql):
    out = run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "spreading", "-d",
               "spreading", "-tAc", sql])
    return out.stdout.decode().strip()


def restored(sql):
    out = run(["docker", "exec", NAME, "psql", "-U", "postgres", "-d", "postgres", "-tAc", sql])
    return out.stdout.decode().strip()


def counts(ask, names):
    query = " union all ".join(f"select '{name}', count(*) from \"{name}\"" for name in names)
    rows = [line.split("|") for line in ask(query).splitlines() if "|" in line]
    return {name: int(total) for name, total in rows}


def tables(ask):
    return ask("select tablename from pg_tables where schemaname = 'public' order by 1").split()


def main() -> int:
    dump = run(["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "spreading", "-Fc",
                "spreading"]).stdout
    if len(dump) < 1000:
        print("dump falhou (o compose está de pé? `docker compose up -d db`)")
        return 1
    print(f"dump: {len(dump) // 1024} KB")
    run(["docker", "rm", "-f", NAME])
    try:
        run(["docker", "run", "-d", "--rm", "--name", NAME, "-e", "POSTGRES_PASSWORD=t",
             "postgres:17"])
        for _attempt in range(40):
            if run(["docker", "exec", NAME, "pg_isready", "-U", "postgres"]).returncode == 0:
                break
            time.sleep(1)
        done = run(["docker", "exec", "-i", NAME, "pg_restore", "-U", "postgres", "-d", "postgres",
                    "--no-owner", "--no-privileges"], input=dump)
        if done.returncode not in (0, 1):  # 1 = avisos de papel/dono, o resto restaurou
            print("pg_restore falhou:", done.stderr.decode()[-300:])
            return 1
        names = tables(live)
        missing = sorted(set(names) - set(tables(restored)))
        want, got = counts(live, names), counts(restored, names)
        problems = [f"falta a tabela {name}" for name in missing]
        for name in names:
            behind = want[name] - got.get(name, 0)  # linhas que o banco ganhou depois do dump
            allowed = max(MIN_SLACK, want[name] * TOLERANCE)
            if name not in missing and (behind > allowed or behind < -MIN_SLACK):
                problems.append(f"{name}: banco {want[name]}, restaurado {got.get(name, 0)}")
        print(f"tabelas: {len(names)} | linhas no banco: {sum(want.values())} | "
              f"restauradas: {sum(got.values())}")
        for line in problems:
            print("DIFERENÇA:", line)
        print("restauração ok" if not problems else "restauração com diferenças")
        return 1 if problems else 0
    finally:
        run(["docker", "rm", "-f", NAME])


if __name__ == "__main__":
    sys.exit(main())
