"""Server-sent event streams: parsing, resuming and reconnecting."""

from __future__ import annotations

import json

import httpx
import pytest

from billionsend import (
    APIConnectionError,
    APIError,
    AsyncBillionSend,
    BillionSend,
    BillionSendError,
    InboxStreamEvent,
    NotFoundError,
    PermissionDeniedError,
    ServerSentEvent,
    StreamEvent,
    StreamGapEvent,
)
from billionsend._streaming import SSEDecoder

from .conftest import Sleeps
from .contract import ChunkStream, ContractMock

TEAM = "/inboxes/events"
INBOX = "/inboxes/{inbox_id}/events"
SSE = {"content-type": "text/event-stream"}


def decode(*chunks: bytes) -> tuple[list[ServerSentEvent], SSEDecoder]:
    decoder = SSEDecoder()
    events: list[ServerSentEvent] = []
    for chunk in chunks:
        events += decoder.feed(chunk)
    return events, decoder


# ------------------------------------------------------------------ the decoder


def test_parses_id_event_and_json_data() -> None:
    events, _ = decode(b'id: 42\nevent: inbox.email.received\ndata: {"inbox_id":"i1"}\n\n')
    assert events == [
        ServerSentEvent(id="42", event="inbox.email.received", data={"inbox_id": "i1"})
    ]


def test_events_split_anywhere_across_chunks() -> None:
    raw = b'id: 1\nevent: a\ndata: {"n":1}\n\nid: 2\nevent: b\ndata: {"n":2}\n\n'
    events, _ = decode(*[raw[i : i + 1] for i in range(len(raw))])
    assert [(e.id, e.event, e.data) for e in events] == [("1", "a", {"n": 1}), ("2", "b", {"n": 2})]


def test_crlf_split_between_chunks_is_one_line_end() -> None:
    events, _ = decode(b"id: 7\r", b"\ndata: 1\r\n\r", b"\n")
    assert [(e.id, e.data) for e in events] == [("7", 1)]


def test_cr_only_line_ends() -> None:
    events, _ = decode(b"data: x\r\rdata: y\r\r")
    assert [e.data for e in events] == ["x", "y"]


def test_comments_and_keepalives_are_skipped() -> None:
    events, _ = decode(b": keepalive\n\n: hello\nid: 3\ndata: 3\n\n: keepalive\n\n")
    assert [(e.id, e.data) for e in events] == [("3", 3)]


def test_multiline_data_is_joined_with_newlines() -> None:
    events, _ = decode(b"data: first\ndata: second\n\n", b'data: {"a":\ndata: [1, 2]}\n\n')
    assert events[0].data == "first\nsecond"
    assert events[1].data == {"a": [1, 2]}


def test_defaults_and_inherited_id() -> None:
    events, _ = decode(b"id: 9\ndata: 1\n\ndata: 2\n\n")
    assert [(e.id, e.event) for e in events] == [("9", "message"), ("9", "message")]


def test_id_without_data_moves_the_position_only() -> None:
    events, decoder = decode(b"id: 10\n\n")
    assert events == []
    assert decoder.last_event_id == "10"


def test_retry_field_and_field_without_colon() -> None:
    events, decoder = decode(b"retry: 2500\nretry: soon\ndata\n\n")
    assert decoder.retry == 2500
    assert [e.data for e in events] == [""]


def test_bom_and_split_utf8() -> None:
    text = 'data: "héllo ✓"\n\n'.encode()
    events, _ = decode(b"\xef\xbb\xbf" + text[:9], text[9:])
    assert [e.data for e in events] == ["héllo ✓"]


def test_unterminated_event_is_not_dispatched() -> None:
    events, _ = decode(b"data: 1\n")
    assert events == []


# ------------------------------------------------------------------ streams


def sse(*blocks: str, error: Exception | None = None) -> ChunkStream:
    return ChunkStream([b.encode() for b in blocks], error=error)


def ev(id: str, n: int = 0, event: str = "inbox.email.received", inbox: str | None = "in_1") -> str:
    """One event as the API writes it."""
    body = {
        "type": event,
        "created_at": "2026-11-16T09:00:00.000Z",
        "inbox_id": inbox,
        "data": {"n": n},
    }
    return f"id: {id}\nevent: {event}\ndata: {json.dumps(body)}\n\n"


GAP = 'event: stream.gap\ndata: {"reason":"retention"}\n\n'


def n(event: StreamEvent) -> int:
    assert isinstance(event, InboxStreamEvent)
    value = event.data["data"]["n"]
    assert isinstance(value, int)
    return value


def test_stream_yields_typed_events(client: BillionSend, mock: ContractMock) -> None:
    raw = ev("1.2.3", 1, "email.received", None)
    mock.respond(
        "GET",
        TEAM,
        headers=SSE,
        stream=sse("retry: 1000\n\n", ": keepalive\n\n", raw[:30], raw[30:]),
    )
    with client.inboxes.events.stream(
        types=["email.received", "inbox.thread.created"], max_reconnects=0
    ) as stream:
        events = list(stream)
    assert events == [
        InboxStreamEvent(
            id="1.2.3",
            event="email.received",
            data={
                "type": "email.received",
                "created_at": "2026-11-16T09:00:00.000Z",
                "inbox_id": None,
                "data": {"n": 1},
            },
        )
    ]
    request = mock.last.request
    assert request.headers["accept"] == "text/event-stream"
    assert "last-event-id" not in request.headers
    assert mock.last.query == {"types": ["email.received,inbox.thread.created"]}
    assert stream.last_event_id == "1.2.3"


def test_resume_from_a_known_id(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", INBOX, headers=SSE, stream=sse(ev("6")))
    stream = client.inboxes.events.stream_inbox(
        "support@example.com", last_event_id="5", max_reconnects=0
    )
    list(stream)
    assert mock.last.request.headers["last-event-id"] == "5"
    assert mock.last.query == {}
    assert mock.last.path_params == {"inbox_id": "support@example.com"}


def test_reconnects_after_the_server_closes(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("retry: 250\n\n", ev("1", 1) + ev("2", 2)))
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("3", 3)))
    stream = client.inboxes.events.stream()
    received = []
    for event in stream:
        received.append(n(event))
        if len(received) == 3:
            break
    stream.close()
    assert received == [1, 2, 3]
    assert "last-event-id" not in mock.calls[0].request.headers
    assert mock.calls[1].request.headers["last-event-id"] == "2"
    assert sleeps == [0.25]


def test_reconnects_after_a_dropped_connection(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond(
        "GET",
        TEAM,
        headers=SSE,
        stream=sse(ev("1", 1), "id: 2\nda", error=httpx.ReadError("reset")),
    )
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("2", 2)))
    with client.inboxes.events.stream() as stream:
        received = []
        for event in stream:
            received.append((event.id, n(event)))
            if len(received) == 2:
                break
    assert received == [("1", 1), ("2", 2)]
    assert mock.calls[1].request.headers["last-event-id"] == "1"
    assert sleeps == [1.0]


def test_early_break_keeps_the_position_of_the_last_event_handed_out(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("1") + ev("2")))
    stream = client.inboxes.events.stream()
    for _ in stream:
        break
    stream.close()
    assert stream.last_event_id == "1"


def test_gap_is_handed_out_once_per_position(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    # The server repeats the gap on every reconnect from the same expired id.
    mock.respond("GET", TEAM, headers=SSE, stream=sse("retry: 1000\n\n", GAP))
    mock.respond("GET", TEAM, headers=SSE, stream=sse(GAP, ev("9", 9)))
    mock.respond("GET", TEAM, headers=SSE, stream=sse(GAP, ev("10", 10)))
    stream = client.inboxes.events.stream(last_event_id="1.1.1")
    received: list[StreamEvent] = []
    for event in stream:
        received.append(event)
        if len(received) == 4:
            break
    stream.close()
    assert received[0] == StreamGapEvent(data={"reason": "retention"})
    assert received[0].id is None
    assert received[0].event == "stream.gap"
    assert [n(e) for e in received[1:2]] == [9]
    # a gap at a newer position is news again
    assert isinstance(received[2], StreamGapEvent)
    assert n(received[3]) == 10
    assert [c.request.headers["last-event-id"] for c in mock.calls] == ["1.1.1", "1.1.1", "9"]


def test_one_inbox_stream_ends_with_the_inbox(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "GET", INBOX, headers=SSE, stream=sse(ev("1", 1), ev("2", 2, "inbox.deleted"), ev("3"))
    )
    stream = client.inboxes.events.stream_inbox("support@example.com")
    events = list(stream)
    assert [e.event for e in events] == ["inbox.email.received", "inbox.deleted"]
    assert stream.last_event_id == "2"
    assert len(mock.calls) == 1


def test_team_stream_goes_on_after_an_inbox_is_deleted(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.respond(
        "GET",
        TEAM,
        headers=SSE,
        stream=sse(ev("1", 1, "inbox.expired"), ev("2", 2)),
    )
    stream = client.inboxes.events.stream(max_reconnects=0)
    assert [n(e) for e in stream] == [1, 2]


def test_malformed_event_raises(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 1\nevent: x\ndata: 1\n\n"))
    with pytest.raises(BillionSendError, match="Malformed"):
        list(client.inboxes.events.stream(max_reconnects=0))


def test_refused_stream_raises_on_open(client: BillionSend, mock: ContractMock) -> None:
    mock.error(
        "GET",
        TEAM,
        403,
        "restricted_api_key",
        "Needs a team-wide key",
        headers={"x-request-id": "req_1"},
    )
    with pytest.raises(PermissionDeniedError) as info:
        client.inboxes.events.stream()
    assert info.value.message == "Needs a team-wide key"
    assert info.value.request_id == "req_1"


def test_refused_reconnect_raises(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("1", 1)))
    mock.error("GET", TEAM, 403, "restricted_api_key", "key revoked")
    stream = client.inboxes.events.stream()
    received: list[int] = []
    with pytest.raises(PermissionDeniedError):
        received.extend(n(e) for e in stream)
    assert received == [1]


def test_deleted_inbox_reconnect_raises_not_found(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", INBOX, headers=SSE, stream=sse(ev("1", 1)))
    mock.error("GET", INBOX, 404, "not_found", "Inbox not found")
    stream = client.inboxes.events.stream_inbox("support@example.com")
    with pytest.raises(NotFoundError):
        list(stream)


def test_failed_reconnects_are_bounded(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("1")))
    for _ in range(3):
        mock.error("GET", TEAM, 503, "service_unavailable", "busy")
    stream = client.inboxes.events.stream(max_reconnects=2)
    with pytest.raises(APIError):
        list(stream)
    assert len(mock.calls) == 4
    # one planned pause, then backoff between the failed attempts
    assert sleeps == [1.0, 1.0, 2.0]


def test_too_many_streams_waits_for_retry_after(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("1")))
    mock.error("GET", TEAM, 429, "too_many_streams", "cap", headers={"retry-after": "4"})
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("2")))
    stream = client.inboxes.events.stream()
    got = []
    for event in stream:
        got.append(event.id)
        if len(got) == 2:
            break
    assert got == ["1", "2"]
    assert sleeps == [1.0, 4.0]
    assert mock.calls[2].request.headers["last-event-id"] == "1"


def test_no_reconnect_raises_the_drop(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "GET", TEAM, headers=SSE, stream=sse(ev("1"), error=httpx.RemoteProtocolError("eof"))
    )
    stream = client.inboxes.events.stream(max_reconnects=0)
    with pytest.raises(APIConnectionError):
        list(stream)
    assert stream.last_event_id == "1"


def test_negative_max_reconnects_is_refused(client: BillionSend) -> None:
    with pytest.raises(ValueError, match="max_reconnects"):
        client.inboxes.events.stream(max_reconnects=-1)


async def test_async_stream_reconnects(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", INBOX, headers=SSE, stream=sse(ev("1", 1, "inbox.thread.created")))
    mock.respond("GET", INBOX, headers=SSE, stream=sse(ev("2", 2)))
    stream = await async_client.inboxes.events.stream_inbox(
        "support@example.com", types="inbox.email.received"
    )
    received = []
    async with stream:
        async for event in stream:
            received.append((event.id, event.event, n(event)))
            if len(received) == 2:
                break
    assert received == [("1", "inbox.thread.created", 1), ("2", "inbox.email.received", 2)]
    assert mock.calls[1].request.headers["last-event-id"] == "1"
    assert mock.calls[1].query == {"types": ["inbox.email.received"]}
    assert sleeps == [1.0]


async def test_async_gap_and_inbox_end(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.respond("GET", INBOX, headers=SSE, stream=sse(GAP, ev("4", 4, "inbox.expired"), ev("5")))
    stream = await async_client.inboxes.events.stream_inbox("in_1", last_event_id="0.0.0")
    events = [e async for e in stream]
    assert [e.event for e in events] == ["stream.gap", "inbox.expired"]


async def test_async_refused_stream(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.error("GET", TEAM, 429, "too_many_streams", "cap")
    with pytest.raises(Exception, match="cap"):
        await async_client.inboxes.events.stream()


async def test_async_drop_without_reconnect(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse(ev("1"), error=httpx.ReadError("reset")))
    stream = await async_client.inboxes.events.stream(max_reconnects=0)
    with pytest.raises(APIConnectionError):
        [e async for e in stream]
