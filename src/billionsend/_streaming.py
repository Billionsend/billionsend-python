"""Server-sent event streams (``text/event-stream``).

A stream is opened by the method that returns it, so a refused stream raises at
once. Iterating yields :class:`InboxStreamEvent` and :class:`StreamGapEvent`
objects; when the server ends the stream (it does so every few minutes) or the
connection drops, the stream reconnects with ``Last-Event-ID`` set to the last
id it saw and resumes without losing events. ``max_reconnects`` bounds the
number of consecutive failed reconnect attempts (0 turns reconnecting off).

The server replays at most 24 hours of missed events. A stream resumed from an
older id first gets a :class:`StreamGapEvent`: events may be missing, so read
the REST lists to catch up. It is handed out once per position, although the
server repeats it on every reconnect until a newer event arrives.

The stream of one inbox ends (iteration stops) after that inbox's
``inbox.deleted`` or ``inbox.expired`` event.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import httpx

from ._base_client import connection_error
from ._exceptions import APIConnectionError, APIError, BillionSendError, RateLimitError

if TYPE_CHECKING:
    from ._base_client import AsyncAPIClient, SyncAPIClient, Timeout

__all__ = [
    "AsyncEventStream",
    "EventStream",
    "InboxStreamEvent",
    "InboxStreamEventData",
    "SSEDecoder",
    "ServerSentEvent",
    "StreamEvent",
    "StreamGapData",
    "StreamGapEvent",
]

DEFAULT_RETRY = 1.0
MAX_RECONNECT_DELAY = 30.0
GAP_EVENT: Literal["stream.gap"] = "stream.gap"
TEAM_STREAM_PATH = "/inboxes/events"
# After one of these, the server ends an inbox's own stream for good.
INBOX_GONE = frozenset({"inbox.deleted", "inbox.expired"})


@dataclass(frozen=True)
class ServerSentEvent:
    """One event of a stream.

    Attributes:
        id: the stream position to resume from (the last ``id`` field seen).
        event: the event name, such as ``"inbox.email.received"``.
        data: the event's ``data`` parsed as JSON (the raw text if it is not JSON).
    """

    id: str | None
    event: str
    data: Any


class InboxStreamEventData(TypedDict):
    """The ``data`` of an inbox event: the webhook event of the same name, wrapped."""

    type: str
    """The event name, such as ``"inbox.email.received"`` (same as ``event``)."""
    created_at: str
    """When it happened (ISO 8601)."""
    inbox_id: str | None
    """The inbox it is about (``None`` for an ``email.received`` that reached no inbox)."""
    data: dict[str, Any]
    """The webhook payload's ``data``."""


@dataclass(frozen=True)
class InboxStreamEvent:
    """One inbox event.

    Attributes:
        id: the stream position after this event (resume from it with ``last_event_id=``).
        event: the event name, such as ``"inbox.email.received"``.
        data: ``type``, ``created_at``, ``inbox_id`` and the webhook payload's ``data``.
    """

    id: str
    event: str
    data: InboxStreamEventData


class StreamGapData(TypedDict):
    reason: str
    """Why events may be missing: ``"retention"`` (the position is over 24 hours old)."""


@dataclass(frozen=True)
class StreamGapEvent:
    """Events may have been lost before this point: catch up through the REST lists."""

    data: StreamGapData
    id: None = None
    event: Literal["stream.gap"] = GAP_EVENT


StreamEvent = InboxStreamEvent | StreamGapEvent


class SSEDecoder:
    """Incremental parser of the ``text/event-stream`` format.

    Lines may end in CRLF, LF or CR and may be split anywhere across chunks;
    comment lines are skipped; several ``data`` lines join with newlines.
    """

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._started = False
        self._after_cr = False
        self._data: list[str] = []
        self._event = ""
        self.last_event_id: str | None = None
        self.retry: int | None = None

    def feed(self, chunk: bytes) -> list[ServerSentEvent]:
        text = self._utf8.decode(chunk)
        if not self._started and text:
            self._started = True
            text = text.removeprefix("\ufeff")
        if self._after_cr and text:
            # The previous chunk ended in CR: a LF here completes that CRLF.
            self._after_cr = False
            text = text.removeprefix("\n")
        self._buffer += text
        events: list[ServerSentEvent] = []
        while True:
            cr = self._buffer.find("\r")
            lf = self._buffer.find("\n")
            ends = [i for i in (cr, lf) if i != -1]
            if not ends:
                break
            index = min(ends)
            end = index + 1
            if self._buffer[index] == "\r":
                if index + 1 == len(self._buffer):
                    self._after_cr = True
                elif self._buffer[index + 1] == "\n":
                    end = index + 2
            line = self._buffer[:index]
            self._buffer = self._buffer[end:]
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def _line(self, line: str) -> ServerSentEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id":
            if "\0" not in value:
                self.last_event_id = value
        elif name == "retry" and value.isdigit():
            self.retry = int(value)
        return None

    def _dispatch(self) -> ServerSentEvent | None:
        if not self._data:
            self._event = ""
            return None
        raw = "\n".join(self._data)
        try:
            data: Any = json.loads(raw)
        except ValueError:
            data = raw
        event = ServerSentEvent(id=self.last_event_id, event=self._event or "message", data=data)
        self._data = []
        self._event = ""
        return event


def _typed(sse: ServerSentEvent) -> StreamEvent:
    if sse.event == GAP_EVENT:
        reason = sse.data.get("reason") if isinstance(sse.data, dict) else None
        return StreamGapEvent(data={"reason": reason if isinstance(reason, str) else "unknown"})
    if sse.id is None or not isinstance(sse.data, dict):
        raise BillionSendError(f"Malformed {sse.event!r} event in the stream")
    return InboxStreamEvent(id=sse.id, event=sse.event, data=cast("InboxStreamEventData", sse.data))


def _retryable(exc: BillionSendError) -> bool:
    return isinstance(exc, (APIConnectionError, RateLimitError, APIError))


class _StreamState:
    def __init__(
        self,
        path: str,
        query: Mapping[str, Any] | None,
        last_event_id: str | None,
        max_reconnects: int,
        extra_headers: Mapping[str, str] | None,
        timeout: Timeout | None,
    ) -> None:
        if max_reconnects < 0:
            raise ValueError("max_reconnects must be 0 or more")
        self.path = path
        self.query = query
        self.last_event_id = last_event_id
        self.max_reconnects = max_reconnects
        self.extra_headers = extra_headers
        self.timeout = timeout
        self.retry = DEFAULT_RETRY
        self.closed = False
        self.one_inbox = path.rstrip("/") != TEAM_STREAM_PATH
        # The position a gap was last handed out at; the server repeats it on reconnect.
        self.gap_at: tuple[str | None] | None = None

    def failure_delay(self, failures: int, exc: BillionSendError) -> float:
        if isinstance(exc, RateLimitError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RECONNECT_DELAY)
        return min(self.retry * 2.0 ** (failures - 1), MAX_RECONNECT_DELAY)

    def saw(self, sse: ServerSentEvent) -> StreamEvent | None:
        """The event to hand out for ``sse``, or None for a repeated gap."""
        event = _typed(sse)
        if isinstance(event, StreamGapEvent):
            if self.gap_at == (self.last_event_id,):
                return None
            self.gap_at = (self.last_event_id,)
            return event
        self.last_event_id = event.id
        if self.one_inbox and event.event in INBOX_GONE:
            self.closed = True
        return event

    def saw_end(self, decoder: SSEDecoder) -> None:
        # Every event was handed out; an id sent without data still moves the position.
        if decoder.last_event_id is not None:
            self.last_event_id = decoder.last_event_id

    def absorb_retry(self, decoder: SSEDecoder) -> None:
        if decoder.retry is not None:
            self.retry = decoder.retry / 1000


class EventStream:
    """A synchronous event stream; iterate it, and close it (or use ``with``) when done."""

    def __init__(
        self,
        client: SyncAPIClient,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        last_event_id: str | None,
        max_reconnects: int,
        extra_headers: Mapping[str, str] | None,
        timeout: Timeout | None,
    ) -> None:
        self._client = client
        self._state = _StreamState(
            path, query, last_event_id, max_reconnects, extra_headers, timeout
        )
        self._response: httpx.Response | None = None

    @property
    def last_event_id(self) -> str | None:
        """The id of the last event received: pass it to resume later."""
        return self._state.last_event_id

    def open(self) -> None:
        s = self._state
        self._response = self._client.open_stream(
            s.path,
            query=s.query,
            last_event_id=s.last_event_id,
            extra_headers=s.extra_headers,
            timeout=s.timeout,
        )

    def _reconnect(self) -> None:
        failures = 0
        while True:
            try:
                self.open()
            except BillionSendError as exc:
                failures += 1
                if not _retryable(exc) or failures > self._state.max_reconnects:
                    raise
                self._client._sleep(self._state.failure_delay(failures, exc))
            else:
                return

    def __iter__(self) -> Iterator[StreamEvent]:
        s = self._state
        while not s.closed:
            if self._response is None:
                self._reconnect()
            response = self._response
            assert response is not None
            decoder = SSEDecoder()
            error: APIConnectionError | None = None
            try:
                for chunk in response.iter_bytes():
                    for sse in decoder.feed(chunk):
                        event = s.saw(sse)
                        if event is not None:
                            yield event
                        if s.closed:
                            break
                    if s.closed:
                        break
                else:
                    s.saw_end(decoder)
            except httpx.TransportError as exc:
                error = connection_error(exc)
            finally:
                s.absorb_retry(decoder)
                response.close()
                self._response = None
            if s.closed:
                return
            if s.max_reconnects == 0:
                if error is not None:
                    raise error
                return
            self._client._sleep(s.retry)

    def close(self) -> None:
        self._state.closed = True
        if self._response is not None:
            self._response.close()
            self._response = None

    def __enter__(self) -> EventStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncEventStream:
    """An asynchronous event stream; ``async for`` it, and close it (or ``async with``)."""

    def __init__(
        self,
        client: AsyncAPIClient,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        last_event_id: str | None,
        max_reconnects: int,
        extra_headers: Mapping[str, str] | None,
        timeout: Timeout | None,
    ) -> None:
        self._client = client
        self._state = _StreamState(
            path, query, last_event_id, max_reconnects, extra_headers, timeout
        )
        self._response: httpx.Response | None = None

    @property
    def last_event_id(self) -> str | None:
        """The id of the last event received: pass it to resume later."""
        return self._state.last_event_id

    async def open(self) -> None:
        s = self._state
        self._response = await self._client.open_stream(
            s.path,
            query=s.query,
            last_event_id=s.last_event_id,
            extra_headers=s.extra_headers,
            timeout=s.timeout,
        )

    async def _reconnect(self) -> None:
        failures = 0
        while True:
            try:
                await self.open()
            except BillionSendError as exc:
                failures += 1
                if not _retryable(exc) or failures > self._state.max_reconnects:
                    raise
                await self._client._sleep(self._state.failure_delay(failures, exc))
            else:
                return

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        s = self._state
        while not s.closed:
            if self._response is None:
                await self._reconnect()
            response = self._response
            assert response is not None
            decoder = SSEDecoder()
            error: APIConnectionError | None = None
            try:
                async for chunk in response.aiter_bytes():
                    for sse in decoder.feed(chunk):
                        event = s.saw(sse)
                        if event is not None:
                            yield event
                        if s.closed:
                            break
                    if s.closed:
                        break
                else:
                    s.saw_end(decoder)
            except httpx.TransportError as exc:
                error = connection_error(exc)
            finally:
                s.absorb_retry(decoder)
                await response.aclose()
                self._response = None
            if s.closed:
                return
            if s.max_reconnects == 0:
                if error is not None:
                    raise error
                return
            await self._client._sleep(s.retry)

    async def close(self) -> None:
        self._state.closed = True
        if self._response is not None:
            await self._response.aclose()
            self._response = None

    async def __aenter__(self) -> AsyncEventStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
