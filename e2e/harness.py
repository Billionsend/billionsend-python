"""What the live tests stand on: a throttled, counted, schema-checking transport.

Every request the SDK makes goes through :class:`LiveTransport`, which

* spaces requests at least ``MIN_GAP`` seconds apart (the key's team limit of 600 a
  minute is shared with real traffic) and stops at ``MAX_REQUESTS`` for the whole run;
* checks each request against the published OpenAPI document before it is sent;
* checks each answer against it after (status documented, body valid for that status);
* stops the run when the API answers 429 twice in a row.

Contract violations are collected, not raised, so the object a call created can still
be cleaned up; each test fails at its end if its calls broke the contract.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

import httpx
from tests.contract import check_request, find_operation, validate

BASE_URL = "https://api.billionsend.com"
MAX_REQUESTS = 150
# The tests may use this many; what is left is kept for cleanup and the final check.
TEST_BUDGET = 116
MIN_GAP = 1.05
MAX_429_IN_A_ROW = 2


class UnsafeMail(Exception):
    """A request that would mail an address this suite may not mail: it is not sent."""


# The only recipients the suite may mail: the success simulator and its plus aliases.
SIMULATOR_RECIPIENT = re.compile(r"success(\+[^@\s]+)?@simulator\.amazonses\.com", re.IGNORECASE)
RECIPIENT_FIELDS = ("to", "cc", "bcc")


def address_of(value: str) -> str:
    """The bare address of ``"Name <a@b>"`` or ``"a@b"``, lower-cased."""
    return (parseaddr(value)[1] or value).strip().lower()


def recipients(body: Any) -> list[str]:
    """Every address under a ``to`` / ``cc`` / ``bcc`` key, at any depth of a JSON body (a
    batch is a list of mails). A value without an ``@`` (an automation's ``"to": "pause"``
    names a step, not an address) is not an address."""
    found: list[str] = []
    if isinstance(body, dict):
        for key, value in body.items():
            if key in RECIPIENT_FIELDS:
                items = value if isinstance(value, list) else [value]
                found += [i for i in items if isinstance(i, str) and "@" in i]
            found += recipients(value)
    elif isinstance(body, list):
        for item in body:
            found += recipients(item)
    return found


# The calls that put addresses or whole domains on the suppression list, and the body fields
# holding them (an address field, a domain field).
SUPPRESSING_CALLS = {
    "/suppressions": ("email", "domain"),
    "/suppressions/batch/add": ("emails", "domains"),
}


def suppressed_by(request: httpx.Request, body: Any) -> list[str]:
    """The addresses a request puts on the suppression list; a whole domain is ``@domain``."""
    fields = SUPPRESSING_CALLS.get(request.url.path)
    if request.method != "POST" or fields is None or not isinstance(body, dict):
        return []
    found: list[str] = []
    for name, prefix in zip(fields, ("", "@"), strict=True):
        value = body.get(name)
        items = value if isinstance(value, list) else [value]
        found += [prefix + address_of(i).removeprefix("@") for i in items if isinstance(i, str)]
    return found


class BudgetExceeded(Exception):
    """The run has used the requests it is allowed."""


class RateLimitedTwice(Exception):
    """The API answered 429 twice in a row: stop, do not push."""


@dataclass
class Ledger:
    base_url: str = BASE_URL
    min_gap: float = MIN_GAP
    test_budget: int = TEST_BUDGET
    requests: int = 0
    in_cleanup: bool = False
    stopped: str | None = None
    consecutive_429: int = 0
    invalid_allowed: int = 0
    checked_requests: int = 0
    checked_answers: int = 0
    violations: list[str] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)
    # Every address (or ``@domain``) this run put on the suppression list, kept even after it
    # is taken off again: a suppressed address is never mailed.
    suppressed: set[str] = field(default_factory=set)
    _last: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def guard_mail(self, request: httpx.Request) -> None:
        """Refuse, before anything is sent or counted, a request that mails an address that
        is not the success simulator's, or one this run suppressed. Nothing switches it off:
        not a dry run, not ``invalid_request()``."""
        try:
            body = json.loads(request.content)
        except ValueError:
            return  # no JSON body (a multipart upload mails no one)
        for address in recipients(body):
            bare = address_of(address)
            if not SIMULATOR_RECIPIENT.fullmatch(bare):
                raise UnsafeMail(
                    f"{request.method} {request.url.path}: {bare} is not the simulator"
                )
            if bare in self.suppressed or "@" + bare.rpartition("@")[2] in self.suppressed:
                raise UnsafeMail(f"{request.method} {request.url.path}: {bare} is suppressed")
        self.suppressed.update(suppressed_by(request, body))

    def reserve(self) -> float:
        """Count one request and return how long to wait before sending it."""
        with self._lock:
            if self.stopped and not self.in_cleanup:
                raise RateLimitedTwice(self.stopped)
            limit = MAX_REQUESTS if self.in_cleanup else self.test_budget
            if self.requests >= limit:
                raise BudgetExceeded(f"{self.requests} requests used, the limit is {limit}")
            self.requests += 1
            now = time.monotonic()
            wait = max(0.0, self._last + self.min_gap - now)
            self._last = now + wait
            return wait

    def check_request(self, request: httpx.Request) -> None:
        if self.invalid_allowed:
            return
        try:
            check_request(request, base_url=self.base_url)
            self.checked_requests += 1
        except AssertionError as exc:
            self.violations.append(f"request: {exc}")

    def answered(self, request: httpx.Request, response: httpx.Response) -> None:
        raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        try:
            op, _ = find_operation(request.method, raw_path)
        except AssertionError:
            return  # not an operation: the request check already said so
        where = f"{op.method} {op.path}"
        status = response.status_code
        self.calls.append((where, status))
        with self._lock:
            self.consecutive_429 = self.consecutive_429 + 1 if status == 429 else 0
            if self.consecutive_429 >= MAX_429_IN_A_ROW:
                self.stopped = f"{where} answered 429 {self.consecutive_429} times in a row"
        documented = op.spec["responses"].get(str(status))
        if documented is None:
            self.violations.append(f"{where}: status {status} is not documented")
            return
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return  # an event stream is not read here: it ends when the client closes it
        media = documented.get("content", {}).get("application/json")
        if media is None:
            return
        try:
            body = response.json()
        except ValueError:
            self.violations.append(f"{where}: the {status} answer is not JSON")
            return
        try:
            validate(body, media["schema"], f"{where} answer {status}")
            self.checked_answers += 1
        except AssertionError as exc:
            self.violations.append(f"answer: {exc}")

    @contextmanager
    def invalid_request(self) -> Iterator[None]:
        """For a request that is meant to break the contract (to see the API refuse it)."""
        self.invalid_allowed += 1
        try:
            yield
        finally:
            self.invalid_allowed -= 1


def _is_stream(response: httpx.Response) -> bool:
    """An event stream never ends by itself: reading it whole would wait forever."""
    return response.status_code < 400 and response.headers.get("content-type", "").startswith(
        "text/event-stream"
    )


class LiveTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """One transport for the sync and the async client."""

    def __init__(
        self,
        ledger: Ledger,
        sync_inner: httpx.BaseTransport,
        async_inner: Callable[[], httpx.AsyncBaseTransport],
    ) -> None:
        self.ledger = ledger
        self._sync = sync_inner
        self._make_async = async_inner
        self._async = async_inner()

    def fork(self) -> LiveTransport:
        """Same ledger and connections, a fresh async connection pool.

        An async pool belongs to the event loop that opened its connections; each
        ``asyncio.run`` needs a pool of its own.
        """
        return LiveTransport(self.ledger, self._sync, self._make_async)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.ledger.guard_mail(request)
        wait = self.ledger.reserve()
        if wait:
            time.sleep(wait)
        self.ledger.check_request(request)
        response = self._sync.handle_request(request)
        if not _is_stream(response):
            response.read()
        self.ledger.answered(request, response)
        return response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.ledger.guard_mail(request)
        wait = self.ledger.reserve()
        if wait:
            await asyncio.sleep(wait)
        self.ledger.check_request(request)
        response = await self._async.handle_async_request(request)
        if not _is_stream(response):
            await response.aread()
        self.ledger.answered(request, response)
        return response
