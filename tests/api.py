"""One way to call either client: ``api.call("emails.get", id)`` runs the sync or the async method.

The generic tests (every operation, every parameter) run once per client this way,
so the async client is held to exactly what the sync one is.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

from billionsend import AsyncBillionSend, AsyncEventStream, BillionSend, EventStream


def snake(text: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()


class Api:
    """A sync view of a client, sync or async."""

    def __init__(self, client: BillionSend | AsyncBillionSend, loop: asyncio.AbstractEventLoop):
        self.client = client
        self.is_async = isinstance(client, AsyncBillionSend)
        self._loop = loop

    def method(self, operation_id: str) -> Any:
        """The SDK method of an ``operationId`` (``emails.receiving.attachments.list``)."""
        target: Any = self.client
        for part in operation_id.split("."):
            target = getattr(target, snake(part))
        return target

    def call(self, dotted: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``client.<dotted>(...)``; an iterator or a stream is collected into a list."""
        target: Any = self.client
        for part in dotted.split("."):
            target = getattr(target, part)
        return self.settle(target(*args, **kwargs))

    def settle(self, result: Any) -> Any:
        if not self.is_async:
            if isinstance(result, EventStream):
                with result:
                    return list(result)
            if isinstance(result, Iterator):
                return list(result)
            return result
        if inspect.isawaitable(result):
            result = self._loop.run_until_complete(result)
        if isinstance(result, AsyncEventStream):
            return self._loop.run_until_complete(self._drain_stream(result))
        if isinstance(result, AsyncIterator):
            return self._loop.run_until_complete(self._collect(result))
        return result

    @staticmethod
    async def _collect(items: AsyncIterator[Any]) -> list[Any]:
        return [item async for item in items]

    @staticmethod
    async def _drain_stream(stream: AsyncEventStream) -> list[Any]:
        async with stream:
            return [event async for event in stream]

    def run(self, coroutine_or_value: Any) -> Any:
        """Await a coroutine on the async client's loop, or pass a value through."""
        if inspect.isawaitable(coroutine_or_value):
            return self._loop.run_until_complete(coroutine_or_value)
        return coroutine_or_value
