"""Python SDK for the BillionSend API.

>>> from billionsend import BillionSend
>>> client = BillionSend()  # reads BILLIONSEND_API_KEY
>>> client.emails.send({"from": "you@example.com", "to": "them@example.com",
...                     "subject": "Hello", "text": "Hi!"})  # doctest: +SKIP
"""

from . import types
from ._client import AsyncBillionSend, BillionSend
from ._exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BillionSendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)
from ._streaming import AsyncEventStream, EventStream, ServerSentEvent
from ._version import __version__

__all__ = [
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AsyncBillionSend",
    "AsyncEventStream",
    "AuthenticationError",
    "BillionSend",
    "BillionSendError",
    "ConflictError",
    "EventStream",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ServerSentEvent",
    "ValidationError",
    "__version__",
    "types",
]
