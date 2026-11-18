"""Webhook signature verification, against the API's signing vectors.

The first vector is the one the API's own signing test pins
(packages/core/test/webhooks.test.ts, the published Standard Webhooks vector);
the rotation vector was signed with Node's crypto the way the API signs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from billionsend import BillionSendError, WebhookVerificationError, verify_webhook

SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
MSG_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
TIMESTAMP = 1614265330
PAYLOAD = '{"test": 2432232314}'
SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

HEADERS = {
    "webhook-id": MSG_ID,
    "webhook-timestamp": str(TIMESTAMP),
    "webhook-signature": SIGNATURE,
}

ROTATED = "whsec_BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
ROTATION_PAYLOAD = (
    '{"type":"email.delivered","created_at":"2026-11-16T09:00:00.000Z","data":{"email_id":"e1"}}'
)
ROTATION_SIGNATURE = (
    "v1,VfgCmhkRJMAi9nv9R1rsE49fWcTpLQ8gcMN9e7uGbrk= "
    "v1,IRvb7GZmh00vSNTRcSQ0MTb0qfbEKpRQlNS8r3daFnQ="
)


def test_published_vector() -> None:
    event: dict[str, object] = dict(verify_webhook(PAYLOAD, HEADERS, SECRET, now=TIMESTAMP))
    assert event == {"test": 2432232314}


def test_bytes_payload_and_any_header_case() -> None:
    headers = {k.upper(): v for k, v in HEADERS.items()}
    assert verify_webhook(PAYLOAD.encode(), headers, SECRET, now=TIMESTAMP)


@pytest.mark.parametrize("secret", [ROTATED, SECRET])
def test_either_secret_verifies_during_a_rotation(secret: str) -> None:
    headers = {
        "webhook-id": "msg_rot",
        "webhook-timestamp": "1700000000",
        "webhook-signature": ROTATION_SIGNATURE,
    }
    event = verify_webhook(ROTATION_PAYLOAD, headers, secret, now=1_700_000_000)
    assert event["type"] == "email.delivered"
    assert event["data"] == {"email_id": "e1"}


def test_tampered_body_fails() -> None:
    with pytest.raises(WebhookVerificationError, match="No webhook signature matches") as info:
        verify_webhook(PAYLOAD + " ", HEADERS, SECRET, now=TIMESTAMP)
    assert isinstance(info.value, BillionSendError)
    assert info.value.name == "webhook_verification_error"
    assert info.value.status_code is None


def test_other_secret_fails() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(PAYLOAD, HEADERS, ROTATED, now=TIMESTAMP)


@pytest.mark.parametrize("skew", [301, -301])
def test_timestamp_outside_the_tolerance_fails(skew: int) -> None:
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_webhook(PAYLOAD, HEADERS, SECRET, now=TIMESTAMP + skew)


@pytest.mark.parametrize("skew", [300, -300])
def test_timestamp_at_the_tolerance_passes(skew: int) -> None:
    assert verify_webhook(PAYLOAD, HEADERS, SECRET, now=TIMESTAMP + skew)


def test_custom_tolerance() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(PAYLOAD, HEADERS, SECRET, tolerance=10, now=TIMESTAMP + 11)


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_missing_header_fails(missing: str) -> None:
    headers = {k: v for k, v in HEADERS.items() if k != missing}
    with pytest.raises(WebhookVerificationError, match="Missing"):
        verify_webhook(PAYLOAD, headers, SECRET, now=TIMESTAMP)


@pytest.mark.parametrize("timestamp", ["abc", "1614265330.5", "-1614265330", "١٦١٤٢٦٥٣٣٠"])
def test_malformed_timestamp_fails(timestamp: str) -> None:
    with pytest.raises(WebhookVerificationError, match="timestamp"):
        verify_webhook(PAYLOAD, {**HEADERS, "webhook-timestamp": timestamp}, SECRET, now=TIMESTAMP)


@pytest.mark.parametrize(
    "signature",
    [
        "v2,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        "v1,",
        "v1,not base64!",
        "g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        "v1,AAAA",
    ],
)
def test_unusable_signatures_fail(signature: str) -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(PAYLOAD, {**HEADERS, "webhook-signature": signature}, SECRET, now=TIMESTAMP)


def test_one_good_signature_among_several_passes() -> None:
    headers = {**HEADERS, "webhook-signature": f"v1,AAAA v2,x {SIGNATURE}"}
    assert verify_webhook(PAYLOAD, headers, SECRET, now=TIMESTAMP)


def test_body_that_is_not_a_json_object_fails() -> None:
    body = "[1,2]"
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    digest = hmac.new(key, f"{MSG_ID}.{TIMESTAMP}.{body}".encode(), hashlib.sha256).digest()
    headers = {**HEADERS, "webhook-signature": "v1," + base64.b64encode(digest).decode()}
    with pytest.raises(WebhookVerificationError, match="JSON object"):
        verify_webhook(body, headers, SECRET, now=TIMESTAMP)


@pytest.mark.parametrize(
    "secret",
    [
        "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "whsec_",
        "whsec_short",
        "whsec_" + base64.b64encode(b"x" * 23).decode(),
        "whsec_" + base64.b64encode(b"x" * 65).decode(),
        "whsec_-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7",
        "whsec_not base64 at all!!",
    ],
)
def test_invalid_secret_is_a_value_error(secret: str) -> None:
    with pytest.raises(ValueError, match="webhook secret"):
        verify_webhook(PAYLOAD, HEADERS, secret, now=TIMESTAMP)


def test_a_secret_with_non_canonical_base64_is_refused() -> None:
    canonical = base64.b64encode(b"x" * 25).decode()
    assert canonical.endswith("A==")
    sloppy = "whsec_" + canonical[:-3] + "B=="  # same bytes, trailing bits set
    assert base64.b64decode(sloppy.removeprefix("whsec_"), validate=True) == b"x" * 25
    with pytest.raises(ValueError, match="webhook secret"):
        verify_webhook(PAYLOAD, HEADERS, sloppy, now=TIMESTAMP)


def _signed(body: str, secret: str = SECRET, msg_id: str = MSG_ID) -> dict[str, str]:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{TIMESTAMP}.{body}".encode(), hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(TIMESTAMP),
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }


def test_a_correctly_signed_body_that_is_not_json_fails() -> None:
    with pytest.raises(WebhookVerificationError, match="not JSON"):
        verify_webhook("not json", _signed("not json"), SECRET, now=TIMESTAMP)


def test_the_body_is_signed_as_bytes_whatever_its_characters() -> None:
    body = '{"type":"email.sent","created_at":"x","data":{"subject":"héllo ✓ \\u00e9"}}'
    assert verify_webhook(body, _signed(body), SECRET, now=TIMESTAMP)["type"] == "email.sent"
    assert (
        verify_webhook(body.encode(), _signed(body), SECRET, now=TIMESTAMP)["type"] == "email.sent"
    )
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body.replace("é", "e"), _signed(body), SECRET, now=TIMESTAMP)


def test_an_empty_body_does_not_verify_as_json() -> None:
    with pytest.raises(WebhookVerificationError, match="not JSON"):
        verify_webhook("", _signed(""), SECRET, now=TIMESTAMP)


def test_signature_header_whitespace_and_the_message_id_matter() -> None:
    padded = {**HEADERS, "webhook-signature": f"  {SIGNATURE}   v1,AAAA  "}
    assert verify_webhook(PAYLOAD, padded, SECRET, now=TIMESTAMP)
    other_id = {**HEADERS, "webhook-id": "msg_other"}
    with pytest.raises(WebhookVerificationError):
        verify_webhook(PAYLOAD, other_id, SECRET, now=TIMESTAMP)


def test_the_secret_needs_its_prefix_in_exact_case() -> None:
    with pytest.raises(ValueError, match="starts with whsec_"):
        verify_webhook(PAYLOAD, HEADERS, SECRET.replace("whsec_", "WHSEC_"), now=TIMESTAMP)


@pytest.mark.parametrize("padding", ["\n", "\r\n", " ", "\t", "  \n"])
def test_a_secret_read_from_a_file_or_an_env_var_may_carry_whitespace(padding: str) -> None:
    assert verify_webhook(PAYLOAD, HEADERS, SECRET + padding, now=TIMESTAMP)
    assert verify_webhook(PAYLOAD, HEADERS, padding + SECRET + padding, now=TIMESTAMP)


@pytest.mark.parametrize("padding", [" ", "\t", "\r\n", " \n "])
def test_header_values_are_trimmed(padding: str) -> None:
    headers = {k: padding + v + padding for k, v in HEADERS.items()}
    assert verify_webhook(PAYLOAD, headers, SECRET, now=TIMESTAMP)


@pytest.mark.parametrize("gap", ["  ", "\t", "\n", "\r\n", " \t "])
def test_signatures_are_separated_by_any_whitespace(gap: str) -> None:
    headers = {**HEADERS, "webhook-signature": f"v1,AAAA{gap}v2,x{gap}{SIGNATURE}"}
    assert verify_webhook(PAYLOAD, headers, SECRET, now=TIMESTAMP)
    headers = {**HEADERS, "webhook-signature": f"{SIGNATURE}{gap}v1,AAAA"}
    assert verify_webhook(PAYLOAD, headers, SECRET, now=TIMESTAMP)


def test_a_header_of_only_whitespace_is_a_missing_header() -> None:
    for name in HEADERS:
        with pytest.raises(WebhookVerificationError, match="Missing"):
            verify_webhook(PAYLOAD, {**HEADERS, name: " \n"}, SECRET, now=TIMESTAMP)


def test_trimming_does_not_let_a_changed_message_id_through() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(PAYLOAD, {**HEADERS, "webhook-id": " msg_other "}, SECRET, now=TIMESTAMP)
