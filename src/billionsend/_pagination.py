"""Iterate over every item of a cursor-paginated list.

Lists are newest first: ``after`` takes the last id of a page and pages toward
older items, ``before`` takes the first id and pages toward newer ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any, TypeVar

__all__ = ["apaginate", "paginate"]

T = TypeVar("T", bound=Mapping[str, Any])

Fetch = Callable[[str | None, str | None], tuple[list[T], bool]]
AsyncFetch = Callable[[str | None, str | None], Awaitable[tuple[list[T], bool]]]


def paginate(fetch: Fetch[T], *, after: str | None, before: str | None) -> Iterator[T]:
    backward = before is not None and after is None
    while True:
        items, has_more = fetch(after, before)
        yield from items
        if not has_more or not items:
            return
        if backward:
            before = str(items[0]["id"])
        else:
            after = str(items[-1]["id"])


async def apaginate(
    fetch: AsyncFetch[T], *, after: str | None, before: str | None
) -> AsyncIterator[T]:
    backward = before is not None and after is None
    while True:
        items, has_more = await fetch(after, before)
        for item in items:
            yield item
        if not has_more or not items:
            return
        if backward:
            before = str(items[0]["id"])
        else:
            after = str(items[-1]["id"])
