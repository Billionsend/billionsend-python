"""HTTP plumbing shared by the sync and async clients."""

from __future__ import annotations

import asyncio
import email.utils
import json
import os
import random
import time
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ._exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    BillionSendError,
    RateLimitError,
    error_class,
)
from ._version import __version__

if TYPE_CHECKING:
    from ._streaming import AsyncEventStream, EventStream

__all__ = ["AsyncAPIClient", "SyncAPIClient", "encode_path"]

DEFAULT_BASE_URL = "https://api.billionsend.com"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
# An event stream sends a keepalive comment every 15 seconds; a read that waits
# far longer than that means the connection is gone.
STREAM_READ_TIMEOUT = 60.0
# A Retry-After longer than this is not waited out: the error is raised at once.
MAX_RETRY_DELAY = 60.0
# A retry of a keyed request can meet the first attempt still running on the server.
CONCURRENT_IDEMPOTENT = "concurrent_idempotent_requests"
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

Timeout = float | httpx.Timeout
QueryValue = str | int | float | bool | None


def encode_path(value: str) -> str:
    """Encode one path segment (an id, or an inbox address such as support@example.com)."""
    return quote(str(value), safe="@")


def encode_query(query: Mapping[str, Any] | None) -> list[tuple[str, QueryValue]]:
    """Drop unset values, write booleans as true/false and lists comma-separated in one key."""

    def text(item: Any) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        return str(item)

    pairs: list[tuple[str, QueryValue]] = []
    for key, value in (query or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.append((key, ",".join(text(item) for item in value)))
        else:
            pairs.append((key, text(value)))
    return pairs


def parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def error_from_response(response: httpx.Response) -> BillionSendError:
    """Map an error answer to the matching exception, keeping the API's error shape."""
    status = response.status_code
    name = "application_error"
    message = response.reason_phrase or f"HTTP {status}"
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = response.text.strip()
        if text:
            message = text[:500]
    else:
        if isinstance(body, dict):
            if isinstance(body.get("name"), str):
                name = body["name"]
            if isinstance(body.get("message"), str):
                message = body["message"]
    cls = error_class(status)
    if cls is RateLimitError:
        return RateLimitError(
            message,
            name=name,
            status_code=status,
            response=response,
            retry_after=parse_retry_after(response),
        )
    if cls is APIError:
        return APIError(
            message,
            name=name,
            status_code=status,
            response=response,
            retry_after=parse_retry_after(response),
        )
    return cls(message, name=name, status_code=status, response=response)


def parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        content_type = response.headers.get("content-type") or "no content type"
        raise BillionSendError(
            f"Expected a JSON answer, got {content_type}",
            name="application_error",
            status_code=response.status_code,
            response=response,
        ) from exc


def connection_error(exc: httpx.TransportError) -> APIConnectionError:
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(f"Request timed out: {exc}" if str(exc) else "Request timed out")
    return APIConnectionError(f"Connection error: {exc}" if str(exc) else "Connection error")


def _merge(headers: dict[str, str], more: Mapping[str, str]) -> None:
    """Set headers, replacing any already there under another spelling of the name."""
    for name, value in more.items():
        for existing in [k for k in headers if k.lower() == name.lower()]:
            del headers[existing]
        headers[name] = value


class BaseClient:
    api_key: str | None
    base_url: str
    timeout: Timeout
    max_retries: int

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: Timeout,
        max_retries: int,
        default_headers: Mapping[str, str] | None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be 0 or more")
        self.api_key = api_key if api_key is not None else os.environ.get("BILLIONSEND_API_KEY")
        resolved = base_url or os.environ.get("BILLIONSEND_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = resolved.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._default_headers = dict(default_headers or {})

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _headers(
        self,
        *,
        auth: bool,
        has_body: bool,
        accept: str = "application/json",
        idempotency_key: str | None = None,
        headers: Mapping[str, str | None] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        out = {"Accept": accept, "User-Agent": f"billionsend-python/{__version__}"}
        if auth:
            if not self.api_key:
                raise BillionSendError(
                    "No API key: pass api_key= or set BILLIONSEND_API_KEY",
                    name="missing_api_key",
                )
            out["Authorization"] = f"Bearer {self.api_key}"
        if has_body:
            out["Content-Type"] = "application/json"
        if idempotency_key is not None:
            out["Idempotency-Key"] = idempotency_key
        _merge(out, self._default_headers)
        _merge(out, {k: v for k, v in (headers or {}).items() if v is not None})
        _merge(out, extra_headers or {})
        return out

    def _request_headers(
        self,
        *,
        auth: bool,
        has_body: bool,
        idempotency_key: str | None,
        auto_idempotency_key: bool,
        extra_headers: Mapping[str, str] | None,
        headers: Mapping[str, str | None] | None,
    ) -> tuple[dict[str, str], bool]:
        """The headers of a request, and whether it carries an idempotency key.

        A sending operation without a key gets a fresh one, so that the retries
        of this one call share it and the API sends the email once.
        """
        out = self._headers(
            auth=auth,
            has_body=has_body,
            idempotency_key=idempotency_key,
            headers=headers,
            extra_headers=extra_headers,
        )
        keyed = any(k.lower() == "idempotency-key" for k in out)
        if auto_idempotency_key and not keyed:
            out["Idempotency-Key"] = str(uuid.uuid4())
            keyed = True
        return out, keyed

    def _retry_delay(
        self, attempt: int, error: BillionSendError, *, method: str, keyed: bool
    ) -> float | None:
        """Seconds to wait before retrying after ``error``, or None to raise it.

        Retried: requests that are safe to repeat (GET, PUT, DELETE…, or any
        request with an idempotency key) failing with 429, 5xx or no answer at
        all, and a keyed request whose first attempt is still running (409
        concurrent_idempotent_requests).
        """
        if attempt >= self.max_retries:
            return None
        if method not in IDEMPOTENT_METHODS and not keyed:
            return None
        status = error.status_code
        if isinstance(error, APIConnectionError):
            pass
        elif status == 409:
            if not (keyed and error.name == CONCURRENT_IDEMPOTENT):
                return None
        elif status is None or (status != 429 and status < 500):
            return None
        if error.response is not None:
            hinted = parse_retry_after(error.response)
            if hinted is not None:
                return hinted if hinted <= MAX_RETRY_DELAY else None
        base = min(0.5 * 2.0**attempt, 8.0)
        return base * (1 - 0.25 * random.random())  # noqa: S311 - jitter, not security

    def _build(
        self,
        http: httpx.Client | httpx.AsyncClient,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        body: Any,
        headers: dict[str, str],
        timeout: Timeout | None,
    ) -> httpx.Request:
        content = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        effective = self.timeout if timeout is None else timeout
        return http.build_request(
            method,
            self._url(path),
            params=encode_query(query),
            content=content,
            headers=headers,
            timeout=effective,
        )

    def _stream_timeout(self, timeout: Timeout | None) -> httpx.Timeout:
        chosen = self.timeout if timeout is None else timeout
        if isinstance(chosen, httpx.Timeout):
            return chosen
        return httpx.Timeout(chosen, read=max(chosen, STREAM_READ_TIMEOUT))


class SyncAPIClient(BaseClient):
    """Owns the ``httpx.Client`` and runs requests with retries."""

    _sleep: Callable[[float], None] = staticmethod(time.sleep)

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: Timeout,
        max_retries: int,
        default_headers: Mapping[str, str] | None,
        http_client: httpx.Client | None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str | None] | None = None,
        body: Any = None,
        idempotency_key: str | None = None,
        auto_idempotency_key: bool = False,
        auth: bool = True,
        extra_headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
    ) -> Any:
        all_headers, keyed = self._request_headers(
            auth=auth,
            has_body=body is not None,
            idempotency_key=idempotency_key,
            auto_idempotency_key=auto_idempotency_key,
            headers=headers,
            extra_headers=extra_headers,
        )
        attempt = 0
        while True:
            request = self._build(
                self._http,
                method,
                path,
                query=query,
                body=body,
                headers=all_headers,
                timeout=timeout,
            )
            error: BillionSendError
            cause: Exception | None = None
            try:
                response = self._http.send(request)
            except httpx.TransportError as exc:
                error, cause = connection_error(exc), exc
            else:
                if response.is_success:
                    return parse_json(response)
                error = error_from_response(response)
                response.close()
            delay = self._retry_delay(attempt, error, method=method, keyed=keyed)
            if delay is None:
                raise error from cause
            self._sleep(delay)
            attempt += 1

    def stream_events(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        last_event_id: str | None = None,
        max_reconnects: int = 5,
        extra_headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
    ) -> EventStream:
        from ._streaming import EventStream

        stream = EventStream(
            self,
            path,
            query=query,
            last_event_id=last_event_id,
            max_reconnects=max_reconnects,
            extra_headers=extra_headers,
            timeout=timeout,
        )
        stream.open()
        return stream

    def open_stream(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        last_event_id: str | None,
        extra_headers: Mapping[str, str] | None,
        timeout: Timeout | None,
    ) -> httpx.Response:
        """Open an event stream; raise the mapped error when the answer is not a stream."""
        headers = self._headers(
            auth=True,
            has_body=False,
            accept="text/event-stream",
            headers={"Last-Event-ID": last_event_id},
            extra_headers=extra_headers,
        )
        request = self._http.build_request(
            "GET",
            self._url(path),
            params=encode_query(query),
            headers=headers,
            timeout=self._stream_timeout(timeout),
        )
        try:
            response = self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            raise connection_error(exc) from exc
        if not response.is_success:
            try:
                response.read()
            finally:
                response.close()
            raise error_from_response(response)
        return response

    def close(self) -> None:
        """Close the HTTP connections (only those of a client the SDK created)."""
        if self._owns_http:
            self._http.close()


class AsyncAPIClient(BaseClient):
    """Owns the ``httpx.AsyncClient`` and runs requests with retries."""

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: Timeout,
        max_retries: int,
        default_headers: Mapping[str, str] | None,
        http_client: httpx.AsyncClient | None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str | None] | None = None,
        body: Any = None,
        idempotency_key: str | None = None,
        auto_idempotency_key: bool = False,
        auth: bool = True,
        extra_headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
    ) -> Any:
        all_headers, keyed = self._request_headers(
            auth=auth,
            has_body=body is not None,
            idempotency_key=idempotency_key,
            auto_idempotency_key=auto_idempotency_key,
            headers=headers,
            extra_headers=extra_headers,
        )
        attempt = 0
        while True:
            request = self._build(
                self._http,
                method,
                path,
                query=query,
                body=body,
                headers=all_headers,
                timeout=timeout,
            )
            error: BillionSendError
            cause: Exception | None = None
            try:
                response = await self._http.send(request)
            except httpx.TransportError as exc:
                error, cause = connection_error(exc), exc
            else:
                if response.is_success:
                    return parse_json(response)
                error = error_from_response(response)
                await response.aclose()
            delay = self._retry_delay(attempt, error, method=method, keyed=keyed)
            if delay is None:
                raise error from cause
            await self._sleep(delay)
            attempt += 1

    async def stream_events(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        last_event_id: str | None = None,
        max_reconnects: int = 5,
        extra_headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
    ) -> AsyncEventStream:
        from ._streaming import AsyncEventStream

        stream = AsyncEventStream(
            self,
            path,
            query=query,
            last_event_id=last_event_id,
            max_reconnects=max_reconnects,
            extra_headers=extra_headers,
            timeout=timeout,
        )
        await stream.open()
        return stream

    async def open_stream(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        last_event_id: str | None,
        extra_headers: Mapping[str, str] | None,
        timeout: Timeout | None,
    ) -> httpx.Response:
        headers = self._headers(
            auth=True,
            has_body=False,
            accept="text/event-stream",
            headers={"Last-Event-ID": last_event_id},
            extra_headers=extra_headers,
        )
        request = self._http.build_request(
            "GET",
            self._url(path),
            params=encode_query(query),
            headers=headers,
            timeout=self._stream_timeout(timeout),
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            raise connection_error(exc) from exc
        if not response.is_success:
            try:
                await response.aread()
            finally:
                await response.aclose()
            raise error_from_response(response)
        return response

    async def close(self) -> None:
        """Close the HTTP connections (only those of a client the SDK created)."""
        if self._owns_http:
            await self._http.aclose()
