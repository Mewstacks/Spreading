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
         "https://meli.la/p1"},
        {"url": "https://produto.mercadolivre.com.br/MLB-111111111", "affiliate_url":
         "https://meli.la/dup"},
        {"url": "https://produto.mercadolivre.com.br/MLB-222222222", "affiliate_url":
         "http://insegura.example/p2"},
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


def test_import_is_idempotent(db):
    import_v1(DATA)
    import_v1(DATA)
    org = Organization.objects.get(slug="lules")
    assert Rule.objects.for_org(org).count() == 1
    assert AffiliateLink.objects.for_org(org).count() == 1
