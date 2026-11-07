from decimal import Decimal

from apps.offers.models import AffiliateLink, Offer
from apps.offers.v1_catalog import import_catalog, import_pairs

IMG = "https://http2.mlstatic.com/D_Q_NP_2X_1.webp"


def row(**over):
    base = {"marketplace": "mercadolivre", "nome": "Robô Aspirador Dreame F10",
            "url": "https://www.mercadolivre.com.br/robo/p/MLB62695902", "imagem": IMG,
            "de": 2099.0, "efetivo": 1160.0, "macro": None, "asin": "",
            "visto": "2026-11-07T10:00:00+00:00"}
    base.update(over)
    return base


def test_import_catalog_maps_ml_and_amazon_and_discards_bad_rows(db):
    rows = [
        row(),
        row(marketplace="amazon", asin="B0CZPPGPJC", url="https://www.amazon.com.br/dp/B0CZPPGPJC",
            imagem="https://m.media-amazon.com/images/I/1.jpg", nome="Cafeteira Nespresso"),
        row(efetivo=2099.0),  # sem desconto
        row(imagem="https://evil.io/x.jpg"),  # foto fora da allowlist
        row(nome="Moto usada revisada"),  # item vedado
        row(marketplace="shopee"),
    ]
    stats = import_catalog(rows)
    assert stats == {"importados": 2, "descartados": 4}
    ml = Offer.objects.get(store="mercadolivre")
    assert ml.product_key == "MLB62695902" and ml.macro == "Eletrodomésticos"
    assert ml.discount_pct == Decimal("44.74") and ml.last_seen.year == 2026
    assert Offer.objects.get(store="amazon").product_key == "B0CZPPGPJC"
    assert import_catalog(rows)["importados"] == 2 and Offer.objects.count() == 2  # idempotente


def test_import_catalog_collapses_same_day_duplicates(db, monkeypatch):
    calls = []
    real = Offer.objects.bulk_create

    def spy(objs, **kwargs):
        calls.append([(o.store, o.link_key, o.valid_on) for o in objs])
        return real(objs, **kwargs)

    monkeypatch.setattr(Offer.objects, "bulk_create", spy)
    rows = [
        row(efetivo=1200.0, visto="2026-11-07T09:00:00+00:00"),
        row(efetivo=1160.0, visto="2026-11-07T10:00:00+00:00"),
        row(efetivo=1300.0, visto="2026-11-07T08:00:00+00:00"),  # mais velha, chega por último
    ]
    stats = import_catalog(rows)
    assert calls and all(len(keys) == len(set(keys)) for keys in calls)  # Postgres exige
    assert Offer.objects.count() == 1
    assert Offer.objects.get().price_to == Decimal("1160.00")  # fica a observação mais recente
    assert stats == {"importados": 1, "descartados": 2}


def pair(**over):
    base = {"marketplace": "mercadolivre", "nome": "Monitor LG 24 polegadas",
            "url": "https://www.mercadolivre.com.br/monitor/p/MLB1234567", "imagem": IMG,
            "macro": None, "preco_original": "199.98", "preco_atual": "145.00",
            "preco_final": "58.20", "verificado_em": "2026-11-07 13:27:35+00:00",
            "codigo": "APROVEITAMELI", "validade": "2026-11-30 23:59:59+00:00",
            "redemption_mode": "code", "restrito": False,
            "regras": {"tipo_desconto": "porcentagem", "valor_desconto": 30.0,
                       "desconto_maximo": 500.0, "valor_minimo": 1.0},
            "link_cupom": "https://www.mercadolivre.com.br/monitor/p/MLB1234567",
            "link_afiliado": "https://meli.la/2Ehvccw"}
    base.update(over)
    return base


def test_import_pairs_code_and_activation(org):
    rows = [
        pair(),
        pair(url="https://www.mercadolivre.com.br/x/p/MLB7654321", codigo="",
             redemption_mode="activation", link_afiliado="",
             link_cupom="https://www.mercadolivre.com.br/x/p/MLB7654321?coupon_campaign_id=13400211"),
        pair(restrito=True),  # primeira compra / novos clientes: bloqueado
        pair(url="https://www.mercadolivre.com.br/y/p/MLB1111111", preco_final="300"),  # sem desc.
        pair(url="https://www.mercadolivre.com.br/z/p/MLB2222222", redemption_mode="activation",
             link_cupom="https://www.mercadolivre.com.br/z/p/MLB2222222"),  # sem campanha
    ]
    stats = import_pairs(rows, org)
    assert stats == {"importados": 2, "descartados": 3, "links": 1}
    code = Offer.objects.get(coupon_kind="code")
    assert code.link_key == "MLB1234567+APROVEITAMELI" and code.price_to == Decimal("58.20")
    assert code.proof_base_price == Decimal("145.00") and code.coupon_pct == Decimal("30.00")
    assert code.coupon_cap == Decimal("500.00") and code.discount_pct == Decimal("70.90")
    act = Offer.objects.get(coupon_kind="activation")
    assert act.link_key == "MLB7654321+c13400211" and act.coupon_code == ""
    assert "coupon_campaign_id=13400211" in act.source_url
    assert AffiliateLink.objects.for_org(org).get().product_key == "MLB1234567+APROVEITAMELI"


def test_pair_link_to_social_profile_is_never_stored(org):
    social = "https://www.mercadolivre.com.br/social/lphoffmann?matt_word=x"
    import_pairs([pair(link_afiliado=social)], org)
    assert AffiliateLink.objects.for_org(org).count() == 0
