"""Settings só para testes (a chave fraca daqui nunca é usada fora do pytest)."""
from .base import *  # noqa: F403

SECRET_KEY = "test-only-key-not-for-any-real-environment-0123456789abcdef"
# Nenhum teste vai à rede por acidente; test_ml_page.py liga por conta própria.
ML_PUBLIC_PAGES = False
SHOPEE_TRACK = False  # nenhum envio de teste chama a Shopee; test_shopee.py liga por conta própria
