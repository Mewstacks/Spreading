from datetime import date
from decimal import Decimal

from apps.coupons.feed import extract_array, ingest, parse_br_number, parse_feed
from apps.coupons.models import Coupon
from tests.conftest import NOW

TODAY = date(2026, 11, 7)


def page(rows_json: str) -> str:
    return f"<html><script>const COUPONS = {rows_json};\nconst X = [1];</script></html>"


ROWS = """[
 {"nome": "MEUVALEMELI", "acao": "Sellers", "dia_inicio": "01/11/2026", "dia_fim": "15/11/2026",
  "valor_desconto": "30,00%", "min_compra": "1", "desconto_max": "500",
  "container_url": "https://lista.mercadolivre.com.br/_Container_aff-list-28",
  "is_mar_aberto": false},
 {"nome": "VENCIDO", "dia_inicio": "01/10/2026", "dia_fim": "06/11/2026", "valor_desconto": "10%"},
 {"nome": "FUTURO", "dia_inicio": "20/11/2026", "dia_fim": "30/11/2026", "valor_desconto": "10%"},
 {"nome": "", "dia_inicio": "01/11/2026", "dia_fim": "30/11/2026"},
 {"nome": "REAIS", "dia_inicio": "01/11/2026", "dia_fim": "09/11/2026",
  "valor_desconto": "R$ 70,00", "min_compra": "399", "desconto_max": "70",
  "container_url": "https://lista.mercadolivre.com.br/_Container_x", "is_mar_aberto": false}
]"""


def test_parse_br_number():
    assert parse_br_number("30,00%") == Decimal("30.00")
    assert parse_br_number("1.199") == Decimal("1199")
    assert parse_br_number("129.90") == Decimal("129.90")
    assert parse_br_number("R$ 1.199,90") == Decimal("1199.90")
    assert parse_br_number("") is None
    assert parse_br_number("abc") is None


def test_extract_array_ignores_brackets_inside_strings():
    html = page('[{"nome": "A]B", "dia_fim": "01/12/2026"}]')
    assert extract_array(html)[0]["nome"] == "A]B"


def test_extract_array_missing_or_broken():
    assert extract_array("<html></html>") == []
    assert extract_array("const COUPONS = [{broken") == []


def test_parse_feed_filters_expired_future_and_nameless():
    coupons = parse_feed(page(ROWS), TODAY)
    assert [c.code for c in coupons] == ["MEUVALEMELI", "REAIS"]
    first = coupons[0]
    assert first.discount_pct == Decimal("30.00") and first.discount_amount is None
    assert first.max_discount == Decimal("500") and first.min_purchase == Decimal("1")
    assert first.valid_until == date(2026, 11, 15)
    assert coupons[1].discount_amount == Decimal("70.00") and coupons[1].discount_pct is None


def test_ingest_is_idempotent_and_stamps_last_seen(db):
    assert ingest(page(ROWS), now=NOW) == 2
    assert ingest(page(ROWS), now=NOW) == 2
    assert Coupon.objects.count() == 2
    assert Coupon.objects.get(code="MEUVALEMELI").last_seen == NOW
