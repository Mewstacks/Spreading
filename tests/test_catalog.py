from datetime import timedelta
from decimal import Decimal

import pytest

from apps.offers import catalog, ml_api, niches
from apps.offers.models import Offer, Rule
from tests.conftest import NOW

SUB = niches.sub_key("Eletrodomésticos", "Robô aspirador")


@pytest.fixture
def api(monkeypatch):
    calls = {"search": [], "price": []}

    def search(term, limit=20, http=None):
        calls["search"].append(term)
        return [{"id": "MLB900", "name": "Robô Aspirador X",
                 "pictures": [{"url": "https://http2.mlstatic.com/a.jpg"}]}]

    def price(product_id, http=None):
        calls["price"].append(product_id)
        return {"price": 100.0, "price_from": 200.0, "winner_tagged": True}

    monkeypatch.setattr(ml_api, "search_products", search)
    monkeypatch.setattr(ml_api, "product_price", price)
    return calls


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Grupo",
                                niches=[SUB], terms="air fryer")


def test_collects_terms_from_active_rules_only(rule, api):
    expected = set(catalog.rule_terms(rule))
    assert "air fryer" in expected and len(expected) > 1  # sub-nicho + termo extra
    assert catalog.collect_catalog(NOW) >= 1
    assert set(api["search"]) == expected
    offer = Offer.objects.get(product_key="MLB900")
    assert offer.discount_pct == Decimal("50.00") and offer.source == catalog.SOURCE
    assert offer.valid_on == NOW.date()


def test_paused_rule_collects_nothing(rule, api):
    rule.active = False
    rule.save()
    assert catalog.collect_catalog(NOW) == 0
    assert api["search"] == []


def test_price_without_official_list_price_is_not_collected(rule, api, monkeypatch):
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 100.0, "price_from": None, "winner_tagged": True})
    assert catalog.collect_catalog(NOW) == 0
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 100.0, "price_from": 200.0, "winner_tagged": False})
    assert catalog.collect_catalog(NOW + timedelta(hours=2)) == 0
    assert not Offer.objects.exists()


def test_each_term_is_refreshed_at_most_once_an_hour(rule, api):
    catalog.collect_catalog(NOW)
    first = len(api["search"])
    assert first == len(set(catalog.rule_terms(rule)))
    catalog.collect_catalog(NOW + timedelta(minutes=30))  # o ciclo roda de 5 em 5 minutos
    assert len(api["search"]) == first
    rule.terms = "air fryer, cafeteira"
    rule.save()
    catalog.collect_catalog(NOW + timedelta(minutes=35))  # termo novo entra na hora
    assert api["search"][first:] == ["cafeteira"]
    catalog.collect_catalog(NOW + timedelta(minutes=61))  # os antigos vencem depois de 1 h
    assert len(api["search"]) == first + 1 + (first)  # todos de novo, menos o novo (35 min)
