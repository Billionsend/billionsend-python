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
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
from tests.contract import check_request, find_operation, validate

BASE_URL = "https://api.billionsend.com"
MAX_REQUESTS = 150
# The tests may use this many; what is left is kept for cleanup and the final check.
TEST_BUDGET = 116
MIN_GAP = 1.05
MAX_429_IN_A_ROW = 2


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
    _last: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

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
        wait = self.ledger.reserve()
        if wait:
            time.sleep(wait)
        self.ledger.check_request(request)
        response = self._sync.handle_request(request)
        response.read()
        self.ledger.answered(request, response)
        return response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        wait = self.ledger.reserve()
        if wait:
            await asyncio.sleep(wait)
        self.ledger.check_request(request)
        response = await self._async.handle_async_request(request)
        await response.aread()
        self.ledger.answered(request, response)
        return response
