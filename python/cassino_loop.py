import msvcrt
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("BASE_URL", "")
API_KEY  = os.environ.get("API_KEY", "")

HEADERS = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
}

MENSAGEM = "/cassino"
DELAY    = 5  # segundos entre cada envio


def list_groups():
    while True:
        try:
            r = requests.get(f"{BASE_URL}/api/grupos", headers=HEADERS, timeout=40)
            if r.status_code == 503:
                print("⏳ Grupos ainda carregando, aguardando 5s...")
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json().get("grupos", [])
        except requests.exceptions.ReadTimeout:
            print("⏳ Timeout, tentando novamente...")
            time.sleep(3)


def refresh_groups():
    """Pede ao servidor para rebuscar os grupos do WhatsApp e retorna a lista atualizada."""
    print("⏳ Solicitando refresh ao servidor...")
    r = requests.post(f"{BASE_URL}/api/grupos/refresh", headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"✅ Refresh concluído: {data.get('total', 0)} grupos.")
    return data.get("grupos", [])


def selecionar_grupo(groups):
    print(f"\n{'#':<4} {'Nome'}")
    print("-" * 50)
    for i, g in enumerate(groups):
        print(f"{i:<4} {g['nome']}")
    print()
    while True:
        try:
            choice = int(input(f"Selecione o grupo (0-{len(groups)-1}): "))
            if 0 <= choice < len(groups):
                return groups[choice]
            print(f"Digite entre 0 e {len(groups)-1}.")
        except ValueError:
            print("Entrada inválida.")


    r = requests.post(
        f"{BASE_URL}/api/enviar",
        json={"grupoid": grupoid, "mensagem": mensagem},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    print("Carregando grupos...")
    try:
        groups = refresh_groups()
    except Exception as e:
        print(f"Erro no refresh: {e}. Usando GET normal...")
        groups = list_groups()
    if not groups:
        print("Nenhum grupo encontrado.")
        raise SystemExit(0)

    selected = selecionar_grupo(groups)
    print(f"\nGrupo: {selected['nome']} ({selected['id']})")
    print(f"Enviando '{MENSAGEM}' a cada {DELAY}s")
    print("Pressione 'r' para trocar de grupo | 'q' para sair\n")

    # 2. Loop infinito
    count = 0
    while True:
        # Verifica tecla pressionada (non-blocking)
        if msvcrt.kbhit():
            tecla = msvcrt.getch().decode(errors="ignore").lower()
            if tecla == "r":
                print("\n🔄 Buscando grupos novamente...")
                try:
                    groups = refresh_groups()
                except Exception as e:
                    print(f"Erro no refresh: {e}. Usando lista em cache...")
                    groups = list_groups()
                if not groups:
                    print("Nenhum grupo encontrado.")
                    raise SystemExit(0)
                selected = selecionar_grupo(groups)
                count = 0
                print(f"\nGrupo: {selected['nome']} ({selected['id']})")
                print(f"Enviando '{MENSAGEM}' a cada {DELAY}s")
                print("Pressione 'r' para trocar de grupo | 'q' para sair\n")
            elif tecla == "q":
                print("\nEncerrando.")
                break
        try:
            count += 1
            result = send_text(grupoid=selected["id"], mensagem=MENSAGEM)
            print(f"[{count}] Enviado — {result}")
        except Exception as e:
            print(f"[{count}] Erro: {e}")
        time.sleep(DELAY)
