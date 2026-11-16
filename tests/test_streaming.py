"""Server-sent event streams: parsing, resuming and reconnecting."""

from __future__ import annotations

import httpx
import pytest

from billionsend import (
    APIConnectionError,
    APIError,
    AsyncBillionSend,
    BillionSend,
    PermissionDeniedError,
    ServerSentEvent,
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


def test_stream_yields_events(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "GET",
        TEAM,
        headers=SSE,
        stream=sse(
            "retry: 1000\n\n",
            ": keepalive\n\n",
            'id: a1\nevent: email.received\ndata: {"x":',
            "1}\n\n",
        ),
    )
    with client.inboxes.stream_events(
        types=["email.received", "inbox.thread.created"], max_reconnects=0
    ) as stream:
        events = list(stream)
    assert [(e.id, e.event, e.data) for e in events] == [("a1", "email.received", {"x": 1})]
    request = mock.last.request
    assert request.headers["accept"] == "text/event-stream"
    assert "last-event-id" not in request.headers
    assert mock.last.query == {"types": ["email.received,inbox.thread.created"]}
    assert stream.last_event_id == "a1"


def test_resume_from_a_known_id(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", INBOX, headers=SSE, stream=sse("id: 6\ndata: {}\n\n"))
    stream = client.inboxes.stream_inbox_events(
        "support@example.com", last_event_id="5", max_reconnects=0
    )
    list(stream)
    assert mock.last.request.headers["last-event-id"] == "5"
    assert mock.last.query == {}
    assert mock.last.path_params == {"inbox_id": "support@example.com"}


def test_reconnects_after_the_server_closes(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond(
        "GET",
        TEAM,
        headers=SSE,
        stream=sse("retry: 250\n\n", "id: 1\ndata: 1\n\nid: 2\ndata: 2\n\n"),
    )
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 3\ndata: 3\n\n"))
    stream = client.inboxes.stream_events()
    received = []
    for event in stream:
        received.append(event.data)
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
        stream=sse("id: 1\ndata: 1\n\n", "id: 2\nda", error=httpx.ReadError("reset")),
    )
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 2\ndata: 2\n\n"))
    with client.inboxes.stream_events() as stream:
        received = []
        for event in stream:
            received.append((event.id, event.data))
            if len(received) == 2:
                break
    assert received == [("1", 1), ("2", 2)]
    assert mock.calls[1].request.headers["last-event-id"] == "1"
    assert sleeps == [1.0]


def test_early_break_keeps_the_position_of_the_last_event_handed_out(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 1\ndata: 1\n\nid: 2\ndata: 2\n\n"))
    stream = client.inboxes.stream_events()
    for _ in stream:
        break
    stream.close()
    assert stream.last_event_id == "1"


def test_refused_stream_raises_on_open(client: BillionSend, mock: ContractMock) -> None:
    mock.error("GET", TEAM, 403, "restricted_api_key", "Needs a team-wide key")
    with pytest.raises(PermissionDeniedError) as info:
        client.inboxes.stream_events()
    assert info.value.message == "Needs a team-wide key"


def test_refused_reconnect_raises(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 1\ndata: 1\n\n"))
    mock.error("GET", TEAM, 403, "restricted_api_key", "key revoked")
    stream = client.inboxes.stream_events()
    received: list[object] = []
    with pytest.raises(PermissionDeniedError):
        received.extend(e.data for e in stream)
    assert received == [1]


def test_failed_reconnects_are_bounded(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 1\ndata: 1\n\n"))
    for _ in range(3):
        mock.error("GET", TEAM, 503, "service_unavailable", "busy")
    stream = client.inboxes.stream_events(max_reconnects=2)
    with pytest.raises(APIError):
        list(stream)
    assert len(mock.calls) == 4
    # one planned pause, then backoff between the failed attempts
    assert sleeps == [1.0, 1.0, 2.0]


def test_too_many_streams_waits_for_retry_after(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 1\ndata: 1\n\n"))
    mock.error("GET", TEAM, 429, "too_many_streams", "cap", headers={"retry-after": "4"})
    mock.respond("GET", TEAM, headers=SSE, stream=sse("id: 2\ndata: 2\n\n"))
    stream = client.inboxes.stream_events()
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
        "GET",
        TEAM,
        headers=SSE,
        stream=sse("id: 1\ndata: 1\n\n", error=httpx.RemoteProtocolError("eof")),
    )
    stream = client.inboxes.stream_events(max_reconnects=0)
    with pytest.raises(APIConnectionError):
        list(stream)
    assert stream.last_event_id == "1"


def test_negative_max_reconnects_is_refused(client: BillionSend) -> None:
    with pytest.raises(ValueError, match="max_reconnects"):
        client.inboxes.stream_events(max_reconnects=-1)


async def test_async_stream_reconnects(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.respond(
        "GET",
        INBOX,
        headers=SSE,
        stream=sse('id: 1\nevent: inbox.thread.created\ndata: {"a":1}\n\n'),
    )
    mock.respond(
        "GET",
        INBOX,
        headers=SSE,
        stream=sse('id: 2\nevent: inbox.email.received\ndata: {"a":2}\n\n'),
    )
    stream = await async_client.inboxes.stream_inbox_events(
        "support@example.com", types="inbox.email.received"
    )
    received = []
    async with stream:
        async for event in stream:
            received.append((event.id, event.event, event.data))
            if len(received) == 2:
                break
    assert received == [
        ("1", "inbox.thread.created", {"a": 1}),
        ("2", "inbox.email.received", {"a": 2}),
    ]
    assert mock.calls[1].request.headers["last-event-id"] == "1"
    assert mock.calls[1].query == {"types": ["inbox.email.received"]}
    assert sleeps == [1.0]


async def test_async_refused_stream(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.error("GET", TEAM, 429, "too_many_streams", "cap")
    with pytest.raises(Exception, match="cap"):
        await async_client.inboxes.stream_events()


async def test_async_drop_without_reconnect(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    mock.respond(
        "GET", TEAM, headers=SSE, stream=sse("id: 1\ndata: 1\n\n", error=httpx.ReadError("reset"))
    )
    stream = await async_client.inboxes.stream_events(max_reconnects=0)
    with pytest.raises(APIConnectionError):
        [e async for e in stream]
