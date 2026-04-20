import os
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
    """List all WhatsApp groups the account is in."""
    r = requests.get(f"{BASE_URL}/api/grupos", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("grupos", [])


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


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. Check connection
    print("Status:", status())

    # 2. Get QR if not connected (opens qr.png)
    # get_qr()

    # 3. List groups
    # groups = list_groups()
    # for g in groups:
    #     print(g["id"], "-", g["nome"])

    # 4. Send text to a contact
    # print(send_text(numero="5511999999999", mensagem="Oi!"))

    # 5. Send text to a group (paste the id from list_groups)
    # print(send_text(grupoid="120363XXXXXX@g.us", mensagem="Oi grupo!"))

    # 6. Send an image to a contact
    # import base64 as b64
    # with open("image.png", "rb") as f:
    #     data = b64.b64encode(f.read()).decode()
    # print(send_media(numero="5511999999999", base64=data, mimetype="image/png", legenda="test"))

    # 7. Send an image to a group
    # print(send_media(grupoid="120363XXXXXX@g.us", base64=data, mimetype="image/png", legenda="test"))

