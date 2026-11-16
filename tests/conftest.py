from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from billionsend import AsyncBillionSend, BillionSend

from .contract import API_KEY, BASE_URL, ContractMock


class Sleeps(list[float]):
    """Delays the client would have slept, recorded instead of slept."""


@pytest.fixture
def mock() -> ContractMock:
    return ContractMock()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def client(mock: ContractMock, sleeps: Sleeps) -> Iterator[BillionSend]:
    http = httpx.Client(transport=mock.transport())
    c = BillionSend(API_KEY, base_url=BASE_URL, http_client=http)
    c._sleep = sleeps.append
    yield c
    http.close()


@pytest.fixture
async def async_client(mock: ContractMock, sleeps: Sleeps) -> AsyncIterator[AsyncBillionSend]:
    http = httpx.AsyncClient(transport=mock.transport())
    c = AsyncBillionSend(API_KEY, base_url=BASE_URL, http_client=http)

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    c._sleep = record  # type: ignore[method-assign]
    yield c
    await http.aclose()
