"""The live suite's harness must notice a drifted API, or its green run means nothing.

These run offline: a fake API answers through the same transport the live run uses.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from e2e.harness import BudgetExceeded, Ledger, LiveTransport, RateLimitedTwice

from billionsend import BillionSend

from .contract import API_KEY, BASE_URL, OPERATIONS, example

USAGE = next(o for o in OPERATIONS if o.key == ("GET", "/usage"))


def usage_body() -> dict[str, Any]:
    body: dict[str, Any] = example(USAGE.success()[2])
    return body


def client(ledger: Ledger, handler: Any) -> BillionSend:
    inner = httpx.MockTransport(handler)
    transport = LiveTransport(ledger, inner, lambda: inner)
    return BillionSend(
        API_KEY,
        base_url=BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )


def ledger(**kw: Any) -> Ledger:
    return Ledger(base_url=BASE_URL, min_gap=0.0, **kw)


def test_a_valid_answer_passes_and_is_counted() -> None:
    led = ledger()
    client(led, lambda r: httpx.Response(200, json=usage_body())).usage.get()
    assert led.violations == []
    assert (led.requests, led.checked_requests, led.checked_answers) == (1, 1, 1)
    assert led.calls == [("GET /usage", 200)]


def test_an_answer_that_does_not_match_the_schema_is_a_violation() -> None:
    led = ledger()
    body = usage_body()
    del body["emails"]
    client(led, lambda r: httpx.Response(200, json=body)).usage.get()
    assert len(led.violations) == 1
    assert "does not match the contract" in led.violations[0]


def test_a_changed_type_is_a_violation() -> None:
    led = ledger()
    body = usage_body()
    body["emails"]["daily"]["used"] = "12"
    client(led, lambda r: httpx.Response(200, json=body)).usage.get()
    assert "does not match the contract" in led.violations[0]


def test_an_undocumented_status_is_a_violation() -> None:
    led = ledger()
    with pytest.raises(Exception, match="418"):
        client(led, lambda r: httpx.Response(418, json={})).usage.get()
    assert led.violations == ["GET /usage: status 418 is not documented"]


def test_a_documented_error_must_have_the_error_shape() -> None:
    led = ledger()
    item = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
    good = {"statusCode": 404, "name": "not_found", "message": "boom"}
    with pytest.raises(Exception, match="boom"):
        client(led, lambda r: httpx.Response(404, json=good)).emails.get(item)
    assert led.violations == []
    with pytest.raises(Exception, match="Not Found"):
        client(led, lambda r: httpx.Response(404, json={"error": "x"})).emails.get(item)
    assert len(led.violations) == 1


def test_an_answer_that_is_not_json_is_a_violation() -> None:
    led = ledger()
    with pytest.raises(Exception, match="JSON"):
        client(led, lambda r: httpx.Response(200, content=b"<html>")).usage.get()
    assert led.violations == ["GET /usage: the 200 answer is not JSON"]


def test_a_request_outside_the_contract_is_a_violation_not_an_exception() -> None:
    led = ledger()
    c = client(
        led, lambda r: httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
    )
    c.request("GET", "/domains", query={"page": 2})
    assert len(led.violations) == 1
    assert "undeclared query parameters" in led.violations[0]


def test_a_deliberately_invalid_request_is_not_held_to_the_contract() -> None:
    led = ledger()
    c = client(
        led, lambda r: httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
    )
    with led.invalid_request():
        c.request("GET", "/domains", query={"page": 2})
    assert led.violations == []


def test_two_429s_in_a_row_stop_the_run_until_cleanup() -> None:
    led = ledger()
    body = {"statusCode": 429, "name": "rate_limit_exceeded", "message": "slow"}
    c = client(led, lambda r: httpx.Response(429, json=body, headers={"retry-after": "1"}))
    for _ in range(2):
        with pytest.raises(Exception, match="slow"):
            c.usage.get()
    assert led.stopped
    with pytest.raises(RateLimitedTwice):
        c.usage.get()
    led.in_cleanup = True
    with pytest.raises(Exception, match="slow"):
        c.usage.get()


def test_a_429_followed_by_an_answer_does_not_stop_the_run() -> None:
    led = ledger()
    answers = iter(
        [
            httpx.Response(429, json={"statusCode": 429, "name": "n", "message": "m"}),
            httpx.Response(200, json=usage_body()),
            httpx.Response(429, json={"statusCode": 429, "name": "n", "message": "m"}),
        ]
    )
    c = client(led, lambda r: next(answers))
    with pytest.raises(Exception, match="m"):
        c.usage.get()
    c.usage.get()
    with pytest.raises(Exception, match="m"):
        c.usage.get()
    assert led.stopped is None


def test_the_request_budget_holds_and_cleanup_has_a_reserve() -> None:
    led = ledger(test_budget=2)
    c = client(led, lambda r: httpx.Response(200, json=usage_body()))
    c.usage.get()
    c.usage.get()
    with pytest.raises(BudgetExceeded):
        c.usage.get()
    led.in_cleanup = True
    c.usage.get()
    assert led.requests == 3


def test_requests_are_spaced() -> None:
    led = Ledger(base_url=BASE_URL, min_gap=0.25)
    c = client(led, lambda r: httpx.Response(200, json=usage_body()))
    started = time.monotonic()
    for _ in range(3):
        c.usage.get()
    assert time.monotonic() - started >= 0.5


async def test_the_async_path_is_checked_too() -> None:
    led = ledger()
    body = usage_body()
    del body["emails"]
    inner = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    transport = LiveTransport(led, inner, lambda: inner).fork()
    from billionsend import AsyncBillionSend

    http = httpx.AsyncClient(transport=transport)
    await AsyncBillionSend(API_KEY, base_url=BASE_URL, http_client=http).usage.get()
    await http.aclose()
    assert len(led.violations) == 1
    assert led.requests == 1
