import inspect

import pytest
import requests
from django.apps import apps

from apps.delivery.transports import WahaTransport
from apps.orgs.models import OrgScopedModel, TenantScopeError


def test_every_org_scoped_model_denies_unscoped_queries(db):
    models = [m for m in apps.get_models() if issubclass(m, OrgScopedModel)]
    assert models, "nenhum modelo por organização encontrado"
    for model in models:
        with pytest.raises(TenantScopeError):
            list(model.objects.all())
        with pytest.raises(TenantScopeError):
            model.objects.filter(pk=1).exists()


def test_for_org_scopes_results(org, destination):
    from apps.orgs.models import Destination, Organization

    other = Organization.objects.create(name="Outra", slug="outra")
    Destination.unscoped.create(org=other, kind="whatsapp_group", external_id="x@g.us", name="x")
    assert Destination.objects.for_org(org).count() == 1
    assert Destination.objects.for_org(other).count() == 1


class FakeHttp:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.response


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def waha(http):
    return WahaTransport("http://waha:3000/", "key", "default", http=http)


def test_waha_success_returns_message_id_and_sends_expected_payload():
    http = FakeHttp(Resp(201, {"id": "true_120363@g.us_ABC"}))
    result = waha(http).send_text("120363@g.us", "oi")
    assert result.success and result.message_id == "true_120363@g.us_ABC"
    url, kwargs = http.calls[0]
    assert url == "http://waha:3000/api/sendText"
    assert kwargs["json"] == {"session": "default", "chatId": "120363@g.us", "text": "oi"}
    assert kwargs["headers"] == {"X-Api-Key": "key"}


def test_waha_5xx_is_transient_4xx_is_permanent():
    transient = waha(FakeHttp(Resp(503))).send_text("c", "t")
    assert not transient.success and transient.retry and transient.klass == "transitorio"
    permanent = waha(FakeHttp(Resp(400))).send_text("c", "t")
    assert not permanent.success and not permanent.retry and permanent.klass == "permanente"


def test_waha_timeout_is_uncertain_no_retry():
    result = waha(FakeHttp(exc=requests.exceptions.Timeout())).send_text("c", "t")
    assert result.outcome == "incerto" and not result.retry


def test_waha_connection_error_is_transient():
    result = waha(FakeHttp(exc=requests.exceptions.ConnectionError("down"))).send_text("c", "t")
    assert result.retry and result.outcome == "falhou"


def test_transport_signature_stable():
    params = list(inspect.signature(WahaTransport.send_text).parameters)
    assert params == ["self", "chat_id", "text"]
