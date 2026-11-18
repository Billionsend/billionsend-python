"""Fixtures of the live suite: the clients, the objects to clean up and the final check.

Run it by hand, never in CI::

    BILLIONSEND_E2E_API_KEY=bs_... uv run pytest e2e

It talks to the real API as the real account, next to real traffic. Read the rules at
the top of ``test_live.py`` before changing anything here.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import types
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contract import API_KEY as DRY_KEY
from tests.contract import BASE_URL as DRY_URL
from tests.contract import ContractMock

from billionsend import AsyncBillionSend, BillionSend, BillionSendError, NotFoundError

from .harness import BASE_URL, MAX_REQUESTS, Ledger, LiveTransport

KEY_VARIABLE = "BILLIONSEND_E2E_API_KEY"
DRY_RUN = os.environ.get("BILLIONSEND_E2E_DRY_RUN") == "1"
FROM = "SDK E2E <sdk-e2e@mail.billionsend.dev>"
# The only recipients this suite may mail: Amazon's success simulator.
SIMULATOR = "simulator.amazonses.com"
MAX_MAILS = 20


def ok(condition: Any, message: str = "") -> None:
    """Assert something about an answer. A dry run (the contract mock answering) only
    exercises the calls and counts the requests, so it cannot hold the mock to this."""
    if not DRY_RUN:
        assert condition, message


class _Refusal:
    """Stands in for the error a dry run's mock never raises."""

    value = types.SimpleNamespace(status_code=0, name="", request_id="", message="")

    def __enter__(self) -> _Refusal:
        return self

    def __exit__(self, *exc: object) -> bool:
        return True


def raises(error: type[BaseException]) -> Any:
    """``pytest.raises``, except in a dry run where the mock answers 200."""
    return _Refusal() if DRY_RUN else pytest.raises(error)


@dataclass
class Item:
    """An object this run created, and how to remove it."""

    kind: str
    ref: str
    delete: Callable[[], Any]


@dataclass
class Live:
    key: str = field(repr=False)  # never in a failure report
    ledger: Ledger
    transport: LiveTransport
    run: str = field(default_factory=lambda: secrets.token_hex(3))
    items: list[Item] = field(default_factory=list)
    mails: int = 0
    mock: ContractMock | None = None
    created: list[tuple[str, str]] = field(default_factory=list)
    left: list[str] | None = None
    test_requests: int = 0

    def __post_init__(self) -> None:
        self.prefix = f"sdk-e2e-python-{self.run}"
        self.http = httpx.Client(transport=self.transport, trust_env=False)
        self.base_url = DRY_URL if DRY_RUN else BASE_URL
        self.client = BillionSend(self.key, base_url=self.base_url, http_client=self.http)

    def new_async_client(self) -> tuple[AsyncBillionSend, Any]:
        """An async client of its own (its connections belong to one ``asyncio.run``)."""
        http = _REAL_ASYNC_CLIENT(transport=self.transport.fork(), trust_env=False)
        return AsyncBillionSend(self.key, base_url=self.base_url, http_client=http), http

    # -- names

    def name(self, what: str) -> str:
        return f"{self.prefix}-{what}"

    def address(self, what: str) -> str:
        """A contact or recipient address: the success simulator, with a plus tag of ours."""
        return f"success+{self.prefix}-{what}@{SIMULATOR}"

    def count_mail(self, n: int = 1) -> None:
        self.mails += n
        assert self.mails <= MAX_MAILS, "this suite may send 20 emails at most"

    # -- objects to clean up

    def track(self, kind: str, ref: str, delete: Callable[[], Any]) -> str:
        self.items.append(Item(kind, ref, delete))
        self.created.append((kind, ref))
        return ref

    def forget(self, ref: str) -> None:
        self.items = [i for i in self.items if i.ref != ref]

    def clients_for_snippets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make ``BillionSend()`` and ``AsyncBillionSend()`` (as the README writes them)
        read this suite's key and send through the throttled, checked transport."""
        monkeypatch.setenv("BILLIONSEND_API_KEY", self.key)
        monkeypatch.setenv("BILLIONSEND_BASE_URL", self.base_url)
        transport = self.transport
        monkeypatch.setattr(
            httpx, "Client", lambda **kw: _REAL_CLIENT(transport=transport, trust_env=False, **kw)
        )
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: _REAL_ASYNC_CLIENT(transport=transport.fork(), trust_env=False, **kw),
        )


_REAL_CLIENT = httpx.Client
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _UniqueIds(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Dry run only: the mock gives every object one id; give each answer its own."""

    CONSTANT = "0b6f5a2e-7c1d-4e8a-9f3b-2d4c6e8a0b1c"

    def __init__(self, inner: httpx.MockTransport) -> None:
        self._inner = inner

    def _fresh(self, response: httpx.Response) -> httpx.Response:
        if "json" not in response.headers.get("content-type", ""):
            return response
        text = response.text.replace(self.CONSTANT, str(uuid.uuid4()))
        return httpx.Response(
            response.status_code,
            headers={"content-type": "application/json"},
            content=text.encode(),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._fresh(self._inner.handle_request(request))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._fresh(await self._inner.handle_async_request(request))


def _live_key() -> str:
    key = os.environ.get(KEY_VARIABLE, "")
    if DRY_RUN:
        return DRY_KEY
    return key


@pytest.fixture(scope="session")
def live() -> Iterator[Live]:
    mock = ContractMock() if DRY_RUN else None
    if mock is not None:
        _seed(mock)
    ledger = Ledger(base_url=DRY_URL, min_gap=0.0, test_budget=10_000) if DRY_RUN else Ledger()
    transport = LiveTransport(
        ledger,
        _UniqueIds(mock.transport()) if mock else httpx.HTTPTransport(),
        lambda: _UniqueIds(mock.transport()) if mock else httpx.AsyncHTTPTransport(),
    )
    state = Live(_live_key(), ledger, transport, mock=mock)
    yield state
    if state.left is None:  # the last test did not get to it
        ledger.in_cleanup = True
        if ledger.stopped:
            time.sleep(0 if DRY_RUN else 30)  # let the limit window pass first
            ledger.stopped = None
            ledger.consecutive_429 = 0
        clean_up(state)
    _write_report(state, state.left or [])
    state.http.close()
    assert not state.left, f"objects left behind: {state.left}"


# What each kind of object is called in the list that shows it.
def _listed(client: BillionSend, kind: str) -> set[str]:
    def names(page: Any) -> set[str]:
        found: set[str] = set()
        for row in page["data"]:
            found |= {str(row[k]) for k in ("id", "email", "name") if row.get(k)}
        return found

    match kind:
        case "email":
            return names(client.emails.list(limit=100))
        case "contact":
            return names(client.contacts.list(limit=100))
        case "segment":
            return names(client.segments.list(limit=100))
        case "topic":
            return names(client.topics.list())
        case "contact_property":
            return names(client.contact_properties.list(limit=100))
        case "template":
            return names(client.templates.list(limit=100))
        case "broadcast":
            return names(client.broadcasts.list(limit=100))
        case "webhook":
            return names(client.webhooks.list(limit=100))
        case "event":
            return names(client.events.list(limit=100))
        case "automation":
            return names(client.automations.list(limit=100))
        case "api_key":
            return names(client.api_keys.list())
        case "suppression":
            return names(client.suppressions.list(limit=100))
    raise AssertionError(f"no list for {kind}")


def clean_up(state: Live) -> list[str]:
    """Delete what the tests did not, then look in the lists: what is still there is returned."""
    state.test_requests = state.ledger.requests
    state.ledger.in_cleanup = True
    left: list[str] = []
    for item in reversed(state.items):
        try:
            item.delete()
        except NotFoundError:
            pass
        except Exception as exc:  # cleanup goes on whatever one delete does
            left.append(f"{item.kind} {item.ref}: delete failed with {type(exc).__name__}")
    state.items.clear()
    for kind in sorted({k for k, _ in state.created}):
        try:
            present = _listed(state.client, kind)
        except Exception as exc:
            left.append(f"{kind}: could not be checked ({type(exc).__name__})")
            continue
        left += [
            f"{k} {ref}: still listed" for k, ref in state.created if k == kind and ref in present
        ]
    state.left = left
    return left


def _write_report(state: Live, left: list[str]) -> None:
    path = os.environ.get("BILLIONSEND_E2E_REPORT")
    if not path:
        return
    summary: dict[str, Any] = {
        "run": state.run,
        "requests": state.ledger.requests,
        "requests_before_cleanup": state.test_requests,
        "mails": state.mails,
        "requests_checked": state.ledger.checked_requests,
        "answers_checked": state.ledger.checked_answers,
        "violations": state.ledger.violations,
        "left_behind": left,
        "calls": [f"{where} -> {status}" for where, status in state.ledger.calls],
    }
    Path(path).write_text(json.dumps(summary, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def contract_holds(live: Live) -> Iterator[None]:
    """A test fails when a request it made or an answer it got broke the published contract."""
    before = len(live.ledger.violations)
    yield
    new = live.ledger.violations[before:]
    assert not new, "the published contract was broken:\n" + "\n".join(new)


def _seed(mock: ContractMock) -> None:
    """Dry run only: answers the contract mock cannot make up from schemas."""
    import base64

    secret = "whsec_" + base64.b64encode(bytes(range(32))).decode()
    mock.respond(
        "POST",
        "/webhooks",
        json_body={
            "object": "webhook",
            "id": "11111111-1111-4111-8111-111111111111",
            "signing_secret": secret,
        },
    )
    mock.respond(
        "POST",
        "/webhooks/{id}/signing-secret/rotate",
        json_body={
            "object": "webhook",
            "id": "11111111-1111-4111-8111-111111111111",
            "signing_secret": secret,
            "previous_secret_expires_at": None,
        },
    )
    done = {
        "object": "contact_import",
        "id": "11111111-1111-4111-8111-111111111111",
        "status": "completed",
        "created_at": "2026-11-19T00:00:00.000Z",
        "completed_at": "2026-11-19T00:00:01.000Z",
        "counts": {"total": 1, "created": 1, "updated": 0, "skipped": 0, "failed": 0},
    }
    for _ in range(4):
        mock.respond("GET", "/contacts/imports/{id}", json_body=done)
    mock.respond(
        "POST",
        "/api-keys",
        json_body={"id": "11111111-1111-4111-8111-111111111111", "token": DRY_KEY},
    )


__all__ = ["BillionSendError", "Live", "MAX_REQUESTS"]
