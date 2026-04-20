import os
import requests
import qrcode

# ── Config ────────────────────────────────────────────────────
# Set these as env vars or just hardcode for quick testing:
#   RAILWAY_URL  = https://your-app.up.railway.app
#   API_KEY      = sk_live_...
BASE_URL = os.environ.get("RAILWAY_URL", "https://spreading-production.up.railway.app")
API_KEY  = os.environ.get("API_KEY",  "sk_live_YIP2j2fe5yUWU1k9Uff2xjpt391nocGu")

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


def send_text(numero: str, mensagem: str):
    """Send a text message."""
    payload = {"numero": numero, "mensagem": mensagem}
    r = requests.post(f"{BASE_URL}/api/enviar/texto", json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def send_media(numero: str, base64: str, mimetype: str, nome_arquivo: str = "", legenda: str = ""):
    """Send a media message (image, video, audio, PDF, etc.)."""
    payload = {
        "numero": numero,
        "base64": base64,
        "mimetype": mimetype,
        "nomeArquivo": nome_arquivo,
        "legenda": legenda,
    }
    r = requests.post(f"{BASE_URL}/api/enviar/midia", json=payload, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


# ── Quick test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("Status:", status())

    # Fetch and open QR code as PNG (only needed when conectado=False)
    get_qr()

    # Send a test text — change the number
    # result = send_text("5511999999999", "Hello from Python!")
    # print(result)

