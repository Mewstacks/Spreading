import base64
import mimetypes
import os
import time
import requests
import qrcode
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("BASE_URL", "")
API_KEY  = os.environ.get("API_KEY", "")

HEADERS = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
}
# ─────────────────────────────────────────────────────────────


def status():
    """Check if WhatsApp is connected."""
    r = requests.get(f"{BASE_URL}/api/status", timeout=10)
    r.raise_for_status()
    return r.json()


def get_qr(save_path: str = "qr.png"):
    """Fetch QR code from the API, save as PNG and open it."""
    r = requests.get(f"{BASE_URL}/api/qrcode", timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("conectado"):
        print("WhatsApp já está conectado, nenhum QR necessário.")
        return

    qr_string = data.get("qr")
    if not qr_string:
        print(data.get("mensagem", "QR ainda não disponível."))
        return

    img = qrcode.make(qr_string)
    img.save(save_path)
    print(f"QR Code salvo em {save_path} — abrindo...")
    os.startfile(save_path)


def list_groups():
    """List all WhatsApp groups. Retries if server is still loading."""
    while True:
        try:
            r = requests.get(f"{BASE_URL}/api/grupos", headers=HEADERS, timeout=40)
            if r.status_code == 503:
                print("⏳ Grupos ainda carregando no servidor, aguardando 5s...")
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json().get("grupos", [])
        except requests.exceptions.ReadTimeout:
            print("⏳ Servidor demorou para responder, tentando novamente...")
            time.sleep(3)


def send_text(numero: str = "", mensagem: str = "", grupoid: str = ""):
    """Send a text message to a contact (numero) or group (grupoid)."""
    payload = {"mensagem": mensagem}
    if grupoid:
        payload["grupoid"] = grupoid
    else:
        payload["numero"] = numero
    r = requests.post(f"{BASE_URL}/api/enviar/texto", json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def send_media(numero: str = "", base64: str = "", mimetype: str = "",
               nome_arquivo: str = "", legenda: str = "", grupoid: str = ""):
    """Send a media message to a contact (numero) or group (grupoid)."""
    payload = {
        "base64": base64,
        "mimetype": mimetype,
        "nomeArquivo": nome_arquivo,
        "legenda": legenda,
    }
    if grupoid:
        payload["grupoid"] = grupoid
    else:
        payload["numero"] = numero
    r = requests.post(f"{BASE_URL}/api/enviar/midia", json=payload, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    # 1. Aguarda WhatsApp conectar
    print("Verificando conexão com WhatsApp...")
    qr_shown = False
    while True:
        try:
            s = status()
            if s.get("conectado"):
                print("✅ WhatsApp conectado!")
                break
            if not qr_shown:
                print("⏳ Escaneie o QR Code para conectar.")
                get_qr()
                qr_shown = True
            else:
                print("⏳ Aguardando conexão...")
            time.sleep(5)
        except Exception as e:
            print(f"Erro ao verificar status: {e}. Tentando novamente em 5s...")
            time.sleep(5)

    # 2. Aguarda grupos sincronizarem no servidor (pode demorar no primeiro login)
    print("\nAguardando sincronização de grupos no servidor...")
    groups = list_groups()
    if not groups:
        print("Nenhum grupo encontrado.")
        raise SystemExit(0)

    # 3. Exibe lista de grupos
    print(f"\n{'#':<4} {'Nome'}")
    print("-" * 50)
    for i, g in enumerate(groups):
        print(f"{i:<4} {g['nome']}")

    # 4. Usuário escolhe o grupo
    print()
    while True:
        try:
            choice = int(input(f"Selecione o número do grupo (0-{len(groups)-1}): "))
            if 0 <= choice < len(groups):
                break
            print(f"Digite um número entre 0 e {len(groups)-1}.")
        except ValueError:
            print("Entrada inválida. Digite um número.")

    selected = groups[choice]
    print(f"\nGrupo selecionado: {selected['nome']} ({selected['id']})")

    # 5. Escolha: texto ou imagem
    tipo = input("\nEnviar (1) Texto ou (2) Imagem? [1]: ").strip()

    if tipo == "2":
        while True:
            caminho = input("Caminho da imagem: ").strip().strip('"')
            if os.path.isfile(caminho):
                break
            print("Arquivo não encontrado. Tente novamente.")

        legenda = input("Legenda (opcional, Enter para pular): ").strip()

        mimetype, _ = mimetypes.guess_type(caminho)
        if not mimetype:
            mimetype = "image/jpeg"

        with open(caminho, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        nome_arquivo = os.path.basename(caminho)
        result = send_media(
            grupoid=selected["id"],
            base64=b64,
            mimetype=mimetype,
            nome_arquivo=nome_arquivo,
            legenda=legenda,
        )
        print("Resultado:", result)
    else:
        msg = input("Mensagem de texto (Enter para usar padrão): ").strip()
        if not msg:
            msg = "🤖 Teste via API Python"

        result = send_text(grupoid=selected["id"], mensagem=msg)
        print("Resultado:", result)

