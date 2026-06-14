"""Token de verificação de e-mail — assinado, com expiração, sem model extra.

Usa django.core.signing (HMAC com SECRET_KEY). O token carrega o pk do usuário e
expira por idade (max_age). Não precisa guardar nada no banco.
"""
from django.core import signing

_SALT = "accounts.email-verificacao"
# 3 dias para clicar no link de verificação.
MAX_AGE_SEG = 60 * 60 * 24 * 3


def gerar_token(user) -> str:
    return signing.dumps({"uid": user.pk}, salt=_SALT)


def ler_token(token: str):
    """Retorna o pk do usuário, ou None se inválido/expirado/adulterado."""
    try:
        data = signing.loads(token, salt=_SALT, max_age=MAX_AGE_SEG)
    except signing.SignatureExpired:
        return None
    except signing.BadSignature:
        return None
    return data.get("uid")
