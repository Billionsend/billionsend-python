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
    WebhookVerificationError,
)
from ._streaming import (
    AsyncEventStream,
    EventStream,
    InboxStreamEvent,
    InboxStreamEventData,
    ServerSentEvent,
    StreamEvent,
    StreamGapData,
    StreamGapEvent,
)
from ._version import __version__
from ._webhooks import WebhookEvent, verify_webhook

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
    "InboxStreamEvent",
    "InboxStreamEventData",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ServerSentEvent",
    "StreamEvent",
    "StreamGapData",
    "StreamGapEvent",
    "ValidationError",
    "WebhookEvent",
    "WebhookVerificationError",
    "__version__",
    "types",
    "verify_webhook",
]
