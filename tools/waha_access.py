"""Entrar no dashboard do WAHA sem abrir o .env: `python tools/waha_access.py`.

O dashboard (http://localhost:3000/dashboard/) pede primeiro o login básico do navegador (usuário e
senha do `.env`) e, depois, o servidor: Name, API URL e API Key. O aviso vermelho "WAHA_API_KEY is
not set or using a default value" é do próprio dashboard e some quando a chave é colada no campo.

Este script abre o navegador e põe cada segredo na área de transferência na hora de colar. Nenhum
valor é impresso.
"""
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = "http://localhost:3000/dashboard/"
API_URL = "http://localhost:3000"
CLIPBOARD = (["clip"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"])


def read_env(path) -> dict:
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def copy(text: str) -> bool:
    """Põe `text` na área de transferência; False se o sistema não tem ferramenta para isso."""
    for command in CLIPBOARD:
        if shutil.which(command[0]):
            data = text.encode("utf-16") if command[0] == "clip" else text.encode("utf-8")
            return subprocess.run(command, input=data, check=False).returncode == 0
    return False


def main(argv=None, env_path=None, copier=copy, opener=webbrowser.open, ask=input) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = read_env(env_path or ROOT / ".env")
    user = env.get("WAHA_DASHBOARD_USERNAME") or "admin"
    steps = [
        ("WAHA_DASHBOARD_PASSWORD", f"Login: usuário {user}", "senha"),
        ("WAHA_API_KEY", f"Server: Name local, API URL {API_URL}", "API Key"),
    ]
    missing = [name for name, _head, _what in steps if not env.get(name)]
    if missing:
        print("faltam no .env: " + ", ".join(missing))
        return 1
    if "--no-open" not in argv:
        opener(DASHBOARD)
    for name, head, what in steps:
        if copier(env[name]):
            print(f"{head}; {what} copiada (Ctrl+V).")
        else:
            print(f"{head}; sem área de transferência, copie {name} do .env.")
        try:
            ask("Enter para o próximo passo ")
        except EOFError:
            pass
    copier("")  # não deixa o segredo na área de transferência
    print("Área de transferência limpa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
