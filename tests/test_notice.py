from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.compose.notice import brl, build_coupon_notice, pct


def coupon(code, pct_=Decimal("30"), cap=Decimal("500"), minp=Decimal("1")):
    return SimpleNamespace(
        code=code, discount_pct=pct_, discount_amount=None, max_discount=cap,
        min_purchase=minp, valid_until=date(2026, 11, 15),
    )


def test_money_and_percent_formatting():
    assert brl(Decimal("500")) == "R$ 500"
    assert brl(Decimal("1199.90")) == "R$ 1.199,90"
    assert brl(Decimal("1500")) == "R$ 1.500"
    assert pct(Decimal("30.00")) == "30%"
    assert pct(Decimal("12.5")) == "12,5%"


def test_notice_golden():
    text = build_coupon_notice([coupon("MEUVALEMELI"), coupon("VALEAPENA", Decimal("25"))],
                               "https://meli.la/abc123")
    assert text == (
        "🎟️ *Cupons Mercado Livre*\n\n"
        "*MEUVALEMELI* · 30% OFF · até R$ 500 · compra mín. R$ 1 · até 15/11\n"
        "*VALEAPENA* · 25% OFF · até R$ 500 · compra mín. R$ 1 · até 15/11\n\n"
        "🔗 https://meli.la/abc123"
    )


def test_notice_has_no_affiliation_banner_and_max_three_codes():
    text = build_coupon_notice([coupon(f"C{i}") for i in range(5)], "https://meli.la/x")
    assert text.count("OFF") == 3
    assert "publi" not in text.lower()


def test_notice_requires_coupons():
    with pytest.raises(ValueError):
        build_coupon_notice([], "https://meli.la/x")
