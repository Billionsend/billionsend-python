"""The hand-written runtime: configuration, errors, retries, uploads and real sockets.

Most of it runs on the contract mock; the last section talks to a real HTTP server
on localhost, so that timeouts, refused connections and a dropped event stream are
the real thing and not a stand-in raised by a test transport.
"""

from __future__ import annotations

import email.utils
import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

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
    InboxStreamEvent,
    NotFoundError,
    RateLimitError,
)
from billionsend._base_client import (
    MAX_RETRY_DELAY,
    STREAM_READ_TIMEOUT,
    encode_form,
    encode_path,
    encode_query,
    parse_retry_after,
)

from .api import Api
from .conftest import Sleeps
from .contract import API_KEY, BASE_URL, ContractMock

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
SEND = {"from": "a@acme.dev", "to": "b@example.com", "subject": "s", "text": "t"}


# ------------------------------------------------------------------ configuration


def test_an_empty_key_in_the_environment_is_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLIONSEND_API_KEY", "")
    client = BillionSend(
        base_url=BASE_URL, http_client=httpx.Client(transport=ContractMock().transport())
    )
    with pytest.raises(BillionSendError) as info:
        client.usage.get()
    assert (info.value.name, info.value.status_code) == ("missing_api_key", None)
    assert "BILLIONSEND_API_KEY" in info.value.message


def test_an_empty_base_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLIONSEND_BASE_URL", "https://env.example.test/")
    assert BillionSend("k", base_url="").base_url == "https://env.example.test"
    monkeypatch.delenv("BILLIONSEND_BASE_URL")
    assert BillionSend("k", base_url="").base_url == "https://api.billionsend.com"


def test_a_key_is_sent_as_a_bearer_token_and_never_without_one(mock: ContractMock) -> None:
    client = BillionSend(
        "bs_secret", base_url=BASE_URL, http_client=httpx.Client(transport=mock.transport())
    )
    with pytest.raises(AssertionError, match="Authorization"):
        client.usage.get()  # the mock expects another key; what matters is what was sent
    assert mock.calls == []


def test_default_headers_replace_ours_whatever_the_case(mock: ContractMock) -> None:
    client = BillionSend(
        API_KEY,
        base_url=BASE_URL,
        default_headers={"user-agent": "my-app/1", "X-Team": "t"},
        http_client=httpx.Client(transport=mock.transport()),
    )
    client.usage.get(extra_headers={"X-Team": "t2"})
    headers = mock.last.request.headers
    assert headers.get_list("user-agent") == ["my-app/1"]
    assert headers["x-team"] == "t2"


# ------------------------------------------------------------------ path and query


def test_path_segments_are_encoded() -> None:
    assert encode_path("a b/c+d@e.com é#?%") == "a%20b%2Fc%2Bd@e.com%20%C3%A9%23%3F%25"
    assert encode_path(ID) == ID


def test_an_empty_path_segment_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        encode_path("")


@pytest.mark.parametrize("value", [".", ".."])
def test_a_dot_path_segment_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="path parameter"):
        encode_path(value)
    assert encode_path("...") == "..."  # only the two that a URL folds away
    assert encode_path("a.b") == "a.b"


def test_query_values() -> None:
    assert encode_query(None) == []
    assert encode_query(
        {"a": None, "b": True, "c": False, "d": 0, "e": [1, "x", True], "f": [], "g": ("p", "q")}
    ) == [("b", "true"), ("c", "false"), ("d", "0"), ("e", "1,x,true"), ("g", "p,q")]


def test_an_empty_list_filter_is_not_sent(client: BillionSend, mock: ContractMock) -> None:
    client.emails.metrics(metrics=[], dimensions=["domain"])
    assert mock.last.query == {"dimensions": ["domain"]}


# ------------------------------------------------------------------ multipart


def test_a_line_break_in_a_file_name_cannot_end_the_header_line() -> None:
    data, files = encode_form({"f": ("a\r\nX-Evil: 1\n.csv", b"x", "text/csv")})
    assert data == {}
    assert files == [("f", ("a%0D%0AX-Evil: 1%0A.csv", b"x", "text/csv"))]


def test_form_fields() -> None:
    class Reader:
        def read(self, size: int = -1) -> bytes:
            return b""

    reader = Reader()
    data, files = encode_form(
        {
            "skip": None,
            "flag": True,
            "off": False,
            "count": 3,
            "obj": {"a": [1]},
            "list": [1, "b"],
            "text": "é",
            "raw": b"xyz",
            "named": ("n.csv", b"1", "text/csv"),
            "stream": reader,
        }
    )
    assert data == {
        "flag": "true",
        "off": "false",
        "count": "3",
        "obj": '{"a":[1]}',
        "list": '[1,"b"]',
        "text": "é",
    }
    assert files == [
        ("raw", ("raw", b"xyz")),
        ("named", ("n.csv", b"1", "text/csv")),
        ("stream", ("stream", reader)),
    ]
    assert encode_form(None) == (None, None)


def _parts(request: httpx.Request) -> dict[str, Any]:
    from email.parser import BytesParser
    from email.policy import HTTP

    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    return {str(p.get_param("name", header="content-disposition")): p for p in message.iter_parts()}


def test_upload_with_a_non_ascii_file_name(client: BillionSend, mock: ContractMock) -> None:
    client.contacts.imports.create({"file": ('联系人 "q".csv', b"Email\na@example.com\n")})
    part = _parts(mock.last.request)["file"]
    assert part.get_filename() == "联系人 %22q%22.csv"
    assert part.get_content_type() == "text/csv"


def test_a_file_name_goes_out_as_utf8_between_quotes_and_never_as_an_encoded_word(
    client: BillionSend, mock: ContractMock
) -> None:
    client.contacts.imports.create({"file": ('联系人 "q"\r\nX: y.csv', b"Email\na@example.com\n")})
    raw = mock.last.request.content
    disposition = "联系人 %22q%22%0D%0AX: y.csv".encode()
    assert b'name="file"; filename="' + disposition + b'"\r\n' in raw
    assert b"=?" not in raw  # no RFC 2047 encoded word
    assert b"filename*" not in raw
    assert b"\r\nX: y.csv" not in raw  # the name did not start a header line of its own


def test_upload_without_a_name_is_still_a_file(client: BillionSend, mock: ContractMock) -> None:
    client.contacts.imports.create({"file": b"Email\na@example.com\n"})
    part = _parts(mock.last.request)["file"]
    assert part.get_filename() == "file"
    assert part.get_payload(decode=True) == b"Email\na@example.com\n"


class Spy(io.BytesIO):
    """A file that remembers how it was read."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.reads: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        self.reads.append(-1 if size is None else size)
        return super().read(size)


def test_a_big_file_is_streamed_in_chunks_not_read_whole(
    client: BillionSend, mock: ContractMock
) -> None:
    row = b"ada@example.com,Ada\n"
    content = b"Email,First Name\n" + row * (3 * 1024 * 1024 // len(row))
    spy = Spy(content)
    client.contacts.imports.create({"file": ("big.csv", spy, "text/csv")})
    assert spy.reads
    assert all(0 < r <= 65536 for r in spy.reads), spy.reads[:5]
    assert _parts(mock.last.request)["file"].get_payload(decode=True) == content


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    "body",
    [b"[1, 2]", b'"text"', b"null", b'{"name": 7, "message": ["x"]}', b"{}"],
)
def test_a_json_error_body_without_the_api_shape(
    client: BillionSend, mock: ContractMock, body: bytes
) -> None:
    client.max_retries = 0
    mock.respond("GET", "/usage", 404, content=body, headers={"content-type": "application/json"})
    with pytest.raises(NotFoundError) as info:
        client.usage.get()
    assert info.value.name == "application_error"
    assert info.value.message == "Not Found"
    assert info.value.status_code == 404


def test_the_error_repr_names_everything(client: BillionSend, mock: ContractMock) -> None:
    client.max_retries = 0
    mock.error("GET", "/usage", 404, "not_found", "gone", headers={"x-request-id": "r-1"})
    with pytest.raises(NotFoundError) as info:
        client.usage.get()
    assert repr(info.value) == (
        "NotFoundError(status_code=404, name='not_found', message='gone', request_id='r-1')"
    )


@pytest.mark.parametrize("status", [200, 201, 204])
@pytest.mark.parametrize("content", [b"", b"  \n"])
def test_a_success_with_no_body_is_none_not_a_parse_error(
    client: BillionSend, mock: ContractMock, status: int, content: bytes
) -> None:
    mock.respond("GET", "/usage", status, content=content)
    assert client.usage.get() is None
    assert len(mock.calls) == 1  # the call worked: nothing to retry


async def test_async_a_success_with_no_body_is_none(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    mock.respond("GET", "/usage", 204, content=b"")
    assert await async_client.usage.get() is None


def test_a_success_that_is_not_json_is_still_an_error(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.respond("GET", "/usage", 200, content=b"<html>", headers={"content-type": "text/html"})
    with pytest.raises(BillionSendError, match="Expected a JSON answer") as info:
        client.usage.get()
    assert info.value.status_code == 200


# ------------------------------------------------------------------ retries


def test_a_retry_after_date_in_the_future_is_waited_out(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    mock.error("GET", "/usage", 503, "service_unavailable", "busy", headers={"retry-after": when})
    client.usage.get()
    assert len(sleeps) == 1
    assert 27 <= sleeps[0] <= 30


def test_a_retry_after_that_is_neither_seconds_nor_a_date_falls_back_to_backoff(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("GET", "/usage", 503, "service_unavailable", "x", headers={"retry-after": "soon"})
    client.usage.get()
    assert 0 < sleeps[0] <= 0.5


def test_parse_retry_after() -> None:
    def parse(value: str | None) -> float | None:
        headers = {} if value is None else {"retry-after": value}
        return parse_retry_after(httpx.Response(429, headers=headers))

    assert parse(None) is None
    assert parse("") is None
    assert parse("12") == 12.0
    assert parse("1.5") == 1.5
    assert parse("-4") == 0.0
    assert parse("garbage") is None
    assert parse("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert MAX_RETRY_DELAY == 60.0


def test_backoff_grows_and_is_capped(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    client.max_retries = 8
    for _ in range(8):
        mock.error("GET", "/usage", 500, "internal_server_error", "oops")
    client.usage.get()
    assert len(sleeps) == 8
    bases = [0.5, 1, 2, 4, 8, 8, 8, 8]
    for slept, base in zip(sleeps, bases, strict=True):
        assert 0.75 * base <= slept <= base


def test_an_api_error_with_a_retry_after_over_the_cap_is_raised(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps
) -> None:
    mock.error("GET", "/usage", 503, "service_unavailable", "x", headers={"retry-after": "61"})
    with pytest.raises(APIError) as info:
        client.usage.get()
    assert info.value.retry_after == 61.0
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422])
def test_a_client_error_is_never_retried(
    client: BillionSend, mock: ContractMock, sleeps: Sleeps, status: int
) -> None:
    mock.error("GET", "/usage", status, "x", "x")
    with pytest.raises(BillionSendError):
        client.usage.get()
    assert len(mock.calls) == 1
    assert sleeps == []


def test_put_is_retried(api: Api, mock: ContractMock, sleeps: Sleeps) -> None:
    mock.error("PUT", "/inboxes/{inbox_id}/threads/{thread_id}/labels", 502, "bad_gateway", "x")
    api.call("inboxes.threads.labels.set", "a@example.com", ID, {"labels": [ID]})
    assert [c.request.method for c in mock.calls] == ["PUT", "PUT"]
    assert len(sleeps) == 1


def test_list_walk_stops_with_the_error_after_the_pages_it_got(
    client: BillionSend, mock: ContractMock
) -> None:
    from .test_pagination import IDS, page

    client.max_retries = 0
    page(mock, "/domains", IDS[0:2], True)
    mock.error("GET", "/domains", 500, "internal_server_error", "oops")
    seen: list[str] = []

    def walk() -> None:
        for domain in client.domains.list_all(limit=2):
            seen.append(domain["id"])

    with pytest.raises(APIError):
        walk()
    assert seen == IDS[0:2]


def test_an_empty_first_page_ends_the_walk(client: BillionSend, mock: ContractMock) -> None:
    from .test_pagination import page

    page(mock, "/domains", [], False)
    assert list(client.domains.list_all()) == []
    assert len(mock.calls) == 1


# ------------------------------------------------------------------ the async client's own paths


async def test_async_refused_stream_and_borrowed_client() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    client = AsyncBillionSend(API_KEY, base_url=BASE_URL, http_client=http)
    with pytest.raises(APIConnectionError, match="refused"):
        await client.inboxes.events.stream(max_reconnects=0)
    await client.close()
    assert not http.is_closed  # the SDK does not close a client it did not make
    await http.aclose()


async def test_async_stream_with_a_timeout_object(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    stream = await async_client.inboxes.events.stream(timeout=httpx.Timeout(3.0), max_reconnects=0)
    assert mock.last.request.extensions["timeout"]["read"] == 3.0
    assert stream.last_event_id is None
    async with stream:
        pass


# ------------------------------------------------------------------ real sockets


class Script(BaseHTTPRequestHandler):
    """Answers as ``plan`` says, one entry per request, and notes what it was asked."""

    plan: ClassVar[list[dict[str, Any]]]
    seen: ClassVar[list[dict[str, str]]]

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.seen.append({k.lower(): v for k, v in self.headers.items()} | {"path": self.path})
        step = self.plan.pop(0)
        if step.get("hang"):
            time.sleep(step["hang"])
            return
        self.send_response(step.get("status", 200))
        for name, value in step.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in step.get("chunks", []):
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        if step.get("cut"):
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if step.get("then_hang"):  # the connection stays open and silent
            time.sleep(step["then_hang"])
            return
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def server() -> Iterator[tuple[str, type[Script]]]:
    class Handler(Script):
        pass

    Handler.plan = []
    Handler.seen = []

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", Handler
    httpd.shutdown()
    httpd.server_close()


def real(url: str, **kw: Any) -> BillionSend:
    return BillionSend(API_KEY, base_url=url, http_client=httpx.Client(trust_env=False), **kw)


def test_a_server_that_does_not_answer_times_out(server: tuple[str, type[Script]]) -> None:
    url, handler = server
    handler.plan = [{"hang": 1.5}]
    started = time.monotonic()
    with pytest.raises(APITimeoutError) as info:
        real(url, timeout=0.3, max_retries=0).usage.get()
    assert time.monotonic() - started < 1.2
    assert info.value.status_code is None


def test_nothing_listening_is_a_connection_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(APIConnectionError) as info:
        real(f"http://127.0.0.1:{port}", max_retries=0).usage.get()
    assert not isinstance(info.value, APITimeoutError)


def test_a_real_error_answer(server: tuple[str, type[Script]]) -> None:
    url, handler = server
    body = json.dumps(
        {"statusCode": 429, "name": "rate_limit_exceeded", "message": "slow"}
    ).encode()
    handler.plan = [
        {
            "status": 429,
            "headers": {
                "content-type": "application/json",
                "retry-after": "0",
                "x-request-id": "r9",
            },
            "chunks": [body],
        },
        {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "chunks": [b'{"ok":', b"true}"],
        },
    ]
    client = real(url)
    answer: Any = client.usage.get()
    assert answer == {"ok": True}
    assert [h["path"] for h in handler.seen] == ["/usage", "/usage"]
    handler.plan = [
        {
            "status": 429,
            "headers": {
                "content-type": "application/json",
                "retry-after": "120",
                "x-request-id": "r9",
            },
            "chunks": [body],
        }
    ]
    with pytest.raises(RateLimitError) as info:
        client.usage.get()
    assert (info.value.request_id, info.value.retry_after) == ("r9", 120.0)


def test_a_real_stream_is_cut_and_resumed_from_the_last_event(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server

    def event(id_: str, kind: str = "inbox.email.received") -> bytes:
        data = {
            "type": kind,
            "created_at": "2026-11-16T09:00:00.000Z",
            "inbox_id": "i",
            "data": {"n": id_},
        }
        return f"id: {id_}\nevent: {kind}\ndata: {json.dumps(data)}\n\n".encode()

    sse = {"content-type": "text/event-stream"}
    handler.plan = [
        {
            "headers": sse,
            "chunks": [b": hi\n\n", event("1"), b"retry: 10\n\n", event("2")[:20]],
            "cut": True,
        },
        {"headers": sse, "chunks": [event("2"), b": keepalive\n\n", event("3")]},
        {"headers": sse, "chunks": [event("4", "inbox.deleted")]},
    ]
    client = real(url)
    client._sleep = lambda seconds: None
    with client.inboxes.events.stream_inbox(
        "a@example.com", types=["inbox.email.received"]
    ) as stream:
        events = list(stream)
    assert [e.id for e in events if isinstance(e, InboxStreamEvent)] == ["1", "2", "3", "4"]
    assert [h.get("last-event-id") for h in handler.seen] == [None, "1", "3"]
    assert handler.seen[0]["path"] == "/inboxes/a@example.com/events?types=inbox.email.received"
    assert handler.seen[0]["accept"] == "text/event-stream"
    assert stream.last_event_id == "4"


DELETED = (
    b"id: 1\nevent: inbox.deleted\n"
    b'data: {"type":"inbox.deleted","created_at":"x","inbox_id":"i","data":{}}\n\n'
)


def test_a_real_stream_refused_with_a_retry_after_waits(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    body = json.dumps({"statusCode": 429, "name": "rate_limit_exceeded", "message": "cap"}).encode()
    sse = {"content-type": "text/event-stream"}
    handler.plan = [
        {"headers": sse, "chunks": []},
        {
            "status": 429,
            "headers": {"content-type": "application/json", "retry-after": "4"},
            "chunks": [body],
        },
        {
            "headers": sse,
            "chunks": [DELETED],
        },
    ]
    sleeps: list[float] = []
    client = real(url)
    client._sleep = sleeps.append
    with client.inboxes.events.stream_inbox("a@example.com") as stream:
        assert [e.event for e in stream] == ["inbox.deleted"]
    assert 4.0 in sleeps


def _sse_event(id_: str, kind: str = "inbox.email.received") -> bytes:
    data = {"type": kind, "created_at": "2026-11-16T09:00:00.000Z", "inbox_id": "i", "data": {}}
    return f"id: {id_}\nevent: {kind}\ndata: {json.dumps(data)}\n\n".encode()


def test_a_stream_that_goes_silent_is_reconnected_from_its_last_event(
    server: tuple[str, type[Script]],
) -> None:
    """A dropped connection that never resets (a NAT, a proxy): the server sends a keepalive
    every 15 seconds, so a read that waits longer means the connection is gone."""
    url, handler = server
    sse = {"content-type": "text/event-stream"}
    handler.plan = [
        {"headers": sse, "chunks": [b"retry: 10\n\n", _sse_event("1")], "then_hang": 3},
        {"headers": sse, "chunks": [_sse_event("2", "inbox.deleted")]},
    ]
    client = real(url)
    client._sleep = lambda seconds: None
    started = time.monotonic()
    with client.inboxes.events.stream_inbox(
        "a@example.com", timeout=httpx.Timeout(5, read=0.3)
    ) as stream:
        events = list(stream)
    assert time.monotonic() - started < 2.5
    assert [e.id for e in events if isinstance(e, InboxStreamEvent)] == ["1", "2"]
    assert [h.get("last-event-id") for h in handler.seen] == [None, "1"]


def test_a_silent_stream_without_reconnecting_raises_the_timeout(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    handler.plan = [
        {"headers": {"content-type": "text/event-stream"}, "chunks": [], "then_hang": 3}
    ]
    client = real(url)
    with pytest.raises(APITimeoutError):
        list(client.inboxes.events.stream(timeout=httpx.Timeout(5, read=0.3), max_reconnects=0))


def test_a_stream_always_has_an_idle_timeout() -> None:
    client = BillionSend(API_KEY, base_url=BASE_URL)
    floor = httpx.Timeout(5, read=STREAM_READ_TIMEOUT)
    assert client._stream_timeout(5) == floor  # a number: no read shorter than a minute
    assert client._stream_timeout(httpx.Timeout(5, read=None)) == floor  # never forever
    assert client._stream_timeout(httpx.Timeout(5, read=2)) == httpx.Timeout(5, read=2)
    assert client._stream_timeout(None).read == STREAM_READ_TIMEOUT


# ------------------------------------------------------------------ what the wire does wrong


def test_a_body_that_cannot_be_decoded_is_a_connection_error(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    bad = {"content-type": "application/json", "content-encoding": "gzip"}
    handler.plan = [{"headers": bad, "chunks": [b"this is not gzip"]}]
    with pytest.raises(APIConnectionError) as info:
        real(url, max_retries=0).usage.get()
    assert not isinstance(info.value, httpx.HTTPError)
    assert info.value.status_code is None
    assert "response" in info.value.message


def test_a_body_that_cannot_be_decoded_is_retried_like_any_lost_answer(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    bad = {"content-type": "application/json", "content-encoding": "gzip"}
    good = {"content-type": "application/json"}
    handler.plan = [
        {"headers": bad, "chunks": [b"this is not gzip"]},
        {"headers": good, "chunks": [b'{"ok":true}']},
    ]
    client = real(url)
    client._sleep = lambda seconds: None
    answer: Any = client.usage.get()
    assert answer == {"ok": True}


def test_a_refused_stream_with_an_undecodable_body_is_a_connection_error(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    bad = {"content-type": "application/json", "content-encoding": "gzip"}
    handler.plan = [{"status": 502, "headers": bad, "chunks": [b"this is not gzip"]}]
    with pytest.raises(APIConnectionError):
        real(url).inboxes.events.stream()


def test_a_stream_body_that_cannot_be_decoded_ends_as_a_dropped_connection(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    sse = {"content-type": "text/event-stream", "content-encoding": "gzip"}
    handler.plan = [{"headers": sse, "chunks": [b"this is not gzip"]}]
    stream = real(url).inboxes.events.stream(max_reconnects=0)
    with pytest.raises(APIConnectionError):
        list(stream)


# ------------------------------------------------------------------ cancelling


async def test_a_task_cancelled_before_it_runs_sends_nothing(
    async_client: AsyncBillionSend, mock: ContractMock
) -> None:
    import asyncio

    task = asyncio.ensure_future(async_client.usage.get())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mock.calls == []


async def test_a_task_cancelled_while_it_waits_to_retry_sends_no_more(
    mock: ContractMock,
) -> None:
    import asyncio

    http = httpx.AsyncClient(transport=mock.transport())
    client = AsyncBillionSend(API_KEY, base_url=BASE_URL, http_client=http)
    mock.error("GET", "/usage", 503, "service_unavailable", "busy", headers={"retry-after": "30"})
    task = asyncio.ensure_future(client.usage.get())
    while not mock.calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)  # the first answer came and the retry is waiting out 30 s
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(mock.calls) == 1
    await http.aclose()


async def test_a_timeout_around_a_call_stops_it_and_it_sends_no_more(mock: ContractMock) -> None:
    import asyncio

    http = httpx.AsyncClient(transport=mock.transport())
    client = AsyncBillionSend(API_KEY, base_url=BASE_URL, http_client=http)
    mock.error("GET", "/usage", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "30"})
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await client.usage.get()
    assert len(mock.calls) == 1
    await http.aclose()


async def test_async_a_refused_stream_with_an_undecodable_body_is_a_connection_error(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    bad = {"content-type": "application/json", "content-encoding": "gzip"}
    handler.plan = [{"status": 502, "headers": bad, "chunks": [b"this is not gzip"]}]
    http = httpx.AsyncClient(trust_env=False)
    client = AsyncBillionSend(API_KEY, base_url=url, http_client=http)
    with pytest.raises(APIConnectionError):
        await client.inboxes.events.stream()
    await http.aclose()


async def test_async_a_body_that_cannot_be_decoded_is_retried(
    server: tuple[str, type[Script]],
) -> None:
    url, handler = server
    bad = {"content-type": "application/json", "content-encoding": "gzip"}
    handler.plan = [
        {"headers": bad, "chunks": [b"this is not gzip"]},
        {"headers": {"content-type": "application/json"}, "chunks": [b'{"ok":true}']},
    ]
    http = httpx.AsyncClient(trust_env=False)
    client = AsyncBillionSend(API_KEY, base_url=url, http_client=http)

    async def no_wait(seconds: float) -> None:
        return None

    client._sleep = no_wait  # type: ignore[method-assign]
    answer: Any = await client.usage.get()
    assert answer == {"ok": True}
    await http.aclose()


@pytest.mark.parametrize("padding", ["\n", "\r\n", " ", "\t", " \n "])
def test_an_api_key_with_surrounding_whitespace_goes_out_trimmed(
    mock: ContractMock, padding: str
) -> None:
    for key in (API_KEY + padding, padding + API_KEY + padding):
        http = httpx.Client(transport=mock.transport())
        BillionSend(key, base_url=BASE_URL, http_client=http).usage.get()
        assert mock.last.request.headers["authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize("padding", ["\n", "\r\n", " ", "\t", " \n "])
async def test_async_an_api_key_with_surrounding_whitespace_goes_out_trimmed(
    mock: ContractMock, padding: str
) -> None:
    http = httpx.AsyncClient(transport=mock.transport())
    await AsyncBillionSend(
        padding + API_KEY + padding, base_url=BASE_URL, http_client=http
    ).usage.get()
    assert mock.last.request.headers["authorization"] == f"Bearer {API_KEY}"
    await http.aclose()


def test_an_api_key_from_the_environment_is_trimmed_too(
    mock: ContractMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BILLIONSEND_API_KEY", API_KEY + "\n")
    http = httpx.Client(transport=mock.transport())
    BillionSend(base_url=BASE_URL, http_client=http).usage.get()
    assert mock.last.request.headers["authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize("blank", ["", " ", "\n", " \t\r\n "])
def test_a_key_of_only_whitespace_is_no_key(
    mock: ContractMock, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("BILLIONSEND_API_KEY", API_KEY)  # the parameter wins, even when blank
    http = httpx.Client(transport=mock.transport())
    with pytest.raises(AuthenticationError) as info:
        BillionSend(blank, base_url=BASE_URL, http_client=http).usage.get()
    assert info.value.name == "missing_api_key"
    monkeypatch.setenv("BILLIONSEND_API_KEY", blank)
    with pytest.raises(AuthenticationError):
        BillionSend(base_url=BASE_URL, http_client=http).usage.get()
    assert mock.calls == []


async def test_async_a_key_of_only_whitespace_is_no_key(mock: ContractMock) -> None:
    http = httpx.AsyncClient(transport=mock.transport())
    with pytest.raises(AuthenticationError):
        await AsyncBillionSend(" \n", base_url=BASE_URL, http_client=http).usage.get()
    assert mock.calls == []
    await http.aclose()


@pytest.mark.parametrize("key", ["bs_a\nX-Evil: 1", "bs_a\rb", "bs_a\x00b", "bs_a\x7fb"])
def test_a_key_with_a_control_character_inside_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        BillionSend(key, base_url=BASE_URL)
    with pytest.raises(ValueError, match="control characters"):
        AsyncBillionSend(key, base_url=BASE_URL)
