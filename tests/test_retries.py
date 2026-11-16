"""Retries: only 429 / 5xx, only for idempotent methods or requests with an idempotency key."""

from __future__ import annotations

import pytest

from billionsend import APIError, AsyncBillionSend, BillionSend, RateLimitError
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
    mock.error("POST", "/emails", 503, "service_unavailable", "busy")
    with pytest.raises(APIError):
        client.emails.send(SEND)
    assert len(mock.calls) == 1
    assert sleeps == []


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
    client.domains.create({"name": "example.com"}, extra_headers={"Idempotency-Key": "d1"})
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


def test_retry_delay_is_capped(client: BillionSend, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error("GET", "/usage", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "3600"})
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
