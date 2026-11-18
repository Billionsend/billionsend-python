"""Verify the signature of a webhook delivery.

BillionSend signs every delivery the Standard Webhooks way: the secret is
``whsec_`` followed by base64 of 24 to 64 bytes, the signed content is
``<webhook-id>.<webhook-timestamp>.<body>``, and ``webhook-signature`` holds
``v1,<base64 HMAC-SHA256>``, several space-separated during a secret rotation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from ._exceptions import WebhookVerificationError

__all__ = ["DEFAULT_TOLERANCE", "WebhookEvent", "verify_webhook"]

DEFAULT_TOLERANCE = 300
"""Seconds a delivery's timestamp may differ from the receiver's clock."""

SECRET_PREFIX = "whsec_"  # noqa: S105 - a prefix, not a secret
SECRET_MIN_BYTES = 24
SECRET_MAX_BYTES = 64


class WebhookEvent(TypedDict):
    """A webhook delivery's body."""

    type: str
    """The event name, such as ``"email.delivered"``."""
    created_at: str
    """When it happened (ISO 8601)."""
    data: dict[str, Any]
    """What it is about; its fields depend on ``type``."""


def _secret_key(secret: str) -> bytes:
    """Key bytes of a ``whsec_`` secret: canonical base64 of 24 to 64 bytes, else ValueError."""
    secret = secret.strip()  # read from a file or an environment variable: a trailing newline
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("A webhook secret starts with whsec_")
    encoded = secret[len(SECRET_PREFIX) :]
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("A webhook secret is whsec_ followed by base64") from exc
    if not SECRET_MIN_BYTES <= len(key) <= SECRET_MAX_BYTES:
        raise ValueError("A webhook secret holds 24 to 64 bytes")
    if base64.b64encode(key).decode("ascii") != encoded:
        raise ValueError("A webhook secret is whsec_ followed by base64")
    return key


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """A header's value, whatever the case of its name in ``headers``."""
    for key, value in headers.items():
        if key.lower() == name and value.strip():
            return value.strip()
    return None


def verify_webhook(
    payload: str | bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> WebhookEvent:
    """Check a webhook delivery and return its parsed body.

    Args:
        payload: the request body exactly as received (bytes or text), before any
            JSON parsing: re-serialised JSON does not verify.
        headers: the request headers (any mapping; names are matched case-insensitively).
            ``webhook-id``, ``webhook-timestamp`` and ``webhook-signature`` are read.
        secret: the endpoint's signing secret (``whsec_…``), as shown when the
            webhook was created or rotated.
        tolerance: seconds the delivery's timestamp may be off the current time,
            either way (300 by default); this bounds replays of a captured delivery.
        now: the current Unix time, for tests.

    Raises:
        WebhookVerificationError: a header is missing, the timestamp is outside
            the tolerance, no signature matches, or the body is not a JSON object.
        ValueError: ``secret`` is not a webhook secret.
    """
    key = _secret_key(secret)
    msg_id = _header(headers, "webhook-id")
    timestamp = _header(headers, "webhook-timestamp")
    signatures = _header(headers, "webhook-signature")
    if msg_id is None or timestamp is None or signatures is None:
        raise WebhookVerificationError("Missing webhook-id, webhook-timestamp or webhook-signature")
    if not timestamp.isascii() or not timestamp.isdigit():
        raise WebhookVerificationError("Invalid webhook-timestamp")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance:
        raise WebhookVerificationError("Webhook timestamp is outside the tolerance")

    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    for candidate in signatures.split():
        version, _, encoded = candidate.partition(",")
        if version != "v1" or not encoded:
            continue
        try:
            given = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if hmac.compare_digest(given, expected):
            break
    else:
        raise WebhookVerificationError("No webhook signature matches")

    try:
        event = json.loads(body)
    except ValueError as exc:
        raise WebhookVerificationError("Webhook body is not JSON") from exc
    if not isinstance(event, dict):
        raise WebhookVerificationError("Webhook body is not a JSON object")
    return cast("WebhookEvent", event)
