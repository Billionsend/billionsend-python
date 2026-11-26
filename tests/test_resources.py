"""Typical calls per resource: where each argument lands (path, query, header, body)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import httpx

from billionsend import AsyncBillionSend, BillionSend
from billionsend.types import SendEmailRequest

from .contract import ContractMock

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
ID2 = "7b2e2e49-4d66-4b5f-8b68-1a3c4d5e6f70"


def test_send_email_body_and_idempotency_key(client: BillionSend, mock: ContractMock) -> None:
    params: SendEmailRequest = {
        "from": "Acme <hello@acme.dev>",
        "to": ["a@example.com", "b@example.com"],
        "subject": "Welcome",
        "html": "<p>Hi</p>",
        "tags": [{"name": "kind", "value": "welcome"}],
    }
    result = client.emails.send(params, idempotency_key="welcome/42")
    call = mock.last
    assert call.request.method == "POST"
    assert call.request.url.path == "/emails"
    assert call.body == params
    assert call.request.headers["idempotency-key"] == "welcome/42"
    assert call.request.headers["content-type"] == "application/json"
    assert "id" in result


def test_send_email_without_idempotency_key_gets_one(
    client: BillionSend, mock: ContractMock
) -> None:
    client.emails.send({"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"})
    assert len(mock.last.request.headers["idempotency-key"]) == 36


def test_batch_send_is_a_json_array(client: BillionSend, mock: ContractMock) -> None:
    emails: list[SendEmailRequest] = [
        {"from": "a@acme.dev", "to": "b@example.com", "subject": "1", "text": "one"},
        {"from": "a@acme.dev", "to": "c@example.com", "subject": "2", "text": "two"},
    ]
    client.emails.batch(emails, idempotency_key="batch-1")
    assert mock.last.body == emails
    assert mock.last.request.headers["idempotency-key"] == "batch-1"


def test_list_query_parameters(client: BillionSend, mock: ContractMock) -> None:
    client.emails.list(limit=25, after=ID)
    assert mock.last.query == {"limit": ["25"], "after": [ID]}
    client.emails.list()
    assert mock.last.query == {}


def test_array_query_parameters_are_comma_separated(
    client: BillionSend, mock: ContractMock
) -> None:
    client.emails.metrics(
        start_date="2026-01-01",
        metrics=["sent", "delivered", "open_rate"],
        dimensions=["domain"],
        domain_id=[ID, ID2],
        granularity="daily",
    )
    assert mock.last.query["metrics"] == ["sent,delivered,open_rate"]
    assert mock.last.query["domain_id"] == [f"{ID},{ID2}"]
    assert mock.last.query["granularity"] == ["daily"]


def test_boolean_query_parameters(client: BillionSend, mock: ContractMock) -> None:
    client.emails.receiving.list(unread=True, include_quarantined=False, wait=20)
    assert mock.last.query == {"unread": ["true"], "include_quarantined": ["false"], "wait": ["20"]}
    client.contacts.delete(ID, erase="true")
    assert mock.last.query == {"erase": ["true"]}


def test_receiving_list_takes_an_inbox_id_or_an_email_address(
    client: BillionSend, mock: ContractMock
) -> None:
    client.emails.receiving.list(inbox_id=ID)
    assert mock.last.query == {"inbox_id": [ID]}
    client.emails.receiving.list(inbox_id="agent@acme.billionsend.app")
    assert mock.last.query == {"inbox_id": ["agent@acme.billionsend.app"]}


def test_keyword_named_query_parameter(client: BillionSend, mock: ContractMock) -> None:
    client.inboxes.threads.search(
        "inbox@example.com", from_="boss@example.com", has_attachment=True
    )
    assert mock.last.query == {"from": ["boss@example.com"], "has_attachment": ["true"]}


def test_path_parameters_are_encoded(client: BillionSend, mock: ContractMock) -> None:
    client.inboxes.threads.emails.get("support@example.com", ID, ID2, include_quarantined=True)
    call = mock.last
    assert call.path_params == {"inbox_id": "support@example.com", "thread_id": ID, "email_id": ID2}
    assert call.request.url.raw_path.startswith(b"/inboxes/support@example.com/threads/")
    client.contacts.get("a/b c+d@e.com")
    assert mock.last.request.url.raw_path == b"/contacts/a%2Fb%20c%2Bd@e.com"


def test_header_parameter(client: BillionSend, mock: ContractMock) -> None:
    client.contacts.batch.create(
        [{"email": "a@example.com"}, {"email": "b@example.com", "first_name": "B"}],
        on_conflict="skip",
        batch_validation="permissive",
    )
    call = mock.last
    assert call.request.headers["x-batch-validation"] == "permissive"
    assert call.query == {"on_conflict": ["skip"]}
    assert call.body == [{"email": "a@example.com"}, {"email": "b@example.com", "first_name": "B"}]


def test_optional_body(client: BillionSend, mock: ContractMock) -> None:
    client.inboxes.quarantine.release("inbox@example.com", ID)
    assert mock.last.request.content == b""
    client.inboxes.quarantine.release("inbox@example.com", ID, {"trust_sender": True})
    assert mock.last.body == {"trust_sender": True}
    client.inboxes.update("inbox@example.com")
    assert mock.last.request.content == b""


def test_no_body_operations_send_no_body(client: BillionSend, mock: ContractMock) -> None:
    client.broadcasts.cancel(ID)
    assert mock.last.request.content == b""
    assert "content-type" not in mock.last.request.headers


def test_agent_signup_sends_no_key(mock: ContractMock) -> None:
    import httpx

    from .contract import BASE_URL

    anonymous = BillionSend(base_url=BASE_URL, http_client=httpx.Client(transport=mock.transport()))
    anonymous.api_key = None
    result = anonymous.agent.signup({"human_email": "owner@example.com", "name": "Scout"})
    assert "authorization" not in mock.last.request.headers
    assert result


def test_resources_reach_their_paths(client: BillionSend, mock: ContractMock) -> None:
    cases: list[tuple[Callable[[], object], str, str]] = [
        (lambda: client.domains.verify(ID), "POST", f"/domains/{ID}/verify"),
        (lambda: client.api_keys.delete(ID), "DELETE", f"/api-keys/{ID}"),
        (lambda: client.contacts.segments.add(ID, ID2), "POST", f"/contacts/{ID}/segments/{ID2}"),
        (lambda: client.contacts.topics.list(ID), "GET", f"/contacts/{ID}/topics"),
        (lambda: client.audiences.contacts.get(ID, ID2), "GET", f"/audiences/{ID}/contacts/{ID2}"),
        (lambda: client.contact_properties.get(ID), "GET", f"/contact-properties/{ID}"),
        (lambda: client.segments.contacts.list(ID, limit=3), "GET", f"/segments/{ID}/contacts"),
        (lambda: client.topics.delete(ID), "DELETE", f"/topics/{ID}"),
        (lambda: client.broadcasts.duplicate(ID), "POST", f"/broadcasts/{ID}/duplicate"),
        (lambda: client.templates.publish(ID), "POST", f"/templates/{ID}/publish"),
        (
            lambda: client.webhooks.rotate_signing_secret(ID),
            "POST",
            f"/webhooks/{ID}/signing-secret/rotate",
        ),
        (lambda: client.suppressions.get(ID), "GET", f"/suppressions/{ID}"),
        (lambda: client.emails.metrics(), "GET", "/emails/metrics"),
        (lambda: client.emails.metrics.clients(), "GET", "/emails/metrics/clients"),
        (lambda: client.contacts.imports.get(ID), "GET", f"/contacts/imports/{ID}"),
        (lambda: client.domains.claims.verify(ID), "POST", f"/domains/{ID}/claim/verify"),
        (lambda: client.events.get("order.paid"), "GET", "/events/order.paid"),
        (lambda: client.events.remove("order.paid"), "DELETE", "/events/order.paid"),
        (lambda: client.automations.runs.get(ID, ID2), "GET", f"/automations/{ID}/runs/{ID2}"),
        (lambda: client.oauth_grants.revoke(ID), "DELETE", f"/oauth/grants/{ID}"),
        (lambda: client.verifications.availability(), "GET", "/verifications/availability"),
        (lambda: client.usage.get(), "GET", "/usage"),
        (lambda: client.deliverability.get(), "GET", "/deliverability"),
        (lambda: client.inboxes.safety.get("i@x.io"), "GET", "/inboxes/i@x.io/safety"),
        (lambda: client.inboxes.labels.list("i@x.io"), "GET", "/inboxes/i@x.io/labels"),
        (lambda: client.inboxes.codes.latest("i@x.io"), "GET", "/inboxes/i@x.io/codes/latest"),
        (
            lambda: client.inboxes.drafts.reject("i@x.io", ID),
            "POST",
            f"/inboxes/i@x.io/drafts/{ID}/reject",
        ),
        (lambda: client.agent.status(), "GET", "/agent/status"),
        (lambda: client.agent.claim_email(), "POST", "/agent/claim-email"),
        (
            lambda: client.emails.receiving.attachments.get(ID, ID2),
            "GET",
            f"/emails/receiving/{ID}/attachments/{ID2}",
        ),
    ]
    for call, method, path in cases:
        call()
        assert (mock.last.request.method, mock.last.request.url.path) == (method, path)


def test_extra_headers_and_timeout(client: BillionSend, mock: ContractMock) -> None:
    client.usage.get(extra_headers={"X-Trace": "abc"}, timeout=3.5)
    request = mock.last.request
    assert request.headers["x-trace"] == "abc"
    assert request.extensions["timeout"]["read"] == 3.5


def test_user_agent_and_accept(client: BillionSend, mock: ContractMock) -> None:
    from billionsend import __version__

    client.usage.get()
    assert mock.last.request.headers["user-agent"] == f"billionsend-python/{__version__}"
    assert mock.last.request.headers["accept"] == "application/json"


async def test_async_send_and_list(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    result = await async_client.emails.send(
        {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"},
        idempotency_key="k",
    )
    assert result["id"]
    assert mock.last.request.headers["idempotency-key"] == "k"
    page = await async_client.contacts.list(limit=2, include="properties")
    assert page["object"] == "list"
    assert mock.last.query == {"limit": ["2"], "include": ["properties"]}


async def test_async_inbox_reply(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    await async_client.inboxes.threads.emails.reply(
        "support@example.com",
        ID,
        ID2,
        {"text": "Thanks!", "cc": ["c@example.com"]},
        idempotency_key="r1",
    )
    call = mock.last
    assert call.request.url.path == f"/inboxes/support@example.com/threads/{ID}/emails/{ID2}/reply"
    assert json.loads(call.request.content) == {"text": "Thanks!", "cc": ["c@example.com"]}
    assert call.request.headers["idempotency-key"] == "r1"


def test_removed_operations_are_gone(client: BillionSend) -> None:
    """The unsubscribe delete and the old webhook rotate left the API; the SDK has no trace."""
    assert not hasattr(client.webhooks, "rotate")
    assert not hasattr(client.unsubscribes, "delete")


def test_the_usage_fields_the_readme_names_exist_in_the_contract() -> None:
    """The README says what ``usage.get()`` holds; the contract (not the SDK) must agree."""
    from .contract import SPEC

    schema = SPEC["components"]["schemas"]["UsageResponse"]["properties"]
    sentence = next(line for line in README.splitlines() if "account's usage (" in line)
    named = re.findall(r"`([a-z_]+(?:\.[a-z_]+)?)`", sentence.split("account's usage (")[1])
    assert {"emails.daily", "emails.monthly", "contacts", "domains"} <= set(named)
    for path in named:
        head, _, tail = path.partition(".")
        assert head in schema, f"usage has no {head}"
        if tail:
            assert tail in schema[head]["properties"], f"usage.{head} has no {tail}"
    # The old shape is gone.
    assert not {"limits", "today", "period"} & set(schema)


CSV = b"Email,First Name\nada@example.com,Ada\n"


def _parts(request: httpx.Request) -> dict[str, tuple[str | None, str | None, bytes]]:
    media = request.headers["content-type"]
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + media.encode() + b"\r\n\r\n" + request.content
    )
    out: dict[str, tuple[str | None, str | None, bytes]] = {}
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        out[str(part.get_param("name", header="content-disposition"))] = (
            part.get_filename(),
            part.get_content_type() if part.get_filename() else None,
            payload,
        )
    return out


def test_contact_import_is_a_multipart_upload(client: BillionSend, mock: ContractMock) -> None:
    result = client.contacts.imports.create(
        {
            "file": ("contacts.csv", CSV, "text/csv"),
            "column_map": {"email": "Email", "first_name": "First Name"},
            "segments": [{"id": ID}],
            "topics": json.dumps([{"id": ID2, "subscription": "opt_in"}]),
            "on_conflict": "skip",
        }
    )
    assert result
    request = mock.last.request
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = _parts(request)
    assert parts["file"] == ("contacts.csv", "text/csv", CSV)
    assert parts["on_conflict"][2] == b"skip"
    # A dict or list is encoded by the SDK; a str goes out as it is.
    assert json.loads(parts["column_map"][2]) == {"email": "Email", "first_name": "First Name"}
    assert json.loads(parts["segments"][2]) == [{"id": ID}]
    assert parts["topics"][2] == json.dumps([{"id": ID2, "subscription": "opt_in"}]).encode()


def test_contact_import_takes_bytes_and_file_objects(
    client: BillionSend, mock: ContractMock
) -> None:
    import io

    client.contacts.imports.create({"file": CSV})
    assert _parts(mock.last.request)["file"][2] == CSV
    client.contacts.imports.create({"file": io.BytesIO(CSV)})
    assert _parts(mock.last.request)["file"][2] == CSV


async def test_async_contact_import(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    await async_client.contacts.imports.create(
        {"file": ("c.csv", CSV), "column_map": {"email": "Email"}}
    )
    parts = _parts(mock.last.request)
    assert parts["file"] == ("c.csv", "text/csv", CSV)
    assert json.loads(parts["column_map"][2]) == {"email": "Email"}
