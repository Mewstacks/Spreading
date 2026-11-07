from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.linkbuilder import runner
from apps.linkbuilder.errors import Blocked, LoginRequired, NoSession, Rejected, Unavailable
from apps.linkbuilder.models import BuilderState, LinkAttempt
from apps.offers.models import AffiliateLink, Offer, Rule
from tests.conftest import NOW
from tests.test_offers import make_offer

PDP = "https://www.mercadolivre.com.br/produto/p/MLB1"
PAIR_URL = "https://www.mercadolivre.com.br/p/MLB333?coupon_campaign_id=13400211"


class FakeDriver:
    def __init__(self, results=None, open_error=None):
        self.results, self.open_error = results or {}, open_error
        self.generated, self.opened, self.closed = [], 0, 0

    def open(self):
        self.opened += 1
        if self.open_error:
            raise self.open_error

    def generate(self, url):
        self.generated.append(url)
        value = self.results.get(url, f"https://meli.la/G{len(self.generated)}")
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed += 1


class Factory:
    """Entrega sempre o mesmo driver e conta as aberturas."""

    def __init__(self, driver):
        self.driver, self.calls = driver, 0

    def __call__(self):
        self.calls += 1
        return self.driver


class Http:
    def __init__(self, final=PDP, status=200):
        self.final, self.status, self.urls = final, status, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return SimpleNamespace(url=self.final, status_code=self.status,
                               text="x" * 30000 + "andes-card")


@pytest.fixture(autouse=True)
def lb_settings(settings):
    settings.LINKBUILDER_ENABLED = True
    settings.LINKBUILDER_MAX_PER_RUN = 8
    settings.LINKBUILDER_MAX_PER_DAY = 120
    settings.LINKBUILDER_PACE_SECONDS = 0
    settings.LINKBUILDER_VERIFY = True


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Grupo",
                                window_start=0, window_end=0, niches=[])


@pytest.fixture
def three(rule):
    make_offer(key="MLB111", price=100, frm=200)  # 50%
    make_offer(key="MLB222", price=140, frm=200)  # 30%
    make_offer(key="MLB333", price=50, frm=100, link_key="MLB333+CODE", coupon_kind="code",
               coupon_code="CODE", coupon_until=NOW + timedelta(days=5), source_url=PAIR_URL)


def go(org, driver, http=None, now=NOW):
    factory = Factory(driver)
    report = runner.run(org, now=now, driver_factory=factory, http=http or Http(),
                        sleep=lambda s: None)
    return report, factory


def state(org):
    return BuilderState.objects.for_org(org).get()


def keys(org):
    return set(AffiliateLink.objects.for_org(org).values_list("product_key", flat=True))


def test_generates_missing_links_pairs_first_then_best_discount(org, three):
    driver = FakeDriver()
    report, _ = go(org, driver)
    assert driver.generated == [
        "https://www.mercadolivre.com.br/p/MLB333?coupon_campaign_id=13400211",  # par primeiro
        "https://www.mercadolivre.com.br/p/MLB111",
        "https://www.mercadolivre.com.br/p/MLB222",
    ]
    assert keys(org) == {"MLB333+CODE", "MLB111", "MLB222"}
    link = AffiliateLink.objects.for_org(org).get(product_key="MLB111")
    assert link.affiliate_url == "https://meli.la/G2" and link.etiqueta == "linkbuilder"
    assert report["geradas"] == 3 and report["estado"] == "pronto"
    assert state(org).state == BuilderState.READY
    assert driver.opened == 1 and driver.closed == 1
    assert LinkAttempt.objects.for_org(org).filter(status=LinkAttempt.OK).count() == 3


def test_per_run_cap_leaves_the_rest_for_the_next_run(org, three, settings):
    settings.LINKBUILDER_MAX_PER_RUN = 2
    driver = FakeDriver()
    go(org, driver)
    assert keys(org) == {"MLB333+CODE", "MLB111"}
    go(org, driver)
    assert keys(org) == {"MLB333+CODE", "MLB111", "MLB222"}


def test_daily_cap_counts_links_generated_today(org, three, settings):
    settings.LINKBUILDER_MAX_PER_DAY = 1
    driver = FakeDriver()
    report, _ = go(org, driver)
    assert len(driver.generated) == 1 and report["geradas"] == 1
    report, factory = go(org, driver)
    assert report["estado"] == "teto_diario" and factory.calls == 0  # nem abre o navegador
    report, _ = go(org, driver, now=NOW + timedelta(days=1))  # outro dia, outro teto
    assert report["geradas"] == 1


def test_disabled_switch_touches_nothing(org, three, settings):
    settings.LINKBUILDER_ENABLED = False
    report, factory = go(org, FakeDriver())
    assert report["estado"] == "desligado" and factory.calls == 0
    assert state(org).state == BuilderState.OFF and not keys(org)


def test_nothing_to_do_does_not_open_the_browser(org, rule):
    report, factory = go(org, FakeDriver())
    assert report["geradas"] == 0 and factory.calls == 0


def test_paused_rules_create_no_demand(org, three, rule):
    rule.active = False
    rule.save()
    _, factory = go(org, FakeDriver())
    assert factory.calls == 0 and not keys(org)


def test_offers_that_already_have_a_link_are_left_alone(org, three):
    AffiliateLink.unscoped.create(org=org, product_key="MLB111", affiliate_url="https://meli.la/x")
    driver = FakeDriver()
    go(org, driver)
    assert "https://www.mercadolivre.com.br/p/MLB111" not in driver.generated
    assert AffiliateLink.objects.for_org(org).get(product_key="MLB111").affiliate_url == (
        "https://meli.la/x")


def test_refused_offer_waits_two_weeks_before_another_try(org, three):
    bait = "https://www.mercadolivre.com.br/p/MLB111"
    refusal = Rejected("⚠️ Este produto não cumpre com os requisitos do Programa.")
    driver = FakeDriver({bait: refusal})
    go(org, driver)
    attempt = LinkAttempt.objects.for_org(org).get(link_key="MLB111")
    assert attempt.status == LinkAttempt.REFUSED and "requisitos" in attempt.reason
    assert attempt.next_try_at == NOW + timedelta(days=14)
    driver.generated.clear()
    go(org, driver, now=NOW + timedelta(days=1))
    assert bait not in driver.generated  # recusado não volta ao topo da fila todo ciclo
    later = NOW + timedelta(days=15)
    Offer.objects.update(last_seen=later)  # a oferta continua sendo vista no catálogo
    go(org, driver, now=later)
    assert bait in driver.generated


def test_storefront_destination_is_refused_and_not_stored(org, three):
    http = Http(final="https://www.mercadolivre.com.br/social/loja/lists")
    go(org, FakeDriver(), http=http)
    assert not keys(org)
    attempt = LinkAttempt.objects.for_org(org).get(link_key="MLB111")
    assert attempt.status == LinkAttempt.REFUSED and attempt.reason == "destino_vitrine"


def test_verification_can_be_switched_off(org, three, settings):
    settings.LINKBUILDER_VERIFY = False
    http = Http(final="https://www.mercadolivre.com.br/social/loja/lists")
    go(org, FakeDriver(), http=http)
    assert http.urls == [] and len(keys(org)) == 3


@pytest.mark.parametrize("bad", ["https://evil.io/x", "http://meli.la/x",
                                 "https://www.mercadolivre.com.br/social/loja?matt_word=x"])
def test_result_that_is_not_a_safe_affiliate_link_is_never_stored(org, three, bad):
    driver = FakeDriver({"https://www.mercadolivre.com.br/p/MLB111": bad})
    go(org, driver)
    assert "MLB111" not in keys(org)
    attempt = LinkAttempt.objects.for_org(org).get(link_key="MLB111")
    assert attempt.status == LinkAttempt.FAILED and attempt.reason == "resultado_invalido"


def test_no_session_is_reported_and_retried_soon(org, three):
    driver = FakeDriver(open_error=NoSession("sem sessão"))
    report, factory = go(org, driver)
    assert report["estado"] == "sem_sessao" and not keys(org)
    assert state(org).paused_until == NOW + timedelta(minutes=5)
    assert factory.calls == 1
    _, paused = go(org, driver, now=NOW + timedelta(minutes=1))
    assert paused.calls == 0  # ainda pausado
    _, again = go(org, driver, now=NOW + timedelta(minutes=6))
    assert again.calls == 1


def test_expired_login_pauses_and_does_not_touch_the_attempts(org, three):
    report, _ = go(org, FakeDriver(open_error=LoginRequired("pediu login")))
    assert report["estado"] == "login" and state(org).detail == "pediu login"
    assert state(org).paused_until == NOW + timedelta(minutes=30)
    assert not LinkAttempt.objects.for_org(org).exists()


def test_block_during_the_run_keeps_what_was_done_and_pauses_two_hours(org, three):
    driver = FakeDriver({"https://www.mercadolivre.com.br/p/MLB111": Blocked("verificação")})
    report, _ = go(org, driver)
    assert keys(org) == {"MLB333+CODE"}  # o par saiu antes do bloqueio
    assert report["estado"] == "verificacao"
    assert state(org).paused_until == NOW + timedelta(hours=2)
    assert not LinkAttempt.objects.for_org(org).filter(link_key="MLB111").exists()


def test_login_lost_mid_run_stops_the_batch(org, three):
    driver = FakeDriver({"https://www.mercadolivre.com.br/p/MLB111": LoginRequired("caiu")})
    report, _ = go(org, driver)
    assert report["estado"] == "login" and keys(org) == {"MLB333+CODE"}
    assert driver.closed == 1


def test_three_unavailable_in_a_row_pauses_with_backoff(org, three, settings):
    bad = Unavailable("o Link Builder não respondeu")
    driver = FakeDriver({
        "https://www.mercadolivre.com.br/p/MLB333?coupon_campaign_id=13400211": bad,
        "https://www.mercadolivre.com.br/p/MLB111": bad,
        "https://www.mercadolivre.com.br/p/MLB222": bad,
    })
    report, _ = go(org, driver)
    assert report["estado"] == "indisponivel" and report["falhas"] == 3 and not keys(org)
    assert state(org).paused_until == NOW + timedelta(hours=1)
    attempt = LinkAttempt.objects.for_org(org).get(link_key="MLB111")
    assert attempt.status == LinkAttempt.FAILED and attempt.attempts == 1
    assert attempt.next_try_at == NOW + timedelta(hours=1)  # recuo cresce com as tentativas


def test_an_isolated_failure_does_not_stop_the_batch(org, three):
    driver = FakeDriver({"https://www.mercadolivre.com.br/p/MLB111": Unavailable("lento")})
    report, _ = go(org, driver)
    assert keys(org) == {"MLB333+CODE", "MLB222"} and report["falhas"] == 1
    assert report["estado"] == "pronto"


def test_two_inconclusive_verifications_in_a_row_pause_two_hours(org, three):
    http = Http(final="https://www.mercadolivre.com.br/gz/account-verification")
    report, _ = go(org, FakeDriver(), http=http)
    assert report["estado"] == "verificacao" and not keys(org)
    assert len(http.urls) == 2  # parou na segunda; a terceira nem foi gerada
    assert state(org).paused_until == NOW + timedelta(hours=2)


def test_command_runs_every_org_and_survives_a_bad_one(org, three, settings, capsys, monkeypatch):
    from django.core.management import call_command

    from apps.orgs.models import Organization

    Organization.objects.create(name="Outra", slug="outra")
    settings.LINKBUILDER_ENABLED = False
    call_command("build_links")
    assert "lules: " in capsys.readouterr().out
    out = capsys.readouterr()
    call_command("build_links", org="outra")
    assert "outra: " in capsys.readouterr().out and out.err == ""

    def boom(org, **kwargs):
        raise RuntimeError("quebrou")

    monkeypatch.setattr("apps.linkbuilder.runner.run", boom)
    call_command("build_links")
    err = capsys.readouterr().err
    assert "lules: falhou: quebrou" in err and "outra: falhou: quebrou" in err


def test_driver_is_always_closed(org, three):
    driver = FakeDriver({"https://www.mercadolivre.com.br/p/MLB333?coupon_campaign_id=13400211":
                         RuntimeError("bug")})
    with pytest.raises(RuntimeError):
        go(org, driver)
    assert driver.closed == 1


# ── plano B: colar links gerados à mão ────────────────────────────────────
def test_manual_pending_lists_the_urls_the_builder_would_ask_for(org, three):
    from apps.linkbuilder import manual

    rows = manual.pending(org, NOW)
    assert [key for _url, key in rows] == ["MLB333+CODE", "MLB111", "MLB222"]
    assert rows[0][0] == PAIR_URL


def test_manual_import_accepts_only_asked_urls_and_safe_links(org, three):
    from apps.linkbuilder import manual

    text = "\n".join([
        f"{PAIR_URL} https://meli.la/PAR",
        "https://www.mercadolivre.com.br/p/MLB111  https://meli.la/UM.",
        "https://www.mercadolivre.com.br/p/MLB222 https://evil.io/x",  # link fora do ML
        "https://www.mercadolivre.com.br/p/MLB222 "
        "https://www.mercadolivre.com.br/social/loja?matt_word=x",  # vitrine
        "https://www.mercadolivre.com.br/p/MLB999 https://meli.la/OUTRO",  # ninguém pediu
        "só texto",
        "",
    ])
    stats = manual.import_pasted(org, text, NOW)
    assert stats == {"importados": 2, "rejeitados": 4}
    links = dict(AffiliateLink.objects.for_org(org).values_list("product_key", "affiliate_url"))
    assert links == {"MLB333+CODE": "https://meli.la/PAR", "MLB111": "https://meli.la/UM"}
    assert AffiliateLink.objects.for_org(org).get(product_key="MLB111").etiqueta == "manual"
