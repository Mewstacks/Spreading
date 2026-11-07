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


def test_production_settings_fail_closed_on_weak_secret():
    assert _settings_import({}).returncode != 0
    assert _settings_import({"DJANGO_SECRET_KEY": "curta"}).returncode != 0
    assert _settings_import({"DJANGO_SECRET_KEY": "troque-por-uma-chave-longa"}).returncode != 0
    strong = "k" * 12 + "Z9x" * 20
    assert _settings_import({"DJANGO_SECRET_KEY": strong}).returncode == 0
    assert _settings_import({"DJANGO_DEBUG": "1"}).returncode == 0
