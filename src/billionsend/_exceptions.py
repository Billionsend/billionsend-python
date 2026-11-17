"""Errors raised by the BillionSend client.

Every error carries the API's error shape: ``status_code``, ``name`` and
``message``, plus the ``request_id`` the API answered with (its
``x-request-id`` header). A failure that never produced an API answer (a
network error, a timeout, a response that is not JSON) is turned into the same
shape, with ``name`` ``"application_error"``.
"""

from __future__ import annotations

import httpx

__all__ = [
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AuthenticationError",
    "BillionSendError",
    "ConflictError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ValidationError",
    "WebhookVerificationError",
]


class BillionSendError(Exception):
    """Base class of every error the SDK raises.

    Attributes:
        status_code: the HTTP status of the answer, or ``None`` when there was none.
        name: the API's error name, such as ``"validation_error"``.
        message: a human-readable explanation.
        request_id: the API's id of the request (``x-request-id``), when it sent one.
        response: the HTTP response, when there was one.
    """

    status_code: int | None
    name: str
    message: str
    request_id: str | None
    response: httpx.Response | None

    def __init__(
        self,
        message: str,
        *,
        name: str = "application_error",
        status_code: int | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.name = name
        self.status_code = status_code
        self.response = response
        self.request_id = response.headers.get("x-request-id") if response is not None else None

    def __str__(self) -> str:
        status = f"{self.status_code} " if self.status_code is not None else ""
        return f"{status}{self.name}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code!r}, "
            f"name={self.name!r}, message={self.message!r}, request_id={self.request_id!r})"
        )


class APIConnectionError(BillionSendError):
    """The request never got an answer: DNS, connection, TLS or protocol failure."""

    def __init__(self, message: str = "Connection error") -> None:
        super().__init__(message, name="application_error", status_code=None)


class APITimeoutError(APIConnectionError):
    """The request timed out."""

    def __init__(self, message: str = "Request timed out") -> None:
        super().__init__(message)


class ValidationError(BillionSendError):
    """400 or 422: the request was refused as invalid."""


class AuthenticationError(BillionSendError):
    """401: the API key is missing, malformed or revoked."""


class PermissionDeniedError(BillionSendError):
    """403: the key may not do this."""


class NotFoundError(BillionSendError):
    """404: no such resource."""


class ConflictError(BillionSendError):
    """409: the request conflicts with the current state (or an idempotency key)."""


class RateLimitError(BillionSendError):
    """429: too many requests; ``retry_after`` holds the server's hint in seconds."""

    retry_after: float | None

    def __init__(
        self,
        message: str,
        *,
        name: str = "rate_limit_exceeded",
        status_code: int | None = 429,
        response: httpx.Response | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, name=name, status_code=status_code, response=response)
        self.retry_after = retry_after


class APIError(BillionSendError):
    """5xx: the API failed to answer the request; ``retry_after`` holds the server's hint in
    seconds, when it sent one."""

    retry_after: float | None

    def __init__(
        self,
        message: str,
        *,
        name: str = "application_error",
        status_code: int | None = None,
        response: httpx.Response | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, name=name, status_code=status_code, response=response)
        self.retry_after = retry_after


class WebhookVerificationError(BillionSendError):
    """A webhook delivery whose signature, timestamp or headers do not check out."""

    def __init__(self, message: str) -> None:
        super().__init__(message, name="webhook_verification_error", status_code=None)


_BY_STATUS: dict[int, type[BillionSendError]] = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}


def error_class(status_code: int) -> type[BillionSendError]:
    if status_code in _BY_STATUS:
        return _BY_STATUS[status_code]
    if status_code == 429:
        return RateLimitError
    if status_code >= 500:
        return APIError
    return BillionSendError
