"""Desafios de segurança compartilhados pelos logins web do Mercado Livre.

O login principal e o portal de relatórios rodam em Chromiums remotos sem câmera.
Quando o ML decide exigir reconhecimento facial, ele envia ambos para a mesma rota
``camera-not-found``.  A decisão de UI continua em cada worker; este módulo mantém
somente a detecção, para os dois fluxos não divergirem quando o texto/idioma mudar.
"""
from urllib.parse import urlparse


CAMERA_NOT_FOUND_PATH = "/jms/mlb/lgz/msl/login/camera-not-found"


def pagina_exige_configuracao_qr(page) -> bool:
    """Retorna ``True`` apenas para o bloqueio de câmera do login do ML.

    A URL é o sinal autoritativo e barato. O texto é fallback para variações de
    domínio/redirect vistas durante o carregamento. Toda leitura é best-effort:
    consultar ``page.url`` ou o DOM durante uma navegação pode levantar e nunca
    deve encerrar a tentativa de login.
    """
    try:
        caminho = (urlparse(str(page.url or "")).path or "").rstrip("/").lower()
    except Exception:
        caminho = ""
    if caminho == CAMERA_NOT_FOUND_PATH:
        return True

    try:
        texto = page.evaluate(
            "() => ((document.title || '') + ' ' +"
            " ((document.body && document.body.textContent) || ''))"
            ".slice(0, 5000).toLowerCase()"
        )
    except Exception:
        return False
    if not isinstance(texto, str):
        return False

    sem_camera = any(marca in texto for marca in (
        "não tem uma câmera", "nao tem uma camera",
        "no tiene una cámara", "no tiene una camara",
    ))
    reconhecimento = any(marca in texto for marca in (
        "reconhecimento facial", "reconocimiento facial",
    ))
    codigo_qr = "código qr" in texto or "codigo qr" in texto
    return sem_camera and reconhecimento and codigo_qr
