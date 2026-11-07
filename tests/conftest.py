from datetime import datetime, timezone

import pytest

from apps.delivery.transports import CONFIRMED, FAILED, UNCERTAIN, Result
from apps.orgs.models import ContainerLink, Destination, Organization

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)  # 12:00 em Brasília


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    status = "WORKING"
    acks = {}

    def session_status(self):
        return self.status

    def session_working(self):
        return self.status == "WORKING"

    def message_ack(self, chat_id, message_id):
        return self.acks.get(message_id)

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.results:
            return self.results.pop(0)
        return Result(True, CONFIRMED, message_id=f"true_{chat_id}_{len(self.sent)}")


def ok(mid="m1"):
    return Result(True, CONFIRMED, message_id=mid)


def transient():
    return Result(False, FAILED, klass="transitorio", retry=True, error="http 503")


def permanent():
    return Result(False, FAILED, klass="permanente", error="http 400")


def uncertain():
    return Result(False, UNCERTAIN, klass="desconhecido", error="timeout")


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Lules", slug="lules")


@pytest.fixture
def destination(org):
    return Destination.unscoped.create(
        org=org, kind=Destination.WA_GROUP, external_id="120363@g.us", name="Teste ofertas"
    )


@pytest.fixture
def container_link(org):
    return ContainerLink.unscoped.create(
        org=org,
        container_url="https://lista.mercadolivre.com.br/_Container_aff-list-28",
        affiliate_url="https://meli.la/abc123",
    )
