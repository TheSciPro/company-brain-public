"""Connected-app reads: field names come from sources.yaml, the client is injected, and other people's items stay hidden."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from nwb.conf import ConfigLoader, SourcesConfig
from nwb.connectors.ports import SourceConnector
from nwb.connectors.read import _ComposioItems, iter_items, map_payload
from tests.conftest import ROOT


class FakeClient:
    def __init__(self, messages: list[Mapping[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self._messages = list(messages or [MSG])

    def execute(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((action, dict(params)))
        return {"messages": self._messages}


MSG = {
    "messageId": "m1",
    "subject": "Brightmart renewal",
    "messageText": "Discount stays 18%",
    "sender": "aditi.verma@northwind-consulting.com",
    "to": "nikhil.bose@northwind-consulting.com, finance@northwind-consulting.com",
    "messageTimestamp": "2026-09-20T10:00:00+05:30",
}


def _gmail():
    cfg = ConfigLoader(ROOT / "config").load("sources", SourcesConfig)
    return next(item for item in cfg.connectors if item.id == "gmail")


def test_payload_maps_acl_from_the_catalog_field_names() -> None:
    item = map_payload(_gmail(), MSG)
    assert item is not None
    assert item.external_id == "m1" and item.author == "aditi.verma@northwind-consulting.com"
    assert {p.ref for p in item.readers} == {
        "aditi.verma@northwind-consulting.com",
        "nikhil.bose@northwind-consulting.com",
        "finance@northwind-consulting.com",
    }
    assert map_payload(_gmail(), {"subject": "no id"}) is None


def test_listing_uses_the_injected_client_and_the_configured_action() -> None:
    client = FakeClient()
    spec = _gmail()
    conn: SourceConnector = _ComposioItems(spec, client)
    items = list(conn.items(datetime.fromisoformat("2026-09-01T00:00:00+00:00")))
    assert [i.title for i in items] == ["Brightmart renewal"]
    action, params = client.calls[0]
    assert action == spec.list_action == "GMAIL_FETCH_EMAILS"
    assert params["max_results"] == spec.max_results
    assert "after:" in params["query"]
    assert not hasattr(_ComposioItems, "execute")


def test_iter_items_without_since_does_not_invent_a_query() -> None:
    client = FakeClient()
    list(iter_items(_gmail(), client, None))
    assert "query" not in client.calls[0][1]
