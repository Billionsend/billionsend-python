"""Retries and idempotency keys.

Retried: 429, 5xx and failures without an answer, for idempotent methods and
requests carrying an idempotency key (sending operations always carry one),
plus 409 concurrent_idempotent_requests for keyed requests.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from billionsend import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncBillionSend,
    BillionSend,
    ConflictError,
    RateLimitError,
)
from billionsend.types import SendEmailRequest

from .conftest import Sleeps
from .contract import ContractMock

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
SEND: SendEmailRequest = {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"}


def test_get_is_retried_on_5xx(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error("GET", "/emails/{id}", 503, "service_unavailable", "busy")
    mock.error("GET", "/emails/{id}", 500, "internal_server_error", "oops")
    result = client.emails.get(ID)
    assert result["object"] == "email"
    assert len(mock.calls) == 3
    assert len(sleeps) == 2
    assert 0 < sleeps[0] <= 0.5
    assert 0 < sleeps[1] <= 1.0


def test_retry_after_is_respected(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error(
        "DELETE", "/domains/{id}", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "3"}
    )
    client.domains.delete(ID)
    assert sleeps == [3.0]


def test_retry_after_http_date(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error(
        "GET",
        "/usage",
        429,
        "rate_limit_exceeded",
        "slow",
        headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )
    client.usage.get()
    assert sleeps == [0.0]


def test_retries_are_bounded(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    for _ in range(3):
        mock.error("GET", "/usage", 500, "internal_server_error", "down")
    with pytest.raises(APIError):
        client.usage.get()
    assert len(mock.calls) == 3  # one try and the default two retries
    assert len(sleeps) == 2


def test_post_without_idempotency_key_is_not_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("POST", "/domains", 503, "service_unavailable", "busy")
    with pytest.raises(APIError):
        client.domains.create({"name": "example.com"})
    assert len(mock.calls) == 1
    assert "idempotency-key" not in mock.last.request.headers
    assert sleeps == []


def test_send_makes_up_a_key_shared_by_its_retries(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("POST", "/emails", 503, "service_unavailable", "busy")
    client.emails.send(SEND)
    client.emails.send(SEND)
    keys = [c.request.headers["idempotency-key"] for c in mock.calls]
    assert len(keys) == 3
    assert keys[0] == keys[1] != keys[2]
    assert uuid.UUID(keys[0]).version == 4
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        ("POST", "/emails/batch", lambda c: c.emails.batch([SEND])),
        (
            "POST",
            "/emails/receiving/{id}/reply",
            lambda c: c.emails.receiving.reply(ID, {"text": "t"}),
        ),
        (
            "POST",
            "/emails/receiving/{id}/forward",
            lambda c: c.emails.receiving.forward(ID, {"to": "x@example.com"}),
        ),
        (
            "POST",
            "/inboxes/{inbox_id}/emails",
            lambda c: c.inboxes.send("in_1", {"to": "x@example.com", "subject": "s", "text": "t"}),
        ),
        (
            "POST",
            "/inboxes/{inbox_id}/drafts/{draft_id}/send",
            lambda c: c.inboxes.drafts.send("in_1", ID),
        ),
        (
            "POST",
            "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}/reply",
            lambda c: c.inboxes.threads.emails.reply("in_1", ID, ID, {"text": "t"}),
        ),
        (
            "POST",
            "/inboxes/{inbox_id}/threads/{thread_id}/emails/{email_id}/forward",
            lambda c: c.inboxes.threads.emails.forward("in_1", ID, ID, {"to": "x@example.com"}),
        ),
    ],
)
def test_every_sending_operation_carries_a_key(
    client: BillionSend, mock: ContractMock, method: str, path: str, call: Any
) -> None:
    mock.fail(method, path, httpx.ConnectError("refused"))
    call(client)
    keys = [c.request.headers["idempotency-key"] for c in mock.calls]
    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_one_key_header_whatever_its_spelling(client: BillionSend, mock: ContractMock) -> None:
    client.emails.send(SEND, idempotency_key="a", extra_headers={"IDEMPOTENCY-KEY": "b"})
    assert mock.last.request.headers.get_list("idempotency-key") == ["b"]


def test_network_failure_of_a_keyed_send_is_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.fail("POST", "/emails", httpx.ReadTimeout("slow"))
    mock.fail("POST", "/emails", httpx.ConnectError("refused"))
    client.emails.send(SEND, idempotency_key="k")
    assert [c.request.headers["idempotency-key"] for c in mock.calls] == ["k", "k", "k"]
    assert len(sleeps) == 2


def test_network_failure_of_a_get_is_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.fail("GET", "/usage", httpx.ConnectError("refused"))
    client.usage.get()
    assert len(mock.calls) == 2


def test_network_failure_of_an_unkeyed_post_is_raised(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.fail("POST", "/domains", httpx.ReadTimeout("slow"))
    with pytest.raises(APITimeoutError) as info:
        client.domains.create({"name": "example.com"})
    assert isinstance(info.value, APIConnectionError)
    assert info.value.status_code is None
    assert info.value.request_id is None
    assert len(mock.calls) == 1
    assert sleeps == []


def test_network_failures_are_bounded(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    for _ in range(3):
        mock.fail("GET", "/usage", httpx.ConnectError("refused"))
    with pytest.raises(APIConnectionError, match="refused"):
        client.usage.get()
    assert len(mock.calls) == 3


def test_concurrent_idempotent_request_is_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error(
        "POST",
        "/emails",
        409,
        "concurrent_idempotent_requests",
        "A request with this idempotency key is still processing",
    )
    client.emails.send(SEND)
    assert len(mock.calls) == 2
    assert len(sleeps) == 1


def test_other_conflicts_are_not_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error(
        "POST",
        "/emails",
        409,
        "invalid_idempotent_request",
        "Same idempotency key used with a different request payload",
    )
    with pytest.raises(ConflictError):
        client.emails.send(SEND, idempotency_key="k")
    assert len(mock.calls) == 1


def test_patch_is_not_retried(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error("PATCH", "/domains/{id}", 500, "internal_server_error", "oops")
    with pytest.raises(APIError):
        client.domains.update(ID, {"click_tracking": True})
    assert len(mock.calls) == 1


def test_post_with_idempotency_key_is_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("POST", "/emails", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "1"})
    result = client.emails.send(SEND, idempotency_key="once")
    assert result["id"]
    assert [c.request.headers["idempotency-key"] for c in mock.calls] == ["once", "once"]
    assert sleeps == [1.0]


def test_idempotency_key_via_extra_headers_also_retries(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("POST", "/domains", 502, "bad_gateway", "upstream")
    client.domains.create({"name": "example.com"}, extra_headers={"idempotency-key": "d1"})
    assert len(mock.calls) == 2


def test_4xx_is_never_retried(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error("GET", "/emails/{id}", 404, "not_found", "gone")
    with pytest.raises(Exception, match="gone"):
        client.emails.get(ID)
    assert len(mock.calls) == 1


def test_max_retries_zero(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    client.max_retries = 0
    mock.error("GET", "/usage", 429, "rate_limit_exceeded", "slow")
    with pytest.raises(RateLimitError):
        client.usage.get()
    assert len(mock.calls) == 1


def test_retry_after_over_a_minute_is_raised_at_once(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error(
        "GET",
        "/usage",
        429,
        "daily_quota_exceeded",
        "quota",
        headers={"retry-after": "3600", "x-request-id": "req_9"},
    )
    with pytest.raises(RateLimitError) as info:
        client.usage.get()
    assert info.value.retry_after == 3600.0
    assert info.value.request_id == "req_9"
    assert len(mock.calls) == 1
    assert sleeps == []


def test_retry_after_of_a_minute_is_waited(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("GET", "/usage", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "60"})
    client.usage.get()
    assert sleeps == [60.0]


def test_negative_max_retries_is_refused() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        BillionSend("bs_x", max_retries=-1)


async def test_async_retry(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error(
        "GET", "/deliverability", 503, "service_unavailable", "busy", headers={"retry-after": "2"}
    )
    await async_client.deliverability.get()
    assert len(mock.calls) == 2
    assert sleeps == [2.0]


async def test_async_post_not_retried(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("POST", "/broadcasts/{id}/send", 500, "internal_server_error", "oops")
    with pytest.raises(APIError):
        await async_client.broadcasts.send(ID, {})
    assert len(mock.calls) == 1


async def test_async_send_key_and_network_retry(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.fail("POST", "/emails", httpx.ConnectError("refused"))
    mock.error("POST", "/emails", 409, "concurrent_idempotent_requests", "still processing")
    await async_client.emails.send(SEND)
    keys = {c.request.headers["idempotency-key"] for c in mock.calls}
    assert len(mock.calls) == 3
    assert len(keys) == 1
    assert len(sleeps) == 2


async def test_async_unkeyed_post_network_failure(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    mock.fail("POST", "/domains", httpx.ConnectError("refused"))
    with pytest.raises(APIConnectionError):
        await async_client.domains.create({"name": "example.com"})
    assert len(mock.calls) == 1
