from apps.offers.models import AffiliateLink, Rule
from apps.orgs.models import ContainerLink, Destination, Organization
from apps.orgs.v1_import import import_v1

DATA = {"orgs": [{
    "id": "x", "name": "lules",
    "rules": [
        {"id": 104, "tipo": "ofertas", "canal": "whatsapp", "grupo_id": "120@g.us",
         "grupo_nome": "Teste ofertas", "marketplace": "mercadolivre", "termo_busca": "robo",
         "termos_negativos": "filtro", "preco_min": None, "preco_max": 900.0,
         "min_desconto_percent": 15.0, "janela_inicio": 8, "janela_fim": 20, "dias_semana": "",
         "intervalo_minutos": 60, "max_envios_dia": 99, "horas_cooldown": 360, "ativo": True},
        {"id": 105, "tipo": "ofertas", "canal": "whatsapp", "grupo_id": "120@g.us",
         "grupo_nome": "Teste ofertas", "marketplace": "amazon"},
        {"id": 106, "tipo": "ofertas", "canal": "telegram", "grupo_id": "@c", "marketplace": ""},
    ],
    "container_links": [
        {"container_url": "https://lista.mercadolivre.com.br/_Container_a", "affiliate_url":
         "https://meli.la/c1"},
        {"container_url": "https://evil.io/x", "affiliate_url": "https://meli.la/c2"},
    ],
    "product_links": [
        {"url": "https://produto.mercadolivre.com.br/MLB-111111111", "affiliate_url":
         "https://meli.la/p1", "verificado": True},
        {"url": "https://produto.mercadolivre.com.br/MLB-111111111", "affiliate_url":
         "https://meli.la/dup", "verificado": True},
        {"url": "https://produto.mercadolivre.com.br/MLB-222222222", "affiliate_url":
         "http://insegura.example/p2", "verificado": True},
    ],
}]}


def test_import_maps_rules_and_filters_unsafe(db):
    stats = import_v1(DATA)
    assert stats == {"orgs": 1, "regras": 1, "regras_puladas": 2, "containers": 1,
                     "produtos": 1, "links_rejeitados": 2}
    org = Organization.objects.get(slug="lules")
    rule = Rule.objects.for_org(org).get()
    assert rule.terms == "robo" and rule.daily_cap == 99 and rule.effective_cap == 30
    assert Destination.objects.for_org(org).count() == 1
    assert ContainerLink.objects.for_org(org).count() == 1
    assert AffiliateLink.objects.for_org(org).get().product_key == "MLB111111111"


def test_import_keeps_one_offers_rule_per_group(db):
    twin = {"id": 107, "tipo": "ofertas", "canal": "whatsapp", "grupo_id": "120@g.us",
            "grupo_nome": "Teste ofertas", "marketplace": "", "intervalo_minutos": 5}
    notice = {"id": 108, "tipo": "aviso_cupons", "canal": "whatsapp", "grupo_id": "120@g.us",
              "grupo_nome": "Teste ofertas", "marketplace": ""}
    data = {"orgs": [{**DATA["orgs"][0], "rules": [DATA["orgs"][0]["rules"][0], twin, notice]}]}
    stats = import_v1(data)
    assert stats["regras"] == 2 and stats["regras_puladas"] == 1  # a gêmea de ofertas some
    org = Organization.objects.get(slug="lules")
    assert sorted(Rule.objects.for_org(org).values_list("name", flat=True)) == ["v1-104", "v1-108"]
    import_v1(data)  # reimportar não descarta a regra já aceita
    assert Rule.objects.for_org(org).count() == 2


def test_import_refuses_storefront_and_unverified_product_links(db):
    def entry(n, url, verified):
        row = {"url": f"https://produto.mercadolivre.com.br/MLB-{n}{n}{n}{n}{n}{n}{n}{n}{n}",
               "affiliate_url": url}
        if verified != "sem-flag":
            row["verificado"] = verified
        return row

    social = "https://www.mercadolivre.com.br/social/loja?matt_word=x&matt_tool=1&ref=abc"
    data = {"orgs": [{"id": "x", "name": "lules", "rules": [], "container_links": [],
                      "product_links": [
                          entry(1, "https://meli.la/ok", True),
                          entry(2, social, True),  # vitrine do afiliado, não o produto
                          entry(3, "https://meli.la/naoverificado", None),
                          entry(4, "https://meli.la/semflag", "sem-flag"),
                      ]}]}
    stats = import_v1(data)
    org = Organization.objects.get(slug="lules")
    assert list(AffiliateLink.objects.for_org(org).values_list("product_key", flat=True)) == [
        "MLB111111111"]
    assert stats["produtos"] == 1 and stats["links_rejeitados"] == 3


def test_reimport_keeps_what_the_panel_changed(db):
    import_v1(DATA)
    org = Organization.objects.get(slug="lules")
    rule = Rule.objects.for_org(org).get()
    rule.active, rule.daily_cap, rule.niches = False, 5, ["m:Automotivo"]
    rule.save()
    import_v1(DATA)  # o painel é o dono da regra depois da primeira importação
    rule.refresh_from_db()
    assert rule.active is False and rule.daily_cap == 5 and rule.niches == ["m:Automotivo"]


def test_import_is_idempotent(db):
    import_v1(DATA)
    import_v1(DATA)
    org = Organization.objects.get(slug="lules")
    assert Rule.objects.for_org(org).count() == 1
    assert AffiliateLink.objects.for_org(org).count() == 1
