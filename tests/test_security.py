import ast
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from apps.coupons.feed import normalize, safe_container_url

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 7)


def row(**over):
    base = {"nome": "OKCODE", "dia_inicio": "01/11/2026", "dia_fim": "15/11/2026",
            "valor_desconto": "10%"}
    base.update(over)
    return base


def test_hostile_coupon_codes_are_dropped():
    hostile = ["<script>alert(1)</script>", "AB", "A" * 41, "COM ESPACO", "x\nINJETA", "$(rm -rf)"]
    for bad in hostile:
        assert normalize(row(nome=bad), TODAY) is None, bad
    assert normalize(row(nome="meu-vale_10"), TODAY).code == "MEU-VALE_10"


def test_container_url_allowlist():
    ok = "https://lista.mercadolivre.com.br/_Container_aff-list-28"
    assert safe_container_url(ok) == ok
    for bad in [
        "http://lista.mercadolivre.com.br/x",
        "https://evil.example.com/mercadolivre.com.br",
        "https://mercadolivre.com.br.evil.io/x",
        "javascript:alert(1)",
        "",
    ]:
        assert safe_container_url(bad) == "", bad
    assert normalize(row(container_url="https://evil.io/x"), TODAY).container_url == ""


def _settings_import(env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "import spreading.settings"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def _setting_values(env_extra, *names):
    strong = "k" * 12 + "Z9x" * 20
    code = "import spreading.settings as s; print([getattr(s, n, None) for n in %r])" % (names,)
    env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    env.update({"DJANGO_SECRET_KEY": strong, **env_extra})
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return ast.literal_eval(done.stdout.strip())


def test_behind_tls_the_cookies_redirect_and_hsts_are_hardened():
    names = ("SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
             "SECURE_HSTS_SECONDS", "SECURE_PROXY_SSL_HEADER", "SECURE_REDIRECT_EXEMPT")
    assert _setting_values({}, *names) == [None, None, None, None, None, None]  # http local
    assert _setting_values({"DJANGO_HTTPS": "1"}, *names) == [
        True, True, True, 2592000, ("HTTP_X_FORWARDED_PROTO", "https"), [r"^healthz$"]]
    assert _setting_values({"DJANGO_HTTPS": "1", "DJANGO_HSTS_SECONDS": "60"},
                           "SECURE_HSTS_SECONDS") == [60]


def test_pages_refuse_to_be_framed(client, db):
    assert client.get("/healthz")["X-Frame-Options"] == "DENY"


def test_production_settings_fail_closed_on_weak_secret():
    assert _settings_import({}).returncode != 0
    assert _settings_import({"DJANGO_SECRET_KEY": "curta"}).returncode != 0
    assert _settings_import({"DJANGO_SECRET_KEY": "troque-por-uma-chave-longa"}).returncode != 0
    strong = "k" * 12 + "Z9x" * 20
    assert _settings_import({"DJANGO_SECRET_KEY": strong}).returncode == 0
    assert _settings_import({"DJANGO_DEBUG": "1"}).returncode == 0
