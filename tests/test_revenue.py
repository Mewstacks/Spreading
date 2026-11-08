from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.offers import shopee
from apps.offers.models import Conversion, SourceRun
from tests.conftest import NOW

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def creds(settings):
    settings.SHOPEE_APP_ID, settings.SHOPEE_APP_SECRET = "app123", "secret456"


@pytest.fixture
def staff(client, monkeypatch, django_user_model):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    client.force_login(django_user_model.objects.create_user("dono", password="x", is_staff=True))


def sale(org, ident, days_ago, status="COMPLETED", commission="10.00", amount="100.00",
         channel="WhatsApp", title="Suporte de mesa", items=1, sub="----"):
    return Conversion.unscoped.create(
        org=org, store="shopee", external_id=ident, purchased_at=NOW - timedelta(days=days_ago),
        status=status, title=title, items=items, amount=Decimal(amount),
        commission=Decimal(commission), channel=channel, sub_id=sub, synced_at=NOW)


def html(client, **query):
    return client.get(reverse("panel:revenue"), query).content.decode()


def test_revenue_requires_staff(client, org):
    assert client.get(reverse("panel:revenue")).status_code == 302


def test_totals_by_status_inside_the_period(client, staff, org):
    sale(org, "a", 2, "COMPLETED", "10.5000", "100.00", items=2)
    sale(org, "b", 1, "PENDING", "3.2500", "50.00")
    sale(org, "c", 3, "CANCELLED", "0", "40.00")
    sale(org, "d", 40, "COMPLETED", "100.0000", "900.00")  # fora dos 30 dias
    page = html(client)
    assert "R$ 10,50" in page and "R$ 3,25" in page  # confirmada e pendente
    assert "R$ 150,00" in page and "3 itens" in page  # vendido: a cancelada não entra
    assert "R$ 100,00" not in page and "R$ 110,50" not in page
    wide = html(client, dias="60")
    assert "R$ 110,50" in wide and "R$ 100,00" in wide  # 10,50 + 100,00 confirmadas


def test_the_latest_sales_show_state_origin_and_extra_items(client, staff, org):
    sale(org, "a", 1, "COMPLETED", "7.0000", title="Lixeira Seletiva", items=3, channel="YouTube")
    sale(org, "b", 2, "PENDING", "1.0000", title="Kit Ducha")
    sale(org, "c", 3, "CANCELLED", "0", title="Fone")
    sale(org, "d", 4, "REFUNDED", "0", title="Capa")  # estado que a loja inventar depois
    page = html(client)
    assert "Lixeira Seletiva" in page and "+2" in page and "YouTube" in page
    assert 'badge-green">Confirmada<' in page and 'badge-yellow">Pendente<' in page
    assert 'badge-red">Cancelada<' in page and 'badge-muted">Refunded<' in page
    assert page.index("Lixeira Seletiva") < page.index("Kit Ducha") < page.index("Fone")
    assert "R$ 7,00" in page  # comissão da venda


def test_the_origin_table_sums_the_commission_without_cancelled_sales(client, staff, org):
    sale(org, "a", 1, "COMPLETED", "10.0000", channel="WhatsApp")
    sale(org, "b", 1, "PENDING", "5.0000", channel="WhatsApp")
    sale(org, "c", 1, "COMPLETED", "2.0000", channel="YouTube")
    sale(org, "d", 1, "CANCELLED", "0", channel="Websites")
    origin = html(client).split('<ul class="rows">', 1)[1].split("</ul>")[0]  # lista de origens
    assert origin.index("WhatsApp") < origin.index("YouTube")  # maior comissão primeiro
    assert "R$ 15,00" in origin and "R$ 2,00" in origin and "Websites" not in origin


def test_an_empty_period_offers_the_wider_one(client, staff, org):
    sale(org, "old", 45)
    page = html(client)
    assert "Sem vendas em 30 dias." in page and 'href="?dias=60"' in page
    assert "Sem vendas" not in html(client, dias="60")


def test_the_widest_period_has_no_wider_door(client, staff, org):
    page = html(client, dias="60")
    assert "Sem vendas em 60 dias." in page and "Ver 60 dias" not in page


def test_an_unknown_period_falls_back_to_thirty_days(client, staff, org):
    sale(org, "a", 20)
    assert 'value="30" selected' in html(client, dias="9999")
    assert 'value="30" selected' in html(client, dias="abc")
    week = html(client, dias="7")
    assert 'value="7" selected' in week and "Sem vendas em 7 dias." in week


def test_without_credentials_the_page_says_so_and_points_to_sources(client, staff, org, settings):
    settings.SHOPEE_APP_SECRET = ""
    assert not shopee.configured()
    page = html(client)
    assert "Shopee desligada." in page and reverse("panel:sources") in page
    assert "Confirmada" not in page


def test_the_sources_page_lists_the_sales_source(client, staff, org):
    SourceRun.objects.create(label="shopee_vendas", checked_at=NOW, result="vendas 199",
                             result_at=NOW)
    page = client.get(reverse("panel:sources")).content.decode()
    assert "Shopee · vendas" in page and "vendas 199" in page


def test_the_menu_links_to_the_revenue_page(client, staff, org):
    assert reverse("panel:revenue") in client.get(reverse("panel:sources")).content.decode()


def test_sales_that_carry_the_spreading_sub_id_are_split_by_group(client, staff, org, destination):
    mine = f"spreading-g{destination.id}---"
    sale(org, "a", 1, "COMPLETED", "10.0000", sub=mine)
    sale(org, "b", 1, "PENDING", "5.0000", sub=mine)
    sale(org, "c", 1, "CANCELLED", "0", sub=mine)  # cancelada não soma
    sale(org, "d", 1, "COMPLETED", "3.0000", sub="spreading-g999---")  # grupo que não existe mais
    sale(org, "e", 1, "COMPLETED", "99.0000", sub="----")  # clique que não passou pelo Spreading
    sale(org, "f", 1, "COMPLETED", "98.0000", sub="spreading-gx---")  # sub-id fora do padrão
    sale(org, "g", 1, "COMPLETED", "97.0000", sub=f"spreadingg{destination.id}")
    page = html(client)
    groups = page.split('id="grupos"', 1)[1].split("</ul>")[0]
    assert "Teste ofertas" in groups and "R$ 15,00" in groups and "2 vendas" in groups
    assert "Grupo 999" in groups and "R$ 3,00" in groups
    assert groups.index("Teste ofertas") < groups.index("Grupo 999")  # maior comissão primeiro
    assert "R$ 99,00" not in groups and "R$ 98,00" not in groups and "R$ 97,00" not in groups


def test_without_attributed_sales_there_is_no_group_list(client, staff, org):
    sale(org, "a", 1, sub="----")
    assert 'id="grupos"' not in html(client)
