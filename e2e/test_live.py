"""The Python SDK against the real BillionSend API.

Off by default: it needs ``BILLIONSEND_E2E_API_KEY`` and is never run by CI::

    BILLIONSEND_E2E_API_KEY=bs_... uv run pytest e2e

Every answer is checked against the published OpenAPI document, so a snapshot that
drifted from the API fails here. Every runnable README example is run.

The account is a real company account, so the rules are part of the code:

* existing objects are only read, never changed or deleted;
* everything created carries ``sdk-e2e-python-<random>`` and is deleted at the end, or
  by the cleanup that runs even when a test fails; the last test lists what is left;
* mail goes only from ``SDK E2E <sdk-e2e@mail.billionsend.dev>`` and only to the
  success simulator (``success+sdk-e2e-…@simulator.amazonses.com``), at most 20 mails;
  the harness refuses any other recipient (to, cc, bcc), and any address this run put on the
  suppression list, before the request is sent (``Ledger.guard_mail``);
* no domain is created or claimed, no broadcast is sent, no inbox is touched, no contact
  is unsubscribed (that is permanent), no webhook secret or key of the account is changed;
* 150 requests at most for the whole run, one a second, and the run stops when the API
  answers 429 twice in a row.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import os
import re
import time
import types
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contract import SNAPSHOT

from billionsend import (
    APITimeoutError,
    AuthenticationError,
    BillionSend,
    ConflictError,
    NotFoundError,
    ValidationError,
    verify_webhook,
)
from billionsend.types import SendEmailRequest

from .conftest import DRY_RUN, FROM, KEY_VARIABLE, Live, clean_up, ok, raises

if not os.environ.get(KEY_VARIABLE) and not DRY_RUN:
    pytest.skip(f"{KEY_VARIABLE} is not set", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def readme_block(marker: str) -> str:
    blocks = [b for b in re.findall(r"```python\n(.*?)```", README, re.S) if marker in b]
    assert len(blocks) == 1, f"{marker!r} matches {len(blocks)} README examples"
    return str(blocks[0])


def run_readme(source: str, **names: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {"__name__": "__readme__", **names}
    exec(compile(source, "README.md", "exec"), scope)  # noqa: S102 - the README's own example
    return scope


def later(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ----------------------------------------------------------------------------------
# The published document


def test_the_snapshot_is_the_published_document() -> None:
    """The document the SDK was built from is the one the API publishes right now."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_openapi", ROOT / "scripts" / "sync_openapi.py"
    )
    assert spec
    assert spec.loader
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    if DRY_RUN:
        pytest.skip("dry run")
    with httpx.Client(trust_env=False, timeout=30) as http:
        response = http.get(sync.DEFAULT_URL, headers={"Accept": "application/json"})
    response.raise_for_status()
    published, _ = sync.sanitize(response.json())
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert published == snapshot


# ----------------------------------------------------------------------------------
# Reading what exists (nothing here changes anything)


def test_the_key_works_and_usage_has_the_published_shape(live: Live) -> None:
    usage = live.client.usage.get()
    ok(usage["emails"]["daily"]["used"] >= 0)
    ok("monthly" in usage["emails"])
    ok("rate_limit" in usage)


def test_reading_the_account(live: Live) -> None:
    c = live.client
    ok(c.deliverability.get())
    domains = c.domains.list(limit=2)
    ok(domains["object"] == "list")
    ok(domains["data"], "the account has verified domains")
    domain = c.domains.get(domains["data"][0]["id"])
    ok(domain["id"] == domains["data"][0]["id"])
    ok(c.verifications.availability() is not None)
    ok(c.oauth_grants.list() is not None)
    ok(c.agent.status()["status"])


def test_reading_lists_of_existing_objects(live: Live) -> None:
    c = live.client
    # The lists of what the suite creates are read at the end, 100 rows each, by the final check.
    reads: list[tuple[str, Any]] = [
        ("received", c.emails.receiving.list(limit=2)),
        ("unsubscribes", c.unsubscribes.list(limit=2)),
        ("verifications", c.verifications.list(limit=2)),
        ("imports", c.contacts.imports.list(limit=2)),
    ]
    for name, page in reads:
        ok(isinstance(page["data"], list), name)
    logs = c.logs.list(limit=2)
    if logs["data"]:
        ok(c.logs.get(logs["data"][0]["id"])["id"] == logs["data"][0]["id"])


def test_reading_the_metrics(live: Live) -> None:
    c = live.client
    start = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    end = datetime.now(UTC).strftime("%Y-%m-%d")
    ok(c.emails.metrics(metrics=["sent", "delivered"], start_date=start, end_date=end))
    ok(c.emails.metrics.links(start_date=start, end_date=end, limit=2) is not None)
    ok(c.emails.metrics.countries(start_date=start, end_date=end) is not None)
    ok(c.emails.metrics.clients(start_date=start, end_date=end) is not None)


def test_pagination_walks_a_real_list_past_its_first_page(live: Live) -> None:
    """The team has thousands of contacts: two pages of two prove the cursor works."""
    seen = list(itertools.islice(live.client.contacts.list_all(limit=2), 4))
    ok(len(seen) == 4)
    ok(len({c["id"] for c in seen}) == 4, "a page repeated an item")


# ----------------------------------------------------------------------------------
# Sending (the README's examples, with our sender and the simulator)


def readme_message(live: Live, subject: str) -> SendEmailRequest:
    return {
        "from": FROM,
        "to": [live.address("mail")],
        "subject": subject,
        "html": "<p>SDK end-to-end test, nothing to do.</p>",
        "tags": [{"name": "sdk_e2e", "value": live.prefix}],
        "attachments": [
            {"filename": "sdk-e2e.txt", "content": base64.b64encode(b"hello").decode()}
        ],
    }


def test_the_quick_start_example_sends_one_email(
    live: Live, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live.clients_for_snippets(monkeypatch)
    source = readme_block("client = BillionSend()  # reads BILLIONSEND_API_KEY\n\nemail =")
    source = source.replace("Acme <hello@acme.dev>", FROM)
    assert "success@simulator.amazonses.com" in source
    source = source.replace("success@simulator.amazonses.com", live.address("quick"))
    live.count_mail()
    run_readme(source)
    printed = capsys.readouterr().out.strip()
    email_id = str(uuid.UUID(printed)) if not DRY_RUN else printed
    live.track("email", email_id, lambda: live.client.emails.delete(email_id))


def test_a_send_is_idempotent_and_its_email_can_be_read(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = live.client
    live.clients_for_snippets(monkeypatch)
    message = readme_message(live, f"{live.prefix} idempotent")
    source = readme_block("idempotency_key=f")
    live.count_mail()
    answers: list[Any] = []

    def send(*args: Any, **kwargs: Any) -> Any:
        answers.append(c.emails.send(*args, **kwargs))
        return answers[-1]

    run_readme(
        source,
        client=types.SimpleNamespace(emails=types.SimpleNamespace(send=send)),
        message=message,
        user_id=live.run,
    )
    sent = answers[0]
    live.track("email", sent["id"], lambda: c.emails.delete(sent["id"]))
    # The same key and the same message again: the first answer, nothing is sent twice.
    ok(c.emails.send(message, idempotency_key=f"welcome/{live.run}") == sent)
    # The same key with another message is refused.
    with raises(ConflictError) as info:
        other: SendEmailRequest = {**message, "subject": "another"}
        c.emails.send(other, idempotency_key=f"welcome/{live.run}")
    ok(info.value.status_code == 409)
    ok(info.value.name == "invalid_idempotent_request")
    ok(info.value.request_id)

    email = c.emails.get(sent["id"])
    ok(email["id"] == sent["id"])
    ok(email["subject"] == message["subject"])
    ok(email["to"] == message["to"])
    ok(email["from"] == FROM)

    attachments = c.emails.attachments.list(sent["id"])
    if attachments["data"]:
        ok(c.emails.attachments.get(sent["id"], attachments["data"][0]["id"]))
    ok(c.emails.insights(sent["id"]) is not None)
    shared = c.emails.share(sent["id"], {"expires_in": "10 minutes"})
    ok(shared["url"].startswith("https://"))
    ok(c.emails.unshare(sent["id"]) is not None)


def test_a_batch_and_a_scheduled_email(live: Live) -> None:
    c = live.client
    messages: list[SendEmailRequest] = [
        {
            "from": FROM,
            "to": [live.address(f"batch{i}")],
            "subject": f"{live.prefix} batch {i}",
            "text": "SDK e2e",
        }
        for i in (1, 2)
    ]
    live.count_mail(2)
    batch = c.emails.batch(messages, idempotency_key=f"{live.prefix}-batch")
    ok(len(batch["data"]) == 2)
    for row in batch["data"]:
        live.track("email", row["id"], partial(c.emails.delete, row["id"]))

    live.count_mail()
    scheduled = c.emails.send(
        {
            "from": FROM,
            "to": [live.address("later")],
            "subject": f"{live.prefix} scheduled",
            "text": "never sent",
            "scheduled_at": later(3),
        },
        idempotency_key=f"{live.prefix}-scheduled",
    )
    live.track("email", scheduled["id"], lambda: c.emails.delete(scheduled["id"]))
    c.emails.update(scheduled["id"], {"scheduled_at": later(4)})
    c.emails.cancel(scheduled["id"])


# ----------------------------------------------------------------------------------
# Contacts and what they hang on


def test_segments_topics_properties_and_contacts(
    live: Live, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    c = live.client
    segment = c.segments.create({"name": live.name("segment")})
    live.track("segment", segment["id"], lambda: c.segments.delete(segment["id"]))
    topic = c.topics.create({"name": live.name("topic"), "default_subscription": "opt_in"})
    live.track("topic", topic["id"], lambda: c.topics.delete(topic["id"]))
    key = live.prefix.replace("-", "_")
    prop = c.contact_properties.create({"key": key, "type": "string", "fallback_value": "none"})
    live.track("contact_property", prop["id"], lambda: c.contact_properties.delete(prop["id"]))

    email = live.address("contact")
    created = c.contacts.create(
        {
            "email": email,
            "first_name": "SDK",
            "last_name": "E2E",
            "segments": [{"id": segment["id"]}],
            "topics": [{"id": topic["id"], "subscription": "opt_in"}],
            "properties": {key: "one"},
        }
    )
    live.track("contact", created["id"], lambda: c.contacts.delete(created["id"]))
    contact = c.contacts.get(created["id"])
    ok(contact["email"] == email)
    properties: Any = contact["properties"]
    ok(properties.get(key) == {"type": "string", "value": "one"})
    # An address with a plus sign and an @ in the path reaches the same contact.
    ok(c.contacts.get(email)["id"] == created["id"])
    updated = c.contacts.update(created["id"], {"first_name": "SDK2", "properties": {key: "two"}})
    ok(updated["id"] == created["id"])
    ok(c.contacts.preferences_link(created["id"])["url"].startswith("https://"))
    ok(c.contacts.topics.list(created["id"])["data"])
    c.contacts.topics.update(created["id"], [{"id": topic["id"], "subscription": "opt_in"}])
    ok(c.contacts.segments.list(created["id"])["data"])
    c.contacts.segments.remove(created["id"], segment["id"])
    c.contacts.segments.add(created["id"], segment["id"])
    ok(c.segments.contacts.list(segment["id"])["data"])

    c.segments.update(segment["id"], {"name": live.name("segment-renamed")})
    ok(c.segments.get(segment["id"])["name"] == live.name("segment-renamed"))
    c.topics.update(topic["id"], {"description": "sdk e2e"})
    ok(c.topics.get(topic["id"])["description"] == "sdk e2e")
    c.contact_properties.update(prop["id"], {"fallback_value": "other"})
    ok(c.contact_properties.get(prop["id"])["fallback_value"] == "other")

    # Batches.
    first, second = live.address("batch-a"), live.address("batch-b")
    batch = c.contacts.batch.create(
        [{"email": first}, {"email": second, "first_name": "B"}], batch_validation="permissive"
    )
    ok(batch["counts"])
    for row in batch["data"]:
        live.track("contact", row["id"], partial(c.contacts.delete, row["id"]))
    got = c.contacts.batch.get({"contacts": [{"email": first}, {"email": second}]})
    ok(len(got["data"]) == 2)
    removed = c.contacts.batch.remove({"emails": [first, second]})
    ok(removed["data"])
    for row in batch["data"]:
        live.forget(row["id"])

    # The audience view of a contact.
    member = c.audiences.contacts.create(segment["id"], {"email": live.address("audience")})
    live.track("contact", member["id"], lambda: c.contacts.delete(member["id"]))
    ok(c.audiences.contacts.get(segment["id"], member["id"]))
    c.audiences.contacts.delete(segment["id"], member["id"])
    live.forget(member["id"])

    # The README's CSV import, run as written against a file of ours.
    monkeypatch.chdir(tmp_path)
    imported = live.address("import")
    (tmp_path / "contacts.csv").write_text(f"Email,First Name\n{imported},E2E\n", encoding="utf-8")
    live.track("contact", imported, lambda: c.contacts.delete(imported))
    live.clients_for_snippets(monkeypatch)
    scope = run_readme(readme_block('with open("contacts.csv"'), client=c)
    ok(scope["job"]["status"] == "completed", scope["job"])
    ok("completed" in capsys.readouterr().out)
    ok(scope["job"]["counts"])

    c.contacts.delete(created["id"])
    live.forget(created["id"])


# ----------------------------------------------------------------------------------
# Templates and broadcasts (a draft; nothing is sent)


def test_templates_and_a_draft_broadcast(live: Live) -> None:
    c = live.client
    template = c.templates.create(
        {
            "name": live.name("template"),
            "subject": "sdk e2e",
            "html": "<p>Hello from the SDK end-to-end test</p>",
        }
    )
    live.track("template", template["id"], lambda: c.templates.delete(template["id"]))
    c.templates.update(template["id"], {"subject": "sdk e2e 2"})
    ok(c.templates.get(template["id"])["subject"] == "sdk e2e 2")
    c.templates.publish(template["id"])
    copy = c.templates.duplicate(template["id"])
    live.track("template", copy["id"], lambda: c.templates.delete(copy["id"]))

    segment = c.segments.create({"name": live.name("broadcast-segment")})
    live.track("segment", segment["id"], lambda: c.segments.delete(segment["id"]))
    draft = c.broadcasts.create(
        {
            "name": live.name("broadcast"),
            "segment_id": segment["id"],
            "from": FROM,
            "subject": f"{live.prefix} draft",
            "html": "<p>Never sent. {{{UNSUBSCRIBE_URL}}}</p>",
        }
    )
    live.track("broadcast", draft["id"], lambda: c.broadcasts.delete(draft["id"]))
    got = c.broadcasts.get(draft["id"])
    ok(got["status"] == "draft")
    c.broadcasts.update(draft["id"], {"subject": f"{live.prefix} draft 2"})
    ok(c.broadcasts.get(draft["id"])["subject"].endswith("draft 2"))
    twin = c.broadcasts.duplicate(draft["id"])
    live.track("broadcast", twin["id"], lambda: c.broadcasts.delete(twin["id"]))
    ok(c.broadcasts.recipients(draft["id"], type="sent")["data"] == [])
    ok(c.broadcasts.clicked_links(draft["id"])["data"] == [])


# ----------------------------------------------------------------------------------
# Webhooks (the signing secret is the real one)


def test_a_webhook_and_the_verification_of_its_real_signing_secret(live: Live) -> None:
    c = live.client
    created = c.webhooks.create(
        {"endpoint": "https://example.com/sdk-e2e", "events": ["quota.reached"]}
    )
    live.track("webhook", created["id"], lambda: c.webhooks.delete(created["id"]))
    ok(created["signing_secret"].startswith("whsec_"))
    c.webhooks.update(created["id"], {"endpoint": "https://example.com/sdk-e2e-2"})
    ok(c.webhooks.get(created["id"])["endpoint"] == "https://example.com/sdk-e2e-2")
    rotated = c.webhooks.rotate_signing_secret(created["id"], {"overlap_hours": 0})
    ok(rotated["signing_secret"].startswith("whsec_"))
    ok(rotated["signing_secret"] != created["signing_secret"])

    # A delivery signed the Standard Webhooks way with the secret the API gave verifies.
    payload = json.dumps(
        {"type": "quota.reached", "created_at": "2026-11-19T00:00:00.000Z", "data": {}}
    )
    stamp = str(int(time.time()))
    key = base64.b64decode(rotated["signing_secret"].removeprefix("whsec_"))
    digest = hmac.new(key, f"msg_e2e.{stamp}.{payload}".encode(), hashlib.sha256).digest()
    headers = {
        "webhook-id": "msg_e2e",
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }
    ok(verify_webhook(payload, headers, rotated["signing_secret"])["type"] == "quota.reached")

    # The README's Flask example, with that secret.
    routes: dict[str, Any] = {}
    bounced: list[str] = []
    request = types.SimpleNamespace(get_data=lambda: payload.encode(), headers=headers)
    run_readme(
        readme_block("def billionsend_webhook()"),
        app=types.SimpleNamespace(post=lambda path: lambda fn: routes.setdefault(path, fn)),
        request=request,
        WEBHOOK_SECRET=rotated["signing_secret"],
        mark_bounced=bounced.append,
    )
    ok(routes["/webhooks/billionsend"]() == ("", 204))
    ok(bounced == [])  # a quota.reached delivery is not a bounce

    ok(c.webhooks.events.list(created["id"])["data"] == [])
    with raises(NotFoundError) as info:
        c.webhooks.events.get(created["id"], str(uuid.uuid4()))
    ok(info.value.status_code == 404)


# ----------------------------------------------------------------------------------
# Suppressions, events and automations


def test_suppressions_manual_entries_only(live: Live) -> None:
    c = live.client
    one, two = live.address("suppressed-1"), live.address("suppressed-2")
    created = c.suppressions.create({"email": one, "origin": "manual"})
    live.track("suppression", created["id"], lambda: c.suppressions.delete(created["id"]))
    ok(c.suppressions.get(created["id"])["email"] == one)
    added = c.suppressions.batch.add({"emails": [two], "origin": "manual"})
    ok(added["data"])
    live.track("suppression", two, lambda: c.suppressions.batch.remove({"emails": [two]}))
    ok(c.suppressions.batch.remove({"emails": [two]})["data"] is not None)
    live.forget(two)
    c.suppressions.delete(created["id"])
    live.forget(created["id"])


def test_custom_events_and_an_automation_that_never_runs(live: Live) -> None:
    c = live.client
    event_name = f"sdk-e2e.python.{live.run}.paid"
    event = c.events.create({"name": event_name, "schema": {"amount": "number"}})
    live.track("event", event_name, lambda: c.events.remove(event_name))
    ok(event["id"])
    ok(c.events.get(event_name)["name"] == event_name)
    c.events.update(event_name, {"schema": {"amount": "number", "note": "string"}})

    contact_email = live.address("event-contact")
    contact = c.contacts.create({"email": contact_email})
    live.track("contact", contact["id"], lambda: c.contacts.delete(contact["id"]))
    sent = c.events.send(
        {"event": event_name, "email": contact_email, "payload": {"amount": 5}},
        idempotency_key=f"{live.prefix}-event",
    )
    ok(sent["event"] == event_name)

    steps: list[Any] = [
        {"key": "start", "type": "trigger", "config": {"event_name": event_name}},
        {"key": "pause", "type": "delay", "config": {"duration": "1 hour"}},
    ]
    connections: list[Any] = [{"from": "start", "to": "pause"}]
    checked = c.automations.validate({"steps": steps, "connections": connections})
    ok(isinstance(checked["valid"], bool))
    automation = c.automations.create(
        {
            "name": live.name("automation"),
            "status": "disabled",
            "steps": steps,
            "connections": connections,
        }
    )
    live.track("automation", automation["id"], lambda: c.automations.remove(automation["id"]))
    got = c.automations.get(automation["id"])
    ok(got["status"] == "disabled")
    c.automations.update(automation["id"], {"name": live.name("automation-2")})
    twin = c.automations.duplicate(automation["id"])
    live.track("automation", twin["id"], lambda: c.automations.remove(twin["id"]))
    simulated = c.automations.simulate(automation["id"], {"contact": {"email": contact_email}})
    ok(simulated is not None)
    ok(c.automations.metrics(automation["id"]) is not None)
    ok(c.automations.runs.list(automation["id"])["data"] == [])
    with raises(NotFoundError):
        c.automations.runs.get(automation["id"], str(uuid.uuid4()))
    ok(c.automations.stop(automation["id"]) is not None)
    c.automations.remove(twin["id"])
    live.forget(twin["id"])


# ----------------------------------------------------------------------------------
# API keys, errors and the async client


def test_an_api_key_made_by_the_sdk_works_and_is_removed(live: Live) -> None:
    c = live.client
    made = c.api_keys.create({"name": live.name("key"), "permission": "full_access"})
    live.track("api_key", made["id"], lambda: c.api_keys.delete(made["id"]))
    token = made["token"]
    ok(token.startswith("bs_"))
    c.api_keys.update(made["id"], {"name": live.name("key-renamed")})
    with httpx.Client(transport=live.transport, trust_env=False) as http:
        scoped = BillionSend(token, base_url=live.base_url, http_client=http)
        ok(scoped.usage.get()["emails"])
    c.api_keys.delete(made["id"])
    live.forget(made["id"])


def test_errors_carry_the_api_shape(live: Live) -> None:
    with httpx.Client(transport=live.transport, trust_env=False) as http:
        stranger = BillionSend("bs_sdk_e2e_not_a_key", base_url=live.base_url, http_client=http)
        with raises(AuthenticationError) as denied:
            stranger.emails.list(limit=1)
    ok(denied.value.status_code == 401)
    ok(denied.value.name)
    ok(denied.value.request_id)

    with raises(NotFoundError) as missing:
        live.client.emails.get(str(uuid.uuid4()))
    ok((missing.value.status_code, missing.value.name) == (404, "not_found"))
    ok(missing.value.request_id)

    with live.ledger.invalid_request(), raises(ValidationError) as refused:
        live.client.templates.create({"name": live.name("empty"), "html": ""})
    ok(refused.value.status_code == 422)


def test_the_readme_error_and_pagination_examples(
    live: Live, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live.clients_for_snippets(monkeypatch)
    run_readme(readme_block("except NotFoundError:"))
    ok(capsys.readouterr().out.strip() == "no such domain")

    source = readme_block("client.contacts.list_all(limit=100)")
    sync_part, async_part = source.split("# async\n")
    # The same loop over the same call, bounded: the account has thousands of contacts.
    bounded = sync_part.replace(
        "client.contacts.list_all(limit=100)",
        "itertools.islice(client.contacts.list_all(limit=2), 3)",
    )
    assert bounded != sync_part
    shown: list[Any] = []  # the example prints real contacts' addresses: keep them out of any log
    run_readme(bounded, client=live.client, itertools=itertools, print=lambda *a: shown.append(a))
    ok(len(shown) == 3)
    import textwrap

    wrapped = "async def main():\n" + textwrap.indent(async_part, "    ")
    client, http = live.new_async_client()
    scope = run_readme(wrapped, async_client=client)

    async def walk() -> None:
        try:
            await scope["main"]()
        finally:
            await http.aclose()

    asyncio.run(walk())
    ok(capsys.readouterr().out.strip())  # the account's domain names


def test_the_readme_async_example_and_the_async_client(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_id = next((r for kind, r in live.created if kind == "email"), None)
    assert email_id, "an earlier test made an email"
    source = readme_block("async def main()").replace(
        "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f", email_id
    )
    live.clients_for_snippets(monkeypatch)
    run_readme(source)


def test_the_readme_stream_resume_example(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    """The team's event stream only reads (no inbox is touched). A fresh connection says where
    it started before any event, so a quiet stream still has a position to resume from."""
    stream = live.client.inboxes.events.stream(timeout=httpx.Timeout(10, read=2), max_reconnects=0)
    seen = 0
    try:
        for _ in stream:  # the team's own events, if any, until two seconds of silence
            seen += 1
            if seen >= 20:
                break
    except APITimeoutError:
        pass
    finally:
        stream.close()
    position = stream.last_event_id
    ok(position, "the stream gave no position to resume from")

    live.clients_for_snippets(monkeypatch)
    scope = run_readme(
        readme_block("last_event_id=saved_position"), client=BillionSend(), saved_position=position
    )
    scope["stream"].close()


# Examples the live rules at the top do not allow to run, and why: each would create what the
# rules forbid (an inbox, a new account).
EXCUSED_README_EXAMPLES = {
    "BillionSend().agent.signup": "creates a new account and its inbox",
    "def answer(question: str)": "creates an inbox and answers mail sent to it",
}


def test_every_readme_example_is_run_here_or_excused() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    markers = [
        *re.findall(r'readme_block\(\s*(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\')', source),
    ]
    run = [(a or b).encode().decode("unicode_escape") for a, b in markers]
    blocks = re.findall(r"```python\n(.*?)```", README, re.S)
    for block in blocks:
        ran = any(marker in block for marker in run)
        excused = any(marker in block for marker in EXCUSED_README_EXAMPLES)
        assert ran != excused, f"a README example is neither run live nor excused:\n{block[:200]}"


# ----------------------------------------------------------------------------------
# The end: delete what is left and list what survived


def test_zz_nothing_is_left_behind(live: Live) -> None:
    left = clean_up(live)
    assert not left, f"objects left behind: {left}"
    assert live.ledger.requests <= 150
    assert live.mails <= 20
