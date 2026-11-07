from types import SimpleNamespace

import pytest
import requests

from apps.linkbuilder.bait import bait_url
from apps.linkbuilder.errors import Rejected, Unavailable
from apps.linkbuilder.pw import classify_result, is_interstitial, is_login
from apps.linkbuilder.verify import verify_landing
from apps.offers.models import Offer

PDP = "https://www.mercadolivre.com.br/robo-aspirador/p/MLB62695902"


def offer(**over):
    base = dict(store="mercadolivre", url=PDP, source_url=PDP, coupon_kind="")
    base.update(over)
    return Offer(**base)


# ── URL-isca ──────────────────────────────────────────────────────────────
def test_bait_is_the_clean_product_page():
    dirty = PDP + "?pdp_filters=item_id%3AMLB1&tracking_id=abc#polycard_client=x"
    assert bait_url(offer(url=dirty)) == PDP
    assert bait_url(offer(url=PDP.replace("https", "http"))) == PDP  # sempre https
    item = "https://produto.mercadolivre.com.br/MLB-3456789012-monitor-lg-_JM?x=1"
    assert bait_url(offer(url=item)) == "https://produto.mercadolivre.com.br/MLB-3456789012-monitor-lg-_JM"


def test_bait_carries_the_coupon_campaign_only_when_valid():
    with_campaign = PDP + "?coupon_campaign_id=13400211"
    assert bait_url(offer(coupon_kind="activation", source_url=with_campaign)) == (
        PDP + "?coupon_campaign_id=13400211")
    assert bait_url(offer(coupon_kind="code", source_url=with_campaign)) == (
        PDP + "?coupon_campaign_id=13400211")
    assert bait_url(offer(coupon_kind="code", source_url=PDP)) == PDP  # código não precisa
    # ativação sem campanha legível não ativa nada: sem isca, sem link
    assert bait_url(offer(coupon_kind="activation", source_url=PDP)) == ""
    assert bait_url(offer(coupon_kind="activation",
                          source_url=PDP + "?coupon_campaign_id=abc%26x")) == ""


@pytest.mark.parametrize("bad", [
    "https://www.mercadolivre.com.br/social/loja/lists",
    "https://www.mercadolivre.com.br/perfil/loja",
    "https://www.mercadolivre.com.br/up/MLBU123456",
    "https://click1.mercadolivre.com.br/mclics/clicks/external/MLB.jsp?x=1",
    "https://www.mercadolivre.com.br/mclics/x",
    "https://evil.io/p/MLB62695902",
    "https://www.mercadolivre.com.br.evil.io/p/MLB62695902",
    "https://user:pass@www.mercadolivre.com.br/p/MLB62695902",
    "https://www.mercadolivre.com.br:8443/p/MLB62695902",
    "javascript:alert(1)",
    "ftp://www.mercadolivre.com.br/p/MLB62695902",
    "",
])
def test_bait_refuses_what_the_program_does_not_accept(bad):
    assert bait_url(offer(url=bad)) == ""


def test_bait_is_for_mercado_livre_only():
    assert bait_url(offer(store="amazon", url="https://www.amazon.com.br/dp/B012345678")) == ""


# ── destino do link gerado ────────────────────────────────────────────────
class Http:
    def __init__(self, final=PDP, status=200, body="x" * 30000 + " andes-card", error=None):
        self.args = SimpleNamespace(final=final, status=status, body=body)
        self.error, self.calls = error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(url=self.args.final, status_code=self.args.status,
                               text=self.args.body)


def test_verify_accepts_the_product_page_and_follows_redirects_anonymously():
    http = Http()
    assert verify_landing("https://meli.la/abc", http=http) == ("ok", PDP)
    url, kwargs = http.calls[0]
    assert url == "https://meli.la/abc" and kwargs["allow_redirects"] is True
    assert "cookies" not in kwargs  # anônimo: não usa a sessão do afiliado


def test_verify_refuses_the_storefront_and_unexpected_pages():
    social = "https://www.mercadolivre.com.br/social/loja/lists"
    status, detail = verify_landing("https://meli.la/abc", http=Http(final=social))
    assert status == "recusado" and "vitrine" in detail
    status, detail = verify_landing("https://meli.la/abc",
                                    http=Http(final="https://www.mercadolivre.com.br/"))
    assert status == "recusado" and "inesperado" in detail
    status, detail = verify_landing("https://meli.la/abc", http=Http(status=404))
    assert status == "recusado" and "indisponivel" in detail


@pytest.mark.parametrize("http", [
    Http(final="https://www.mercadolivre.com.br/gz/account-verification?x=1"),
    Http(final="https://www.mercadolivre.com.br/jms/mlb/lgz/login"),
    Http(status=403), Http(status=429), Http(status=503),
    Http(body="<html>challenge</html>"),  # PDP de verdade tem centenas de KB e classes andes-
    Http(error=requests.exceptions.ConnectTimeout()),
])
def test_verify_never_condemns_a_link_on_a_block(http):
    assert verify_landing("https://meli.la/abc", http=http)[0] == "inconclusivo"


# ── leitura do portal ─────────────────────────────────────────────────────
def test_classify_result_returns_the_link_or_says_why_not():
    assert classify_result("  https://meli.la/2Ehvccw \n") == "https://meli.la/2Ehvccw"
    for refusal in ["⚠️ Este URL não é permitido pelo Programa.",
                    "⚠️ Este produto não cumpre com os requisitos do Programa.",
                    "URL inválida", "Este URL não é do Mercado Livre Brasil"]:
        with pytest.raises(Rejected):
            classify_result(refusal)
    for empty in ["", "   ", "erro qualquer sem link"]:
        with pytest.raises(Unavailable):
            classify_result(empty)


def test_page_state_heuristics():
    assert is_interstitial("https://www.mercadolivre.com.br/gz/account-verification?go=x")
    assert is_interstitial("https://www.mercadolivre.com.br/captcha")
    assert not is_interstitial("https://www.mercadolivre.com.br/afiliados/linkbuilder")
    assert is_login("https://www.mercadolivre.com.br/jms/mlb/lgz/login?go=x")
    assert is_login("https://www.mercadolivre.com.br/registration")
    assert not is_login("https://www.mercadolivre.com.br/afiliados/linkbuilder")
