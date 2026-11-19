"""The live suite's harness must notice a drifted API, or its green run means nothing.

These run offline: a fake API answers through the same transport the live run uses.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from e2e.harness import (
    SUPPRESSING_CALLS,
    BudgetExceeded,
    Ledger,
    LiveTransport,
    RateLimitedTwice,
    UnsafeMail,
)

from billionsend import BillionSend

from .contract import API_KEY, BASE_URL, OPERATIONS, ContractMock, example

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


def test_an_event_stream_is_not_read_to_its_end() -> None:
    """A stream never ends by itself: the transport hands it on unread, and the call is
    counted and its status checked like any other."""
    ended: list[bool] = []

    class Long(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"id: 1\n\n"
            for _ in range(30):  # stands for "forever": reading it all takes 0.6 s
                time.sleep(0.02)
                yield b": keepalive\n\n"
            ended.append(True)

    led = ledger()
    c = client(
        led,
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Long()),
    )
    stream = c.inboxes.events.stream(max_reconnects=0)
    assert ended == []  # opening it did not read it
    stream.close()
    assert (led.requests, led.violations) == (1, [])
    assert led.calls == [("GET /inboxes/events", 200)]


def test_a_refused_event_stream_is_still_read_and_checked() -> None:
    led = ledger()
    body = {"statusCode": 429, "name": "rate_limit_exceeded", "message": "cap"}
    c = client(led, lambda r: httpx.Response(429, json=body))
    with pytest.raises(Exception, match="cap"):
        c.inboxes.events.stream(max_reconnects=0)
    assert led.violations == []


# ------------------------------------------------------------------ who may be mailed

SIM = "simulator.amazonses.com"


def mail(**fields: Any) -> Any:
    return {"from": "SDK E2E <sdk-e2e@mail.billionsend.dev>", "subject": "s", "text": "t", **fields}


def mailing_client(led: Ledger) -> tuple[BillionSend, ContractMock]:
    mock = ContractMock()
    inner = httpx.MockTransport(mock.handle)
    transport = LiveTransport(led, inner, lambda: inner)
    c = BillionSend(
        API_KEY, base_url=BASE_URL, max_retries=0, http_client=httpx.Client(transport=transport)
    )
    return c, mock


@pytest.mark.parametrize(
    "to",
    [
        f"success@{SIM}",
        f"success+sdk-e2e-ab12-mail@{SIM}",
        f"SUCCESS+Tag@{SIM.upper()}",
        f"SDK <success+x@{SIM}>",
        [f"success@{SIM}", f"success+2@{SIM}"],
    ],
)
def test_the_simulator_and_its_plus_aliases_may_be_mailed(to: Any) -> None:
    led = ledger()
    c, mock = mailing_client(led)
    c.emails.send(mail(to=to))
    assert len(mock.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        f"bounce@{SIM}",
        f"complaint@{SIM}",
        f"suppressionlist@{SIM}",
        f"ooto@{SIM}",
        f"bounce+x@{SIM}",
        f"success+@{SIM}",
        f"xsuccess@{SIM}",
        f"success@{SIM}.example.com",
        f"success@mail.{SIM}",
        "success@example.com",
        "ada@example.com",
        f"Success <ada@example.com> <success@{SIM}>",
        "no-at-sign-is-not-a-recipient-but-the-next-one-is@example.com",
    ],
)
@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_any_other_recipient_is_refused_before_the_request(bad: str, field: str) -> None:
    led = ledger()
    c, mock = mailing_client(led)
    fields: dict[str, Any] = {"to": [f"success@{SIM}"], field: [bad]}
    with pytest.raises(UnsafeMail, match="not the simulator"):
        c.emails.send(mail(**fields))
    assert mock.calls == []
    assert led.requests == 0  # not even counted


def test_a_bad_recipient_in_a_batch_or_a_string_to_is_refused() -> None:
    led = ledger()
    c, mock = mailing_client(led)
    good = mail(to=[f"success@{SIM}"])
    with pytest.raises(UnsafeMail):
        c.emails.batch([good, mail(to=[f"success@{SIM}"], bcc=["ada@example.com"])])
    with pytest.raises(UnsafeMail):
        c.emails.send(mail(to="ada@example.com"))
    assert mock.calls == []


def test_nothing_switches_the_recipient_rule_off() -> None:
    led = ledger()
    c, mock = mailing_client(led)
    with led.invalid_request(), pytest.raises(UnsafeMail):
        c.emails.send(mail(to=[f"bounce@{SIM}"]))
    led.in_cleanup = True
    with pytest.raises(UnsafeMail):
        c.emails.send(mail(to=["ada@example.com"]))
    assert mock.calls == []


async def test_the_async_path_refuses_too() -> None:
    from billionsend import AsyncBillionSend

    led = ledger()
    mock = ContractMock()
    inner = httpx.MockTransport(mock.handle)
    transport = LiveTransport(led, inner, lambda: inner).fork()
    http = httpx.AsyncClient(transport=transport)
    c = AsyncBillionSend(API_KEY, base_url=BASE_URL, max_retries=0, http_client=http)
    with pytest.raises(UnsafeMail):
        await c.emails.send(mail(to=["ada@example.com"]))
    await c.emails.send(mail(to=[f"success@{SIM}"]))
    await http.aclose()
    assert len(mock.calls) == 1


def test_a_step_named_to_in_an_automation_is_not_a_recipient() -> None:
    led = ledger()
    c, mock = mailing_client(led)
    c.request(
        "POST",
        "/automations",
        body={"name": "n", "steps": [], "connections": [{"from": "start", "to": "pause"}]},
    )
    assert len(mock.calls) == 1


def test_an_address_this_run_suppressed_is_never_mailed_again() -> None:
    led = ledger()
    c, mock = mailing_client(led)
    one, two = f"success+sdk-e2e-1-s1@{SIM}", f"success+sdk-e2e-1-s2@{SIM}"
    c.suppressions.create({"email": one, "origin": "manual"})
    c.suppressions.batch.add({"emails": [two], "origin": "manual"})
    sent = len(mock.calls)
    for suppressed in (one, one.upper(), two, f"X <{two}>"):
        with pytest.raises(UnsafeMail, match="suppressed"):
            c.emails.send(mail(to=[f"success@{SIM}"], cc=[suppressed]))
    c.emails.send(mail(to=[f"success+sdk-e2e-1-other@{SIM}"]))
    assert len(mock.calls) == sent + 1


def test_a_domain_this_run_suppressed_is_never_mailed_again() -> None:
    led = ledger()
    c, mock = mailing_client(led)
    c.suppressions.create({"domain": SIM, "origin": "manual"})
    sent = len(mock.calls)
    with pytest.raises(UnsafeMail, match="suppressed"):
        c.emails.send(mail(to=[f"success@{SIM}"]))
    assert len(mock.calls) == sent
    other, other_mock = mailing_client(ledger())
    other.suppressions.batch.add({"domains": [SIM.upper()], "origin": "manual"})
    with pytest.raises(UnsafeMail, match="suppressed"):
        other.emails.send(mail(to=[f"success+sdk-e2e-1-x@{SIM}"]))
    assert [c.operation.path for c in other_mock.calls] == ["/suppressions/batch/add"]


def test_removing_a_suppression_does_not_make_the_address_mailable() -> None:
    led = ledger()
    c, _mock = mailing_client(led)
    one = f"success+sdk-e2e-1-s1@{SIM}"
    c.suppressions.batch.add({"emails": [one], "origin": "manual"})
    c.suppressions.batch.remove({"emails": [one]})
    with pytest.raises(UnsafeMail, match="suppressed"):
        c.emails.send(mail(to=[one]))


def test_a_suppressed_address_is_refused_in_replies_and_drafts_too() -> None:
    led = ledger()
    c, _mock = mailing_client(led)
    one = f"success+sdk-e2e-1-s1@{SIM}"
    c.suppressions.create({"email": one, "origin": "manual"})
    with pytest.raises(UnsafeMail):
        c.inboxes.send("a@example.com", mail(to=[one]))
    with pytest.raises(UnsafeMail):
        c.inboxes.drafts.create("a@example.com", {"to": [one], "subject": "s"})


def test_the_harness_knows_every_call_that_suppresses_an_address() -> None:
    """A new way to add to the suppression list in the API must be taught to the harness."""
    adding = {
        path
        for key_path, op in ((o.path, o) for o in OPERATIONS)
        for path in [key_path]
        if "suppress" in path and op.method == "POST" and "remove" not in path
    }
    assert adding == set(SUPPRESSING_CALLS)
