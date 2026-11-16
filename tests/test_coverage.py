"""Every operation of the snapshot is reachable through the SDK, and nothing else is.

``CALLS`` maps each (method, path) of the contract to the SDK call that serves
it. The test runs every call through the contract mock, with the sync and the
async client, and compares the operations hit with the snapshot: an operation
the SDK does not reach, or a request outside the contract, fails.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from typing import Any

from billionsend import AsyncBillionSend, BillionSend

from .contract import OPERATION_KEYS, OPERATIONS, SNAPSHOT, ContractMock, example

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
ID2 = "7b2e2e49-4d66-4b5f-8b68-1a3c4d5e6f70"
INBOX = "support@example.com"

Call = Callable[[Any, Any], Any]

CALLS: dict[tuple[str, str], Call] = {
    # Emails
    ("POST", "/emails"): lambda c, b: c.emails.send(b, idempotency_key="key-1"),
    ("POST", "/emails/batch"): lambda c, b: c.emails.batch(b, idempotency_key="key-2"),
    ("GET", "/emails"): lambda c, b: c.emails.list(limit=10),
    ("GET", "/emails/{id}"): lambda c, b: c.emails.get(ID),
    ("PATCH", "/emails/{id}"): lambda c, b: c.emails.update(ID, b),
    ("DELETE", "/emails/{id}"): lambda c, b: c.emails.delete(ID),
    ("POST", "/emails/{id}/cancel"): lambda c, b: c.emails.cancel(ID),
    ("GET", "/emails/{id}/insights"): lambda c, b: c.emails.insights(ID),
    ("GET", "/emails/metrics"): lambda c, b: c.emails.metrics(metrics=["sent", "delivered"]),
    ("GET", "/emails/receiving"): lambda c, b: c.emails.receiving.list(unread=True),
    ("GET", "/emails/receiving/{id}"): lambda c, b: c.emails.receiving.get(ID),
    ("DELETE", "/emails/receiving/{id}"): lambda c, b: c.emails.receiving.delete(ID),
    ("POST", "/emails/receiving/{id}/reply"): lambda c, b: c.emails.receiving.reply(ID, b),
    ("POST", "/emails/receiving/{id}/forward"): lambda c, b: c.emails.receiving.forward(ID, b),
    ("GET", "/emails/receiving/{email_id}/attachments"): lambda c, b: (
        c.emails.receiving.attachments.list(ID)
    ),
    ("GET", "/emails/receiving/{email_id}/attachments/{id}"): lambda c, b: (
        c.emails.receiving.attachments.get(ID, ID2)
    ),
    ("GET", "/emails/receiving/{email_id}/attachments/{id}/text"): lambda c, b: (
        c.emails.receiving.attachments.text(ID, ID2)
    ),
    # Domains
    ("POST", "/domains"): lambda c, b: c.domains.create(b),
    ("GET", "/domains"): lambda c, b: c.domains.list(),
    ("GET", "/domains/{id}"): lambda c, b: c.domains.get(ID),
    ("PATCH", "/domains/{id}"): lambda c, b: c.domains.update(ID, b),
    ("DELETE", "/domains/{id}"): lambda c, b: c.domains.delete(ID),
    ("POST", "/domains/{id}/verify"): lambda c, b: c.domains.verify(ID),
    # API keys
    ("POST", "/api-keys"): lambda c, b: c.api_keys.create(b),
    ("GET", "/api-keys"): lambda c, b: c.api_keys.list(),
    ("DELETE", "/api-keys/{id}"): lambda c, b: c.api_keys.delete(ID),
    # Contacts
    ("POST", "/contacts"): lambda c, b: c.contacts.create(b),
    ("GET", "/contacts"): lambda c, b: c.contacts.list(),
    ("POST", "/contacts/batch"): lambda c, b: c.contacts.batch.create(
        b, on_conflict="upsert", batch_validation="permissive"
    ),
    ("POST", "/contacts/batch/get"): lambda c, b: c.contacts.batch.get(b),
    ("POST", "/contacts/batch/remove"): lambda c, b: c.contacts.batch.remove(b),
    ("GET", "/contacts/{id}"): lambda c, b: c.contacts.get(ID),
    ("PATCH", "/contacts/{id}"): lambda c, b: c.contacts.update(ID, b),
    ("DELETE", "/contacts/{id}"): lambda c, b: c.contacts.delete(ID, erase=True),
    ("POST", "/contacts/{id}/preferences-link"): lambda c, b: c.contacts.preferences_link(ID),
    ("GET", "/contacts/{id}/topics"): lambda c, b: c.contacts.topics.list(ID),
    ("PATCH", "/contacts/{id}/topics"): lambda c, b: c.contacts.topics.update(ID, b),
    ("POST", "/contacts/{id}/segments/{segmentId}"): lambda c, b: c.contacts.segments.add(ID, ID2),
    ("DELETE", "/contacts/{id}/segments/{segmentId}"): lambda c, b: c.contacts.segments.remove(
        ID, ID2
    ),
    # Audiences
    ("POST", "/audiences/{audienceId}/contacts"): lambda c, b: c.audiences.contacts.create(ID, b),
    ("GET", "/audiences/{audienceId}/contacts/{id}"): lambda c, b: c.audiences.contacts.get(
        ID, ID2
    ),
    ("PATCH", "/audiences/{audienceId}/contacts/{id}"): lambda c, b: c.audiences.contacts.update(
        ID, ID2, b
    ),
    ("DELETE", "/audiences/{audienceId}/contacts/{id}"): lambda c, b: c.audiences.contacts.delete(
        ID, ID2, erase="false"
    ),
    # Contact properties
    ("POST", "/contact-properties"): lambda c, b: c.contact_properties.create(b),
    ("GET", "/contact-properties"): lambda c, b: c.contact_properties.list(),
    ("GET", "/contact-properties/{id}"): lambda c, b: c.contact_properties.get(ID),
    ("PATCH", "/contact-properties/{id}"): lambda c, b: c.contact_properties.update(ID, b),
    ("DELETE", "/contact-properties/{id}"): lambda c, b: c.contact_properties.delete(ID),
    # Segments
    ("POST", "/segments"): lambda c, b: c.segments.create(b),
    ("GET", "/segments"): lambda c, b: c.segments.list(),
    ("GET", "/segments/{id}"): lambda c, b: c.segments.get(ID),
    ("PATCH", "/segments/{id}"): lambda c, b: c.segments.update(ID, b),
    ("DELETE", "/segments/{id}"): lambda c, b: c.segments.delete(ID),
    ("GET", "/segments/{id}/contacts"): lambda c, b: c.segments.contacts.list(ID),
    # Topics
    ("POST", "/topics"): lambda c, b: c.topics.create(b),
    ("GET", "/topics"): lambda c, b: c.topics.list(),
    ("GET", "/topics/{id}"): lambda c, b: c.topics.get(ID),
    ("PATCH", "/topics/{id}"): lambda c, b: c.topics.update(ID, b),
    ("DELETE", "/topics/{id}"): lambda c, b: c.topics.delete(ID),
    # Broadcasts
    ("POST", "/broadcasts"): lambda c, b: c.broadcasts.create(b),
    ("GET", "/broadcasts"): lambda c, b: c.broadcasts.list(),
    ("GET", "/broadcasts/{id}"): lambda c, b: c.broadcasts.get(ID),
    ("PATCH", "/broadcasts/{id}"): lambda c, b: c.broadcasts.update(ID, b),
    ("DELETE", "/broadcasts/{id}"): lambda c, b: c.broadcasts.delete(ID),
    ("POST", "/broadcasts/{id}/send"): lambda c, b: c.broadcasts.send(ID, b),
    ("POST", "/broadcasts/{id}/duplicate"): lambda c, b: c.broadcasts.duplicate(ID),
    ("POST", "/broadcasts/{id}/cancel"): lambda c, b: c.broadcasts.cancel(ID),
    # Templates
    ("POST", "/templates"): lambda c, b: c.templates.create(b),
    ("GET", "/templates"): lambda c, b: c.templates.list(),
    ("GET", "/templates/{id}"): lambda c, b: c.templates.get(ID),
    ("PATCH", "/templates/{id}"): lambda c, b: c.templates.update(ID, b),
    ("DELETE", "/templates/{id}"): lambda c, b: c.templates.delete(ID),
    ("POST", "/templates/{id}/publish"): lambda c, b: c.templates.publish(ID),
    ("POST", "/templates/{id}/duplicate"): lambda c, b: c.templates.duplicate(ID),
    # Webhooks
    ("POST", "/webhooks"): lambda c, b: c.webhooks.create(b),
    ("GET", "/webhooks"): lambda c, b: c.webhooks.list(),
    ("GET", "/webhooks/{id}"): lambda c, b: c.webhooks.get(ID),
    ("PATCH", "/webhooks/{id}"): lambda c, b: c.webhooks.update(ID, b),
    ("DELETE", "/webhooks/{id}"): lambda c, b: c.webhooks.delete(ID),
    ("POST", "/webhooks/{id}/rotate"): lambda c, b: c.webhooks.rotate(ID, b),
    # Suppressions
    ("POST", "/suppressions"): lambda c, b: c.suppressions.create(b),
    ("GET", "/suppressions"): lambda c, b: c.suppressions.list(origin="bounce"),
    ("GET", "/suppressions/{id}"): lambda c, b: c.suppressions.get(ID),
    ("DELETE", "/suppressions/{id}"): lambda c, b: c.suppressions.delete(ID),
    ("POST", "/suppressions/batch/add"): lambda c, b: c.suppressions.batch.add(b),
    ("POST", "/suppressions/batch/remove"): lambda c, b: c.suppressions.batch.remove(b),
    # Unsubscribes
    ("POST", "/unsubscribes"): lambda c, b: c.unsubscribes.create(b),
    ("GET", "/unsubscribes"): lambda c, b: c.unsubscribes.list(domain_id=ID),
    ("DELETE", "/unsubscribes/{id}"): lambda c, b: c.unsubscribes.delete(ID),
    # Verifications
    ("GET", "/verifications/availability"): lambda c, b: c.verifications.availability(),
    ("POST", "/verifications/estimate"): lambda c, b: c.verifications.estimate(b),
    ("POST", "/verifications"): lambda c, b: c.verifications.create(b),
    ("GET", "/verifications"): lambda c, b: c.verifications.list(),
    ("GET", "/verifications/{id}"): lambda c, b: c.verifications.get(ID),
    # Usage and deliverability
    ("GET", "/usage"): lambda c, b: c.usage.get(),
    ("GET", "/deliverability"): lambda c, b: c.deliverability.get(),
    # Inboxes
    ("POST", "/inboxes"): lambda c, b: c.inboxes.create(b),
    ("GET", "/inboxes"): lambda c, b: c.inboxes.list(kind="agent", limit=5),
    ("GET", "/inboxes/{inbox_id}"): lambda c, b: c.inboxes.get(INBOX),
    ("PATCH", "/inboxes/{inbox_id}"): lambda c, b: c.inboxes.update(INBOX, b),
    ("DELETE", "/inboxes/{inbox_id}"): lambda c, b: c.inboxes.delete(INBOX),
    ("POST", "/inboxes/{inbox_id}/emails"): lambda c, b: c.inboxes.send(INBOX, b),
    ("GET", "/inboxes/events"): lambda c, b: c.inboxes.events.stream(max_reconnects=0),
    ("GET", "/inboxes/{inbox_id}/events"): lambda c, b: c.inboxes.events.stream_inbox(
        INBOX, max_reconnects=0
    ),
    ("GET", "/inboxes/{inbox_id}/agent"): lambda c, b: c.inboxes.agent.get(INBOX),
    ("GET", "/inboxes/{inbox_id}/safety"): lambda c, b: c.inboxes.safety.get(INBOX),
    ("PATCH", "/inboxes/{inbox_id}/safety"): lambda c, b: c.inboxes.safety.update(INBOX, b),
    ("GET", "/inboxes/{inbox_id}/labels"): lambda c, b: c.inboxes.labels.list(INBOX),
    ("POST", "/inboxes/{inbox_id}/labels"): lambda c, b: c.inboxes.labels.create(INBOX, b),
    ("PATCH", "/inboxes/{inbox_id}/labels/{label_id}"): lambda c, b: c.inboxes.labels.update(
        INBOX, ID, b
    ),
    ("DELETE", "/inboxes/{inbox_id}/labels/{label_id}"): lambda c, b: c.inboxes.labels.delete(
        INBOX, ID
    ),
    ("GET", "/inboxes/{inbox_id}/threads"): lambda c, b: c.inboxes.threads.list(INBOX),
    ("GET", "/inboxes/{inbox_id}/threads/search"): lambda c, b: c.inboxes.threads.search(
        INBOX, query="invoice", from_="billing@example.com"
    ),
    ("GET", "/inboxes/{inbox_id}/threads/{thread_id}"): lambda c, b: c.inboxes.threads.get(
        INBOX, ID
    ),
    ("PATCH", "/inboxes/{inbox_id}/threads/{thread_id}"): lambda c, b: c.inboxes.threads.update(
        INBOX, ID, b
    ),
    ("DELETE", "/inboxes/{inbox_id}/threads/{thread_id}"): lambda c, b: c.inboxes.threads.delete(
        INBOX, ID
    ),
    ("POST", "/inboxes/{inbox_id}/threads/{thread_id}/labels"): lambda c, b: (
        c.inboxes.threads.labels.add(INBOX, ID, b)
    ),
    ("PUT", "/inboxes/{inbox_id}/threads/{thread_id}/labels"): lambda c, b: (
        c.inboxes.threads.labels.set(INBOX, ID, b)
    ),
    ("DELETE", "/inboxes/{inbox_id}/threads/{thread_id}/labels/{label_id}"): lambda c, b: (
        c.inboxes.threads.labels.remove(INBOX, ID, ID2)
    ),
    ("GET", "/inboxes/{inbox_id}/threads/{thread_id}/emails"): lambda c, b: (
        c.inboxes.threads.emails.list(INBOX, ID)
    ),
    ("GET", "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}"): lambda c, b: (
        c.inboxes.threads.emails.get(INBOX, ID, ID2)
    ),
    ("POST", "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}/reply"): lambda c, b: (
        c.inboxes.threads.emails.reply(INBOX, ID, ID2, b, idempotency_key="key-3")
    ),
    ("POST", "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}/forward"): lambda c, b: (
        c.inboxes.threads.emails.forward(INBOX, ID, ID2, b)
    ),
    ("GET", "/inboxes/{inbox_id}/quarantine"): lambda c, b: c.inboxes.quarantine.list(INBOX),
    ("POST", "/inboxes/{inbox_id}/quarantine/{email_id}/release"): lambda c, b: (
        c.inboxes.quarantine.release(INBOX, ID, b)
    ),
    ("GET", "/inboxes/{inbox_id}/codes/latest"): lambda c, b: c.inboxes.codes.latest(
        INBOX, wait="30"
    ),
    ("POST", "/inboxes/{inbox_id}/drafts"): lambda c, b: c.inboxes.drafts.create(INBOX, b),
    ("GET", "/inboxes/{inbox_id}/drafts"): lambda c, b: c.inboxes.drafts.list(INBOX),
    ("GET", "/inboxes/{inbox_id}/drafts/{draft_id}"): lambda c, b: c.inboxes.drafts.get(INBOX, ID),
    ("PATCH", "/inboxes/{inbox_id}/drafts/{draft_id}"): lambda c, b: c.inboxes.drafts.update(
        INBOX, ID, b
    ),
    ("DELETE", "/inboxes/{inbox_id}/drafts/{draft_id}"): lambda c, b: c.inboxes.drafts.delete(
        INBOX, ID
    ),
    ("POST", "/inboxes/{inbox_id}/drafts/{draft_id}/send"): lambda c, b: c.inboxes.drafts.send(
        INBOX, ID, idempotency_key="key-4"
    ),
    ("POST", "/inboxes/{inbox_id}/drafts/{draft_id}/reject"): lambda c, b: c.inboxes.drafts.reject(
        INBOX, ID
    ),
    # Agent
    ("POST", "/agent/signup"): lambda c, b: c.agent.signup(b),
    ("GET", "/agent/status"): lambda c, b: c.agent.status(),
    ("POST", "/agent/claim-email"): lambda c, b: c.agent.claim_email(),
}

# Operations that only ever answer an error (no success response in the contract).
ERROR_ONLY = {o.key for o in OPERATIONS if not o.has_success()}


def body_for(key: tuple[str, str]) -> Any:
    op = next(o for o in OPERATIONS if o.key == key)
    request_body = op.spec.get("requestBody")
    if request_body is None:
        return None
    return example(request_body["content"]["application/json"]["schema"], full=False)


def test_table_matches_the_contract() -> None:
    assert len(OPERATION_KEYS) == 136
    assert set(CALLS) == OPERATION_KEYS


def _check_result(key: tuple[str, str], result: Any, mock: ContractMock) -> None:
    from .contract import validate

    op = next(o for o in OPERATIONS if o.key == key)
    _, media, schema = op.success()
    if media == "application/json" and schema is not None:
        validate(result, schema, f"{key} result")
        assert mock.last.operation.key == key


def test_every_operation_sync(client: BillionSend, mock: ContractMock) -> None:
    from billionsend import BillionSendError, EventStream

    for key, call in CALLS.items():
        if key in ERROR_ONLY:
            try:
                call(client, body_for(key))
            except BillionSendError:
                pass
            else:  # pragma: no cover - the contract answers an error
                raise AssertionError(f"{key} should have raised")
            continue
        result = call(client, body_for(key))
        if isinstance(result, EventStream):
            with result:
                events = list(result)
            assert events
            assert events[0].event == "inbox.email.received"
            continue
        _check_result(key, result, mock)
    assert mock.hits == OPERATION_KEYS


async def test_every_operation_async(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    from billionsend import AsyncEventStream, BillionSendError

    for key, call in CALLS.items():
        if key in ERROR_ONLY:
            try:
                await call(async_client, body_for(key))
            except BillionSendError:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"{key} should have raised")
            continue
        result = call(async_client, body_for(key))
        assert inspect.isawaitable(result), f"{key}: async method must be awaited"
        result = await result
        if isinstance(result, AsyncEventStream):
            async with result:
                events = [e async for e in result]
            assert events
            continue
        _check_result(key, result, mock)
    assert mock.hits == OPERATION_KEYS


def _public_methods(resource: Any, prefix: str) -> set[str]:
    names: set[str] = set()
    for name, value in vars(resource).items():
        if not name.startswith("_") and type(value).__name__.endswith("Resource"):
            names |= _public_methods(value, f"{prefix}.{name}")
    for name, _ in inspect.getmembers(type(resource), inspect.isfunction):
        if not name.startswith("_"):
            names.add(f"{prefix}.{name}")
    return names


def test_sync_and_async_have_the_same_surface() -> None:
    sync = BillionSend("bs_x")
    async_ = AsyncBillionSend("bs_x")
    resources = [n for n, v in vars(sync).items() if type(v).__name__.endswith("Resource")]
    sync_methods: set[str] = set()
    async_methods: set[str] = set()
    for name in resources:
        sync_methods |= _public_methods(getattr(sync, name), name)
        async_methods |= _public_methods(getattr(async_, name), name)
    assert sync_methods == async_methods
    operations = {m for m in sync_methods if not m.endswith("_all")}
    assert len(operations) == len(OPERATION_KEYS) == 136


def test_idempotency_key_where_the_contract_declares_it() -> None:
    """Operations declaring the Idempotency-Key header take ``idempotency_key``; no other does."""
    client = BillionSend("bs_x")
    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for path, item in raw["paths"].items():
        for method, op in item.items():
            declared = any(
                p["in"] == "header" and p["name"] == "Idempotency-Key"
                for p in op.get("parameters", [])
            )
            obj: Any = client
            for part in op["operationId"].split("."):
                obj = getattr(obj, re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part).lower())
            takes = "idempotency_key" in inspect.signature(obj).parameters
            assert takes == declared, (method.upper(), path)


def test_methods_are_named_after_operation_ids() -> None:
    """``CALLS`` reaches each operation through the method its operationId names."""
    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for path, item in raw["paths"].items():
        for method, op in item.items():
            snake = ".".join(
                re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part).lower()
                for part in op["operationId"].split(".")
            )
            source = inspect.getsource(CALLS[(method.upper(), path)])
            assert f"c.{snake}(" in source, (method.upper(), path, snake)


def _method(client: Any, operation_id: str) -> Any:
    obj: Any = client
    for part in operation_id.split("."):
        obj = getattr(obj, re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part).lower())
    return obj


def test_every_cursor_list_has_an_iterator() -> None:
    """A list taking ``after`` / ``before`` and answering ``has_more`` has a ``*_all`` iterator."""
    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    sync, async_ = BillionSend("bs_x"), AsyncBillionSend("bs_x")
    lists = []
    for item in raw["paths"].values():
        op = item.get("get")
        if op is None:
            continue
        query = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
        schema = op["responses"]["200"]["content"].get("application/json", {}).get("schema", {})
        ref = schema.get("$ref", "")
        page = raw["components"]["schemas"][ref.rsplit("/", 1)[-1]] if ref else schema
        if {"after", "before"} <= query and "has_more" in page.get("properties", {}):
            lists.append(op["operationId"])
            for client in (sync, async_):
                method = _method(client, op["operationId"])
                assert callable(getattr(method.__self__, method.__name__ + "_all", None)), op[
                    "operationId"
                ]
    assert len(lists) == 21
