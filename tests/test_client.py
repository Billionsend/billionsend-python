"""Client configuration, lifecycle and the contract mock itself."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from billionsend import AsyncBillionSend, BillionSend

from .contract import API_KEY, BASE_URL, SNAPSHOT, ContractMock, find_operation

ROOT = Path(__file__).resolve().parent.parent


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILLIONSEND_API_KEY", raising=False)
    monkeypatch.delenv("BILLIONSEND_BASE_URL", raising=False)
    client = BillionSend()
    assert client.api_key is None
    assert client.base_url == "https://api.billionsend.com"
    assert client.max_retries == 2
    assert client.timeout == 60.0


def test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLIONSEND_API_KEY", "bs_env")
    monkeypatch.setenv("BILLIONSEND_BASE_URL", "https://api.example.test/v/")
    client = BillionSend()
    assert client.api_key == "bs_env"
    assert client.base_url == "https://api.example.test/v"


def test_arguments_win_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLIONSEND_API_KEY", "bs_env")
    monkeypatch.setenv("BILLIONSEND_BASE_URL", "https://env.example.test")
    client = AsyncBillionSend(
        "bs_arg", base_url="https://arg.example.test", timeout=5, max_retries=0
    )
    assert (client.api_key, client.base_url, client.timeout, client.max_retries) == (
        "bs_arg",
        "https://arg.example.test",
        5,
        0,
    )


def test_base_url_with_a_path_prefix() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={})

    client = BillionSend(
        "bs_x",
        base_url="https://gw.example.test/billionsend/",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.usage.get()
    assert str(seen[0]) == "https://gw.example.test/billionsend/usage"


def test_default_headers(mock: ContractMock) -> None:
    client = BillionSend(
        API_KEY,
        base_url=BASE_URL,
        default_headers={"X-Team": "t1"},
        http_client=httpx.Client(transport=mock.transport()),
    )
    client.usage.get()
    assert mock.last.request.headers["x-team"] == "t1"


def test_context_manager_closes_its_own_client() -> None:
    with BillionSend("bs_x") as client:
        http = client._http
    assert http.is_closed


def test_injected_client_is_left_open() -> None:
    http = httpx.Client()
    with BillionSend("bs_x", http_client=http):
        pass
    assert not http.is_closed
    http.close()


async def test_async_context_manager() -> None:
    async with AsyncBillionSend("bs_x") as client:
        http = client._http
    assert http.is_closed


def test_timeout_object_is_passed_through(mock: ContractMock) -> None:
    client = BillionSend(
        API_KEY,
        base_url=BASE_URL,
        timeout=httpx.Timeout(2.0, connect=1.0),
        http_client=httpx.Client(transport=mock.transport()),
    )
    client.usage.get()
    assert mock.last.request.extensions["timeout"]["connect"] == 1.0
    assert mock.last.request.extensions["timeout"]["read"] == 2.0


def test_streams_get_a_longer_read_timeout(client: BillionSend, mock: ContractMock) -> None:
    client.timeout = 10.0
    with client.inboxes.events.stream(max_reconnects=0) as stream:
        list(stream)
    assert mock.last.request.extensions["timeout"]["read"] == 60.0
    assert mock.last.request.extensions["timeout"]["connect"] == 10.0


# ------------------------------------------------------------ the contract mock


def test_mock_rejects_unknown_operations() -> None:
    with pytest.raises(AssertionError, match="no such path"):
        find_operation("GET", "/nope")
    with pytest.raises(AssertionError, match="method not allowed"):
        find_operation("PUT", "/emails")


def test_mock_prefers_literal_segments() -> None:
    assert find_operation("GET", "/emails/metrics")[0].path == "/emails/metrics"
    assert find_operation("GET", "/inboxes/events")[0].path == "/inboxes/events"
    assert find_operation("POST", "/contacts/batch/get")[0].path == "/contacts/batch/get"


def test_mock_rejects_invalid_requests(client: BillionSend) -> None:
    with pytest.raises(AssertionError, match="request body"):
        client.domains.create({})  # type: ignore[typeddict-item]
    with pytest.raises(AssertionError, match="undeclared query"):
        client.request("GET", "/domains", query={"page": 2})
    with pytest.raises(AssertionError, match="query limit"):
        client.request("GET", "/domains", query={"limit": 1000})
    with pytest.raises(AssertionError, match="Authorization"):
        client.request("GET", "/usage", auth=False)
    with pytest.raises(AssertionError, match="without an API key"):
        client.request("POST", "/agent/signup", body={"human_email": "a@b.c", "name": "n"})


# ------------------------------------------------------------ repository hygiene


def test_snapshot_names_only_billionsend() -> None:
    pattern = re.compile("|".join(["re" + "send", "sv" + "ix"]), re.IGNORECASE)
    tracked = [
        SNAPSHOT,
        *ROOT.glob("src/**/*.py"),
        *ROOT.glob("tests/*.py"),
        *ROOT.glob("scripts/*.py"),
    ]
    tracked += [ROOT / n for n in ("README.md", "CHANGELOG.md", "pyproject.toml")]
    offenders = [
        str(p.relative_to(ROOT))
        for p in tracked
        if p.exists() and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
