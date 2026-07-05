"""Criptografia simétrica (Fernet) p/ segredos por usuário em repouso.

Chave em settings.SECRETS_FERNET_KEY (Fly secret). Sem chave (dev), opera em
modo passthrough — NÃO criptografa — para não travar o ambiente local. Em
produção (DEBUG=0) a ausência da chave é erro explícito.

Gerar a chave:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_PREFIX = "fernet:"  # marca ciphertext p/ distinguir de valor legado em texto puro


def _fernet():
    key = getattr(settings, "SECRETS_FERNET_KEY", "") or ""
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured(
                "SECRETS_FERNET_KEY ausente em produção — segredos ficariam em texto."
            )
        return None  # dev: passthrough
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(texto: str) -> str:
    """Texto puro -> ciphertext marcado. Vazio continua vazio. Passthrough sem chave."""
    if not texto:
        return texto
    if texto.startswith(_PREFIX):  # já criptografado
        return texto
    f = _fernet()
    if f is None:
        return texto
    return _PREFIX + f.encrypt(texto.encode()).decode()


def decrypt(valor: str) -> str:
    """Ciphertext marcado -> texto puro. Valor legado (sem prefixo) volta como está."""
    if not valor or not valor.startswith(_PREFIX):
        return valor
    f = _fernet()
    if f is None:
        # Marcado como cifrado mas sem chave: não há como abrir. Devolve vazio.
        return ""
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(valor[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return ""
