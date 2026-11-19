"""Domain-scoped suppressions, per-domain deliverability and the domain pause error.

Each case runs through the sync and the async client (the ``api`` fixture), and every
request and queued answer is held to the published snapshot by the contract mock.
"""

from __future__ import annotations

from typing import Any

import pytest

from billionsend import PermissionDeniedError, ValidationError
from billionsend.types import (
    BatchAddSuppressionsRequest,
    CreateSuppressionRequest,
    DomainDeliverability,
    GetSuppressionResponse,
    ListSuppressionsResponse,
    ListSuppressionsResponseDataItem,
    UpdateDomainRequest,
)

from .api import Api
from .contract import ContractMock, example

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
ID2 = "7b2f2e49-4d66-4b5f-8b68-1a3c4d5e6f70"
CREATED = "2026-11-19T10:00:00.000Z"

EMAIL_ENTRY: GetSuppressionResponse = {
    "id": ID,
    "email": "ada@example.com",
    "kind": "email",
    "domain": None,
    "removable": False,
    "origin": "bounce",
    "source_id": None,
    "created_at": CREATED,
    "object": "suppression",
}
DOMAIN_ENTRY: GetSuppressionResponse = {
    "id": ID2,
    "email": None,
    "kind": "domain",
    "domain": "spam.example.org",
    "removable": True,
    "origin": "manual",
    "source_id": None,
    "created_at": CREATED,
    "object": "suppression",
}
LIST_DOMAIN: ListSuppressionsResponseDataItem = {
    "id": ID2,
    "email": None,
    "kind": "domain",
    "domain": "spam.example.org",
    "removable": True,
    "origin": "manual",
    "source_id": None,
    "created_at": CREATED,
}
LIST_EMAIL: ListSuppressionsResponseDataItem = {
    "id": ID,
    "email": "ada@example.com",
    "kind": "email",
    "domain": None,
    "removable": False,
    "origin": "bounce",
    "source_id": None,
    "created_at": CREATED,
}


def test_create_suppression_by_domain(api: Api, mock: ContractMock) -> None:
    body: CreateSuppressionRequest = {"domain": "spam.example.org", "origin": "manual"}
    mock.respond("POST", "/suppressions", json_body=DOMAIN_ENTRY)
    entry = api.call("suppressions.create", body)
    assert mock.last.body == {"domain": "spam.example.org", "origin": "manual"}
    assert entry["email"] is None
    assert (entry["kind"], entry["domain"], entry["removable"]) == (
        "domain",
        "spam.example.org",
        True,
    )


def test_create_suppression_by_email_still_sends_only_the_email(
    api: Api, mock: ContractMock
) -> None:
    mock.respond("POST", "/suppressions", json_body=EMAIL_ENTRY)
    entry = api.call("suppressions.create", {"email": "ada@example.com"})
    assert mock.last.body == {"email": "ada@example.com"}
    assert entry["removable"] is False


def test_batch_add_takes_domains_alone_or_with_emails(api: Api, mock: ContractMock) -> None:
    only_domains: BatchAddSuppressionsRequest = {"domains": ["a.example.org", "b.example.org"]}
    api.call("suppressions.batch.add", only_domains)
    assert mock.last.body == {"domains": ["a.example.org", "b.example.org"]}
    both: BatchAddSuppressionsRequest = {
        "emails": ["ada@example.com"],
        "domains": ["a.example.org"],
    }
    api.call("suppressions.batch.add", both)
    assert mock.last.body == both


@pytest.mark.parametrize("kind", ["email", "domain", "all"])
def test_list_suppressions_filters_by_kind(api: Api, mock: ContractMock, kind: str) -> None:
    api.call("suppressions.list", kind=kind)
    assert mock.last.query == {"kind": [kind]}


def test_list_without_kind_sends_no_kind(api: Api, mock: ContractMock) -> None:
    api.call("suppressions.list", origin="manual")
    assert mock.last.query == {"origin": ["manual"]}


def test_list_all_keeps_the_kind_on_every_page(api: Api, mock: ContractMock) -> None:
    listing: ListSuppressionsResponse = {"object": "list", "has_more": True, "data": [LIST_DOMAIN]}
    mock.respond("GET", "/suppressions", json_body=listing)
    mock.respond("GET", "/suppressions", json_body={**listing, "has_more": False})
    entries = api.call("suppressions.list_all", kind="domain", limit=1)
    assert [e["domain"] for e in entries] == ["spam.example.org", "spam.example.org"]
    assert [c.query["kind"] for c in mock.calls] == [["domain"], ["domain"]]
    assert mock.calls[1].query["after"] == [ID2]


def test_a_domain_entry_has_no_email_to_assume(api: Api, mock: ContractMock) -> None:
    listing = {"object": "list", "has_more": False, "data": [LIST_EMAIL, LIST_DOMAIN]}
    mock.respond("GET", "/suppressions", json_body=listing)
    entries = api.call("suppressions.list_all", kind="all")
    names = [e["email"] or e["domain"] for e in entries]
    assert names == ["ada@example.com", "spam.example.org"]


def test_get_suppression_returns_a_domain_entry(api: Api, mock: ContractMock) -> None:
    mock.respond("GET", "/suppressions/{id}", json_body=DOMAIN_ENTRY)
    assert api.call("suppressions.get", ID2) == DOMAIN_ENTRY


def test_deliverability_lists_each_domain(api: Api, mock: ContractMock) -> None:
    domain: DomainDeliverability = {
        "domain_id": ID,
        "domain": "acme.dev",
        "status": "paused",
        "emails_sent": 1200,
        "hard_bounce_rate": 0.031,
        "complaint_rate": 0.0004,
        "hard_bounces": 37,
        "complaints": 1,
        "delivered": 1100,
        "window_days": 7,
        "tier": "paused",
        "paused_by": "domain",
        "recent_hard_bounce_rate": 0.05,
        "recent_complaint_rate": 0.0,
        "pause_margin": -0.2,
    }
    team = example({"$ref": "#/components/schemas/DeliverabilityResponse"})
    body: dict[str, Any] = {**team, "domains": [domain]}
    mock.respond("GET", "/deliverability", json_body=body)
    got = api.call("deliverability.get")
    assert got["domains"] == [domain]
    assert got["domains"][0]["paused_by"] == "domain"


@pytest.mark.parametrize("name", ["domain_sending_paused", "sending_paused"])
def test_a_paused_sender_is_a_permission_error(api: Api, mock: ContractMock, name: str) -> None:
    api.client.max_retries = 0
    mock.error("POST", "/emails", 403, name, "sending is paused")
    with pytest.raises(PermissionDeniedError) as info:
        api.call(
            "emails.send",
            {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"},
        )
    assert (info.value.status_code, info.value.name) == (403, name)


def test_the_return_path_is_sent_on_create_and_refused_on_update(
    api: Api, mock: ContractMock
) -> None:
    api.client.max_retries = 0
    api.call("domains.create", {"name": "acme.dev"})
    assert mock.last.body == {"name": "acme.dev"}  # no custom_return_path: the API's default
    api.call("domains.create", {"name": "acme.dev", "custom_return_path": "mail"})
    assert mock.last.body == {"name": "acme.dev", "custom_return_path": "mail"}
    change: UpdateDomainRequest = {"custom_return_path": "other"}
    mock.error("PATCH", "/domains/{id}", 422, "validation_error", "custom_return_path is fixed")
    with pytest.raises(ValidationError):
        api.call("domains.update", ID, change)
    assert mock.last.body == {"custom_return_path": "other"}
