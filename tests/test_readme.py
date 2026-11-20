"""Every Python example of the README is run, as written, against the contract mock.

The blocks are taken out of README.md itself, so an example that stops working (a
renamed method, a field the API no longer has) fails here. A block no test below
claims fails too, so a new example cannot go in unrun.
"""

from __future__ import annotations

import asyncio
import json
import re
import textwrap
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from billionsend import BillionSend, verify_webhook
from billionsend._base_client import SyncAPIClient

from .contract import API_KEY, BASE_URL, OPERATIONS, SPEC, ChunkStream, ContractMock, example
from .test_webhooks import SECRET

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
BLOCKS = re.findall(r"```python\n(.*?)```", README, re.S)

# One marker per example; each must pick out exactly one block.
MARKERS = {
    "quick start": "client = BillionSend()  # reads BILLIONSEND_API_KEY\n\nemail =",
    "async": "async def main()",
    "signup": "BillionSend().agent.signup",
    "idempotency": "idempotency_key=f",
    "agent loop": "def answer(question: str)",
    "resume a stream": "last_event_id=saved_position",
    "webhooks": "def billionsend_webhook()",
    "csv import": 'with open("contacts.csv"',
    "brand": "client.brand.update(",
    "pagination": "client.contacts.list_all(limit=100)",
    "errors": "except NotFoundError:",
}


def block(name: str) -> str:
    found = [b for b in BLOCKS if MARKERS[name] in b]
    assert len(found) == 1, f"{name}: {len(found)} README blocks match"
    return str(found[0])


def test_every_python_block_of_the_readme_is_run_here() -> None:
    claimed = [b for b in BLOCKS if any(m in b for m in MARKERS.values())]
    assert len(claimed) == len(BLOCKS), "a README example has no test"
    assert len(MARKERS) == len(BLOCKS)
    for name in MARKERS:
        block(name)


@pytest.fixture
def env(mock: ContractMock, monkeypatch: pytest.MonkeyPatch) -> ContractMock:
    """``BillionSend()`` as the README writes it: key and URL from the environment, and the
    HTTP clients it makes talk to the mock."""
    monkeypatch.setenv("BILLIONSEND_API_KEY", API_KEY)
    monkeypatch.setenv("BILLIONSEND_BASE_URL", BASE_URL)
    sync, asynchronous = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda **kw: sync(transport=mock.transport(), **kw))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: asynchronous(transport=mock.transport(), **kw)
    )
    monkeypatch.setattr(SyncAPIClient, "_sleep", staticmethod(lambda seconds: None))
    return mock


def run(source: str, **names: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {"__name__": "__readme__", **names}
    exec(compile(source, "README.md", "exec"), scope)  # noqa: S102 - the README's own examples
    return scope


def response(path: str, **changes: Any) -> dict[str, Any]:
    op = next(o for o in OPERATIONS if o.path == path)
    body: dict[str, Any] = example(op.success()[2])
    return {**body, **changes}


def test_quick_start(env: ContractMock, capsys: pytest.CaptureFixture[str]) -> None:
    run(block("quick start"))
    assert (env.last.request.method, env.last.request.url.path) == ("POST", "/emails")
    assert env.last.body["subject"] == "Welcome to Acme"
    assert (
        capsys.readouterr().out.strip()
        == example({"$ref": "#/components/schemas/SendEmailResponse"})["id"]
    )


def test_async_example(env: ContractMock, capsys: pytest.CaptureFixture[str]) -> None:
    run(block("async"))
    assert env.last.request.url.path == "/emails/6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
    assert capsys.readouterr().out.strip() == "value"


def test_agent_signup(env: ContractMock, capsys: pytest.CaptureFixture[str]) -> None:
    signup = response("/agent/signup", api_key=API_KEY)
    env.respond("POST", "/agent/signup", 201, json_body=signup)
    run(block("signup"))
    assert env.calls[0].body == {"human_email": "owner@example.com", "name": "Scout"}
    assert "authorization" not in env.calls[0].request.headers
    assert env.calls[1].request.url.path == "/agent/status"
    status = example({"$ref": "#/components/schemas/AgentAccountStatus"})["status"]
    assert capsys.readouterr().out.strip() == f"{signup['inbox']['email_address']} {status}"


def test_idempotency_example(env: ContractMock) -> None:
    message = {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"}
    run(block("idempotency"), client=BillionSend(), message=message, user_id=42)
    assert env.last.request.headers["idempotency-key"] == "welcome/42"


def test_agent_loop(env: ContractMock) -> None:
    ids = {
        "thread": "11111111-1111-4111-8111-111111111111",
        "email": "22222222-2222-4222-8222-222222222222",
    }
    received = {
        "type": "inbox.email.received",
        "created_at": "2026-11-16T09:00:00.000Z",
        "inbox_id": "in_1",
        "data": {"thread_id": ids["thread"], "email_id": ids["email"]},
    }
    deleted = {**received, "type": "inbox.deleted", "data": {}}
    env.respond(
        "GET",
        "/inboxes/{inbox_id}/events",
        stream=ChunkStream(
            [
                f"id: 1\nevent: inbox.email.received\ndata: {json.dumps(received)}\n\n".encode(),
                f"id: 2\nevent: inbox.deleted\ndata: {json.dumps(deleted)}\n\n".encode(),
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )
    inbound = {
        **example(SPEC["components"]["schemas"]["InboxThreadEmail"]["anyOf"][0]),
        "extracted_text": "Where is my order?",
    }
    assert inbound["direction"] == "inbound"
    env.respond(
        "GET", "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}", json_body=inbound
    )
    run(block("agent loop"))
    stream_call, get_call, reply_call = env.calls[-3:]
    assert stream_call.query == {"types": ["inbox.email.received"]}
    assert (get_call.path_params["thread_id"], get_call.path_params["email_id"]) == tuple(
        ids.values()
    )
    assert reply_call.request.method == "POST"
    assert reply_call.request.url.path.endswith(
        f"/threads/{ids['thread']}/emails/{ids['email']}/reply"
    )
    assert reply_call.body == {"text": "Thanks, we got your message:\n\n> Where is my order?"}


def test_agent_loop_skips_mail_we_sent(env: ContractMock) -> None:
    ids = "11111111-1111-4111-8111-111111111111"
    body = {
        "type": "inbox.email.received",
        "created_at": "2026-11-16T09:00:00.000Z",
        "inbox_id": "in_1",
        "data": {"thread_id": ids, "email_id": ids},
    }
    gap = 'event: stream.gap\ndata: {"reason":"retention"}\n\n'
    gone = {**body, "type": "inbox.deleted", "data": {}}
    env.respond(
        "GET",
        "/inboxes/{inbox_id}/events",
        stream=ChunkStream(
            [
                gap.encode(),
                f"id: 1\nevent: inbox.email.received\ndata: {json.dumps(body)}\n\n".encode(),
                f"id: 2\nevent: inbox.deleted\ndata: {json.dumps(gone)}\n\n".encode(),
            ]
        ),
        headers={"content-type": "text/event-stream"},
    )
    env.respond(
        "GET",
        "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}",
        json_body=example(SPEC["components"]["schemas"]["InboxThreadEmail"]["anyOf"][1]),
    )
    run(block("agent loop"))
    assert [c.request.method for c in env.calls[-2:]] == ["GET", "GET"]
    assert not any(c.request.method == "POST" and "reply" in c.request.url.path for c in env.calls)


def test_resume_a_stream(env: ContractMock) -> None:
    client = BillionSend()
    scope = run(block("resume a stream"), client=client, saved_position="42")
    assert env.last.request.headers["last-event-id"] == "42"
    scope["stream"].close()


def test_wait_for_a_code(env: ContractMock) -> None:
    """The one-line example under the agent loop."""
    assert 'client.inboxes.codes.latest(inbox["id"], from_="example.com", wait=30)' in README
    BillionSend().inboxes.codes.latest("support@example.com", from_="example.com", wait=30)
    assert env.last.query == {"from": ["example.com"], "wait": ["30"]}


def test_webhook_example(env: ContractMock) -> None:
    import base64
    import hashlib
    import hmac
    import time

    payload = json.dumps(
        {
            "type": "email.bounced",
            "created_at": "2026-11-16T09:00:00.000Z",
            "data": {"email_id": "e1"},
        }
    )
    stamp = str(int(time.time()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    digest = hmac.new(key, f"msg_1.{stamp}.{payload}".encode(), hashlib.sha256).digest()
    headers = {
        "webhook-id": "msg_1",
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }
    routes: dict[str, Any] = {}
    bounced: list[str] = []
    app = types.SimpleNamespace(post=lambda path: lambda fn: routes.setdefault(path, fn))
    request = types.SimpleNamespace(get_data=lambda: payload.encode(), headers=headers)
    run(
        block("webhooks"),
        app=app,
        request=request,
        WEBHOOK_SECRET=SECRET,
        mark_bounced=bounced.append,
    )
    handler = routes["/webhooks/billionsend"]
    assert handler() == ("", 204)
    assert bounced == ["e1"]
    request.get_data = lambda: payload.replace("e1", "e2").encode()
    assert handler() == ("", 400)
    assert bounced == ["e1"]
    assert verify_webhook(payload, headers, SECRET)["type"] == "email.bounced"


def test_csv_import_example(
    env: ContractMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    (tmp_path / "contacts.csv").write_bytes(b"Email,First Name\nada@example.com,Ada\n")
    path = "/contacts/imports/{id}"
    env.respond("GET", path, json_body=response(path, status="queued"))
    env.respond("GET", path, json_body=response(path, status="in_progress"))
    env.respond("GET", path, json_body=response(path, status="completed"))
    run(block("csv import"), client=BillionSend())
    upload = env.calls[0]
    assert upload.request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert upload.body["file"] == "Email,First Name\nada@example.com,Ada\n"
    assert json.loads(upload.body["column_map"]) == {"email": "Email", "first_name": "First Name"}
    assert upload.body["on_conflict"] == "skip"
    assert [c.request.method for c in env.calls] == ["POST", "GET", "GET", "GET"]
    assert capsys.readouterr().out.startswith("completed ")


def test_brand_example(env: ContractMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    (tmp_path / "logo.png").write_bytes(png)
    (tmp_path / "acme-logo.png").write_bytes(png)
    domain_id = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
    run(block("brand"), client=BillionSend(), domain_id=domain_id)
    assert [(c.request.method, c.request.url.path) for c in env.calls] == [
        ("PATCH", "/brand"),
        ("PUT", "/brand/logo"),
        ("PUT", f"/domains/{domain_id}/unsubscribe-page/logo"),
        ("PATCH", f"/domains/{domain_id}"),
        ("DELETE", "/brand/logo"),
    ]
    assert env.calls[0].body == {"accent_color": "#2B44FF", "font": "geist", "powered_by": False}
    assert env.calls[1].body["file"] == png.decode("latin-1")
    assert 'filename="logo.png"' in env.calls[1].request.content.decode("latin-1")
    assert 'filename="acme-logo.png"' in env.calls[2].request.content.decode("latin-1")
    assert env.calls[3].body == {"unsubscribe_page": {"support_url": "https://acme.dev/support"}}


def test_pagination_example(env: ContractMock, capsys: pytest.CaptureFixture[str]) -> None:
    source = block("pagination")
    sync_part, async_part = source.split("# async\n")
    run(sync_part, client=BillionSend())
    assert env.last.query == {"limit": ["100"]}
    wrapped = "async def main():\n" + textwrap.indent(async_part, "    ")
    from billionsend import AsyncBillionSend

    async def go() -> None:
        scope = run(wrapped, async_client=AsyncBillionSend())
        await scope["main"]()

    asyncio.run(go())
    assert env.last.request.url.path == "/domains"
    assert len(capsys.readouterr().out.split()) == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, "no such domain"),
        (429, "retry in 7.0"),
        (500, "500 name_500 message 500 req-500"),
    ],
)
def test_error_example(
    env: ContractMock, capsys: pytest.CaptureFixture[str], status: int, expected: str
) -> None:
    for _ in range(3):
        env.error(
            "GET",
            "/domains/{id}",
            status,
            f"name_{status}",
            f"message {status}",
            headers={"retry-after": "7", "x-request-id": f"req-{status}"},
        )
    run(block("errors"))
    assert capsys.readouterr().out.strip() == expected
