"""Auto-paging over cursor lists."""

from __future__ import annotations

from typing import Any

from billionsend import AsyncBillionSend, BillionSend

from .contract import OPERATIONS, ContractMock, example

IDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 8)]


def page(mock: ContractMock, path: str, ids: list[str], has_more: bool) -> None:
    op = next(o for o in OPERATIONS if o.key == ("GET", path))
    _, _, schema = op.success()
    body: dict[str, Any] = example(schema)
    template = body["data"][0]
    body["data"] = [{**template, "id": i} for i in ids]
    body["has_more"] = has_more
    mock.respond("GET", path, json_body=body)


def test_forward_over_pages(client: BillionSend, mock: ContractMock) -> None:
    page(mock, "/domains", IDS[0:3], True)
    page(mock, "/domains", IDS[3:6], True)
    page(mock, "/domains", IDS[6:7], False)
    seen = [d["id"] for d in client.domains.list_all(limit=3)]
    assert seen == IDS
    assert [c.query for c in mock.calls] == [
        {"limit": ["3"]},
        {"limit": ["3"], "after": [IDS[2]]},
        {"limit": ["3"], "after": [IDS[5]]},
    ]


def test_backward_from_before(client: BillionSend, mock: ContractMock) -> None:
    page(mock, "/emails", IDS[4:6], True)
    page(mock, "/emails", IDS[2:4], False)
    seen = [e["id"] for e in client.emails.list_all(before=IDS[6])]
    assert seen == IDS[4:6] + IDS[2:4]
    assert mock.calls[1].query == {"before": [IDS[4]]}


def test_stops_lazily(client: BillionSend, mock: ContractMock) -> None:
    page(mock, "/contacts", IDS[0:2], True)
    iterator = client.contacts.list_all()
    assert next(iterator)["id"] == IDS[0]
    assert len(mock.calls) == 1


def test_empty_page_with_has_more_stops(client: BillionSend, mock: ContractMock) -> None:
    page(mock, "/templates", IDS[0:1], True)
    body = example(next(o for o in OPERATIONS if o.key == ("GET", "/templates")).success()[2])
    body["data"] = []
    body["has_more"] = True
    mock.respond("GET", "/templates", json_body=body)
    assert [t["id"] for t in client.templates.list_all()] == IDS[0:1]
    assert len(mock.calls) == 2


def test_nested_list_keeps_path_and_filters(client: BillionSend, mock: ContractMock) -> None:
    page(mock, "/inboxes/{inbox_id}/threads", IDS[0:2], True)
    page(mock, "/inboxes/{inbox_id}/threads", IDS[2:3], False)
    threads = list(client.inboxes.threads.list_all("support@example.com", labels="vip", limit=2))
    assert [t["id"] for t in threads] == IDS[0:3]
    assert mock.calls[1].query == {"limit": ["2"], "after": [IDS[1]], "labels": ["vip"]}
    assert mock.calls[1].path_params == {"inbox_id": "support@example.com"}


def test_every_cursor_list_has_a_paginator() -> None:
    client = BillionSend("bs_x")
    assert callable(client.inboxes.threads.search_all)
    assert callable(client.emails.receiving.list_all)
    assert callable(client.inboxes.drafts.list_all)
    assert not hasattr(client.topics, "list_all")  # not a cursor list in the contract


async def test_async_pages(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    page(mock, "/webhooks", IDS[0:2], True)
    page(mock, "/webhooks", IDS[2:4], False)
    seen = [w["id"] async for w in async_client.webhooks.list_all(limit=2)]
    assert seen == IDS[0:4]
    assert mock.calls[1].query == {"limit": ["2"], "after": [IDS[1]]}


async def test_async_backward(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    page(mock, "/suppressions", IDS[3:5], True)
    page(mock, "/suppressions", IDS[1:3], False)
    seen = [s["id"] async for s in async_client.suppressions.list_all(before=IDS[5])]
    assert seen == IDS[3:5] + IDS[1:3]
    assert mock.calls[1].query == {"before": [IDS[3]]}


def test_thread_emails_of_either_direction(client: BillionSend, mock: ContractMock) -> None:
    path = "/inboxes/{inbox_id}/threads/{thread_id}/emails"
    page(mock, path, IDS[0:2], True)
    page(mock, path, IDS[2:3], False)
    seen = [e["id"] for e in client.inboxes.threads.emails.list_all("in_1", "th_1", limit=2)]
    assert seen == IDS[0:3]
    assert mock.calls[1].query == {"limit": ["2"], "after": [IDS[1]]}
