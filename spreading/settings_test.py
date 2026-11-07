"""Settings só para testes (a chave fraca daqui nunca é usada fora do pytest)."""
from .base import *  # noqa: F403

SECRET_KEY = "test-only-key-not-for-any-real-environment-0123456789abcdef"
