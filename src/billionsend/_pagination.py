"""Iterate over every item of a cursor-paginated list.

Lists are newest first: ``after`` takes the last id of a page and pages toward
older items, ``before`` takes the first id and pages toward newer ones.

A page without a ``data`` array raises ``BillionSendError`` (``application_error``)
at once. A page that has more after it but whose edge item (the last one
forward, the first one backward) has no non-empty string ``id`` hands out its
items, then raises, since there is no cursor to continue from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any

from ._exceptions import BillionSendError

__all__ = ["apaginate", "paginate"]

Fetch = Callable[[str | None, str | None], object]
AsyncFetch = Callable[[str | None, str | None], Awaitable[object]]


def _items(page: object) -> tuple[list[Any], bool]:
    data = page.get("data") if isinstance(page, Mapping) else None
    if not isinstance(data, list):
        raise BillionSendError("The list page has no data array")
    assert isinstance(page, Mapping)
    return data, page.get("has_more") is True


def _cursor(items: list[Any], backward: bool) -> str:
    edge = items[0] if backward else items[-1]
    cursor = edge.get("id") if isinstance(edge, Mapping) else None
    if not isinstance(cursor, str) or not cursor:
        side = "first" if backward else "last"
        raise BillionSendError(f"The {side} item of the list page has no id to continue from")
    return cursor


def paginate(fetch: Fetch, *, after: str | None, before: str | None) -> Iterator[Any]:
    backward = before is not None and after is None
    while True:
        items, has_more = _items(fetch(after, before))
        if not has_more or not items:
            yield from items
            return
        try:
            cursor = _cursor(items, backward)
        except BillionSendError:
            yield from items
            raise
        yield from items
        if backward:
            before = cursor
        else:
            after = cursor


async def apaginate(
    fetch: AsyncFetch, *, after: str | None, before: str | None
) -> AsyncIterator[Any]:
    backward = before is not None and after is None
    while True:
        items, has_more = _items(await fetch(after, before))
        if not has_more or not items:
            for item in items:
                yield item
            return
        try:
            cursor = _cursor(items, backward)
        except BillionSendError:
            for item in items:
                yield item
            raise
        for item in items:
            yield item
        if backward:
            before = cursor
        else:
            after = cursor
