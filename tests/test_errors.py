"""Error answers become exceptions carrying the API's error shape."""

from __future__ import annotations

import httpx
import pytest

from billionsend import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncBillionSend,
    AuthenticationError,
    BillionSend,
    BillionSendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)

from .contract import API_KEY, BASE_URL, ContractMock

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"


@pytest.mark.parametrize(
    ("status", "name", "cls"),
    [
        (400, "invalid_idempotency_key", ValidationError),
        (401, "missing_api_key", AuthenticationError),
        (403, "restricted_api_key", PermissionDeniedError),
        (404, "not_found", NotFoundError),
        (409, "invalid_idempotent_request", ConflictError),
        (422, "validation_error", ValidationError),
        (429, "rate_limit_exceeded", RateLimitError),
        (500, "internal_server_error", APIError),
        (503, "service_unavailable", APIError),
        (418, "teapot", BillionSendError),
    ],
)
def test_status_maps_to_exception(
    client: BillionSend, mock: ContractMock, status: int, name: str, cls: type[BillionSendError]
) -> None:
    client.max_retries = 0
    mock.error(
        "POST", "/emails", status, name, f"refused with {status}", headers={"x-request-id": "r1"}
    )
    with pytest.raises(cls) as info:
        client.emails.send(
            {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"}
        )
    err = info.value
    assert type(err) is cls
    assert isinstance(err, BillionSendError)
    assert (err.status_code, err.name, err.message) == (status, name, f"refused with {status}")
    assert err.response is not None
    assert err.request_id == "r1"
    assert str(err) == f"{status} {name}: refused with {status}"


def test_rate_limit_carries_retry_after(client: BillionSend, mock: ContractMock) -> None:
    mock.error(
        "POST", "/domains", 429, "rate_limit_exceeded", "slow down", headers={"retry-after": "7"}
    )
    with pytest.raises(RateLimitError) as info:
        client.domains.create({"name": "example.com"})
    assert info.value.retry_after == 7.0


def test_non_json_error_keeps_the_shape(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "GET",
        "/emails/{id}",
        502,
        content=b"<html>Bad gateway</html>",
        headers={"content-type": "text/html"},
    )
    client.max_retries = 0
    with pytest.raises(APIError) as info:
        client.emails.get(ID)
    assert info.value.status_code == 502
    assert info.value.name == "application_error"
    assert info.value.message == "<html>Bad gateway</html>"


def test_empty_error_body(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", "/emails/{id}", 404, content=b"")
    with pytest.raises(NotFoundError) as info:
        client.emails.get(ID)
    assert info.value.name == "application_error"
    assert info.value.message == "Not Found"


def test_non_json_success_is_an_error(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", "/usage", 200, content=b"ok", headers={"content-type": "text/plain"})
    with pytest.raises(BillionSendError) as info:
        client.usage.get()
    assert info.value.name == "application_error"
    assert info.value.status_code == 200


def _failing_client(exc: Exception) -> BillionSend:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return BillionSend(
        API_KEY, base_url=BASE_URL, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_network_failure() -> None:
    client = _failing_client(httpx.ConnectError("connection refused"))
    with pytest.raises(APIConnectionError) as info:
        client.usage.get()
    assert info.value.status_code is None
    assert info.value.name == "application_error"
    assert "connection refused" in info.value.message


def test_timeout() -> None:
    client = _failing_client(httpx.ReadTimeout("timed out"))
    with pytest.raises(APITimeoutError) as info:
        client.usage.get()
    assert isinstance(info.value, APIConnectionError)
    assert info.value.name == "application_error"


def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILLIONSEND_API_KEY", raising=False)
    mock = ContractMock()
    client = BillionSend(base_url=BASE_URL, http_client=httpx.Client(transport=mock.transport()))
    with pytest.raises(AuthenticationError) as info:
        client.usage.get()
    assert info.value.name == "missing_api_key"
    assert info.value.status_code is None  # no answer came: nothing was sent
    assert mock.calls == []


async def test_async_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILLIONSEND_API_KEY", raising=False)
    client = AsyncBillionSend(base_url=BASE_URL)
    with pytest.raises(AuthenticationError):
        await client.usage.get()
    with pytest.raises(AuthenticationError):
        await client.inboxes.events.stream()
    await client.close()


async def test_async_error(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.error("GET", "/inboxes/{inbox_id}", 404, "not_found", "Inbox not found")
    with pytest.raises(NotFoundError) as info:
        await async_client.inboxes.get("nobody@example.com")
    assert info.value.message == "Inbox not found"


async def test_async_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    client = AsyncBillionSend(
        API_KEY,
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(APIConnectionError):
        await client.usage.get()
