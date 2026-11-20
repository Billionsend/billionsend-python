"""Brand defaults, logo uploads, the unsubscribe page settings and the dormant-contact switches.

Each call is checked for its method, path, JSON body or multipart parts (read back from the
request the client really sent), and each answer for what the SDK hands back.
"""

from __future__ import annotations

import io
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any

import httpx
import pytest

from billionsend import (
    APIConnectionError,
    APIError,
    AsyncBillionSend,
    BillionSend,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from billionsend.types import Brand, UpdateBrandRequest, UpdateDomainRequest

from .contract import OPERATIONS, ContractMock

ID = "6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f"
# The PNG signature and a 1x1 IHDR: the SDK forwards the bytes, the server judges the format.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
LOGO_URL = "https://assets.billionsend.com/logos/6a1f1d38/logo.png"

BRAND: Brand = {
    "object": "brand",
    "logo_url": LOGO_URL,
    "accent_color": "#2B44FF",
    "font": "geist",
    "company_address": "Acme Inc., 1 Market St, San Francisco, CA 94105",
    "powered_by": False,
    "powered_by_locked": False,
    "show_topics": True,
    "branding_locked": False,
}


def _parts(request: httpx.Request) -> dict[str, tuple[str | None, str | None, bytes]]:
    media = request.headers["content-type"]
    assert media.startswith("multipart/form-data; boundary=")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + media.encode() + b"\r\n\r\n" + request.content
    )
    out: dict[str, tuple[str | None, str | None, bytes]] = {}
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        out[str(part.get_param("name", header="content-disposition"))] = (
            part.get_filename(),
            part.get_content_type() if part.get_filename() else None,
            payload,
        )
    return out


def _default_answer(method: str, path: str) -> dict[str, Any]:
    op = next(o for o in OPERATIONS if o.key == (method, path))
    body = ContractMock.default_response(op).json()
    assert isinstance(body, dict)
    return body


# ------------------------------------------------------------------ brand


def test_brand_get(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("GET", "/brand", json_body=BRAND)
    result = client.brand.get()
    call = mock.last
    assert (call.request.method, call.request.url.path) == ("GET", "/brand")
    assert not call.request.content
    assert result == BRAND
    assert result["font"] == "geist"
    assert result["powered_by_locked"] is False


def test_brand_get_on_a_free_plan_with_nothing_set(client: BillionSend, mock: ContractMock) -> None:
    free: Brand = {
        "object": "brand",
        "logo_url": None,
        "accent_color": None,
        "font": None,
        "company_address": None,
        "powered_by": True,
        "powered_by_locked": True,
        "show_topics": False,
        "branding_locked": True,
    }
    mock.respond("GET", "/brand", json_body=free)
    result = client.brand.get()
    assert result["logo_url"] is None
    assert result["powered_by_locked"] is True
    assert result["branding_locked"] is True


def test_brand_update_sends_only_what_is_given(client: BillionSend, mock: ContractMock) -> None:
    params: UpdateBrandRequest = {
        "accent_color": "#2B44FF",
        "font": "serif",
        "company_address": "Acme Inc., 1 Market St, San Francisco, CA 94105",
        "powered_by": False,
        "show_topics": True,
    }
    mock.respond("PATCH", "/brand", json_body={**BRAND, "font": "serif"})
    result = client.brand.update(params)
    call = mock.last
    assert (call.request.method, call.request.url.path) == ("PATCH", "/brand")
    assert call.request.headers["content-type"] == "application/json"
    assert call.body == params
    assert result["font"] == "serif"


def test_brand_update_null_clears_a_field(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "PATCH", "/brand", json_body={**BRAND, "accent_color": None, "company_address": None}
    )
    result = client.brand.update({"accent_color": None, "company_address": None})
    assert mock.last.body == {"accent_color": None, "company_address": None}
    assert result["accent_color"] is None
    assert result["company_address"] is None


def test_brand_update_without_params_is_an_empty_patch(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.respond("PATCH", "/brand", json_body=BRAND)
    assert client.brand.update() == BRAND
    assert (mock.last.request.method, mock.last.request.url.path) == ("PATCH", "/brand")


def test_brand_write_by_a_domain_scoped_key_is_refused(
    client: BillionSend, mock: ContractMock
) -> None:
    mock.error("PATCH", "/brand", 403, "restricted_api_key", "This key cannot change the brand")
    with pytest.raises(PermissionDeniedError) as info:
        client.brand.update({"show_topics": False})
    assert info.value.name == "restricted_api_key"
    assert info.value.status_code == 403


# ------------------------------------------------------------------ logo upload


def test_brand_logo_upload_from_bytes(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("PUT", "/brand/logo", json_body={"object": "logo", "logo_url": LOGO_URL})
    result = client.brand.logo.upload({"file": PNG})
    request = mock.last.request
    assert (request.method, request.url.path) == ("PUT", "/brand/logo")
    parts = _parts(request)
    assert list(parts) == ["file"]
    assert parts["file"][0] == "file"
    assert parts["file"][2] == PNG
    assert result == {"object": "logo", "logo_url": LOGO_URL}


def test_brand_logo_upload_with_file_name_and_content_type(
    client: BillionSend, mock: ContractMock
) -> None:
    client.brand.logo.upload({"file": ("acme-logo.webp", b"RIFF....WEBPVP8 ", "image/webp")})
    assert _parts(mock.last.request) == {
        "file": ("acme-logo.webp", "image/webp", b"RIFF....WEBPVP8 ")
    }


def test_brand_logo_upload_from_an_open_file(
    client: BillionSend, mock: ContractMock, tmp_path: Path
) -> None:
    path = tmp_path / "logo.png"
    path.write_bytes(PNG)
    with path.open("rb") as handle:
        client.brand.logo.upload({"file": handle})
    assert _parts(mock.last.request) == {"file": ("logo.png", "image/png", PNG)}


def test_brand_logo_upload_from_a_stream_with_a_name(
    client: BillionSend, mock: ContractMock
) -> None:
    client.brand.logo.upload({"file": ("logo.jpg", io.BytesIO(PNG), "image/jpeg")})
    assert _parts(mock.last.request) == {"file": ("logo.jpg", "image/jpeg", PNG)}


@pytest.mark.parametrize(
    ("status", "name", "error"),
    [
        (404, "not_found", NotFoundError),
        (422, "validation_error", ValidationError),
    ],
)
def test_logo_upload_errors(
    client: BillionSend,
    mock: ContractMock,
    status: int,
    name: str,
    error: type[Exception],
) -> None:
    mock.error("PUT", "/brand/logo", status, name, "The logo was refused")
    with pytest.raises(error) as info:
        client.brand.logo.upload({"file": b"not an image"})
    assert getattr(info.value, "status_code", None) == status


def test_brand_logo_delete(client: BillionSend, mock: ContractMock) -> None:
    mock.respond("DELETE", "/brand/logo", json_body={"object": "logo", "deleted": True})
    result = client.brand.logo.delete()
    request = mock.last.request
    assert (request.method, request.url.path) == ("DELETE", "/brand/logo")
    assert not request.content
    assert result == {"object": "logo", "deleted": True}


# ------------------------------------------------------------------ domain unsubscribe page


def test_domain_logo_upload(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "PUT",
        "/domains/{id}/unsubscribe-page/logo",
        json_body={"object": "logo", "logo_url": LOGO_URL},
    )
    result = client.domains.unsubscribe_page.logo.upload(
        ID, {"file": ("logo.png", PNG, "image/png")}
    )
    request = mock.last.request
    assert (request.method, request.url.path) == ("PUT", f"/domains/{ID}/unsubscribe-page/logo")
    assert _parts(request) == {"file": ("logo.png", "image/png", PNG)}
    assert result["logo_url"] == LOGO_URL


def test_domain_logo_delete(client: BillionSend, mock: ContractMock) -> None:
    mock.respond(
        "DELETE",
        "/domains/{id}/unsubscribe-page/logo",
        json_body={"object": "logo", "deleted": True},
    )
    result = client.domains.unsubscribe_page.logo.delete(ID)
    request = mock.last.request
    assert (request.method, request.url.path) == ("DELETE", f"/domains/{ID}/unsubscribe-page/logo")
    assert not request.content
    assert result == {"object": "logo", "deleted": True}


def test_domain_update_writes_support_url_and_powered_by(
    client: BillionSend, mock: ContractMock
) -> None:
    params: UpdateDomainRequest = {
        "unsubscribe_page": {"support_url": "https://acme.dev/support", "powered_by": False}
    }
    client.domains.update(ID, params)
    call = mock.last
    assert (call.request.method, call.request.url.path) == ("PATCH", f"/domains/{ID}")
    assert call.body == params


def test_domain_update_null_clears_support_url_and_follows_the_team_default(
    client: BillionSend, mock: ContractMock
) -> None:
    params: UpdateDomainRequest = {"unsubscribe_page": {"support_url": None, "powered_by": None}}
    client.domains.update(ID, params)
    assert mock.last.body == params


def test_domain_unsubscribe_page_answer_carries_the_new_fields(
    client: BillionSend, mock: ContractMock
) -> None:
    answer = _default_answer("GET", "/domains/{id}")
    answer["unsubscribe_page"] = {
        "brand_name": "Acme",
        "message": None,
        "success_message": None,
        "redirect_url": None,
        "support_url": "https://acme.dev/support",
        "powered_by": None,
        "powered_by_effective": True,
        "background_color": None,
        "text_color": None,
        "accent_color": "#2B44FF",
        "logo_radius": "rounded",
        "logo_url": None,
    }
    mock.respond("GET", "/domains/{id}", json_body=answer)
    page = client.domains.get(ID)["unsubscribe_page"]
    assert page["support_url"] == "https://acme.dev/support"
    assert page["powered_by"] is None
    assert page["powered_by_effective"] is True
    assert page["logo_url"] is None


# ------------------------------------------------------------------ dormant contacts


def test_broadcast_create_and_update_carry_the_dormant_switches(
    client: BillionSend, mock: ContractMock
) -> None:
    client.broadcasts.create(
        {
            "segment_id": ID,
            "from": "Acme <news@acme.dev>",
            "subject": "March news",
            "html": "<p>Hi</p>",
            "dormant_check": False,
            "include_dormant": True,
        }
    )
    assert mock.last.body["dormant_check"] is False
    assert mock.last.body["include_dormant"] is True
    client.broadcasts.update(ID, {"dormant_check": True, "include_dormant": False})
    call = mock.last
    assert (call.request.method, call.request.url.path) == ("PATCH", f"/broadcasts/{ID}")
    assert call.body == {"dormant_check": True, "include_dormant": False}


def test_broadcast_get_returns_the_dormant_switches(
    client: BillionSend, mock: ContractMock
) -> None:
    answer = _default_answer("GET", "/broadcasts/{id}")
    answer["dormant_check"] = True
    answer["include_dormant"] = False
    mock.respond("GET", "/broadcasts/{id}", json_body=answer)
    broadcast = client.broadcasts.get(ID)
    assert broadcast["dormant_check"] is True
    assert broadcast["include_dormant"] is False


# ------------------------------------------------------------------ retries


def test_a_logo_upload_is_not_repeated_after_a_5xx(
    client: BillionSend, mock: ContractMock, sleeps: list[float]
) -> None:
    mock.error("PUT", "/brand/logo", 503, "service_unavailable", "busy")
    with pytest.raises(APIError):
        client.brand.logo.upload({"file": PNG})
    assert len(mock.calls) == 1
    assert sleeps == []


def test_a_domain_logo_upload_is_not_repeated_after_a_lost_connection(
    client: BillionSend, mock: ContractMock, sleeps: list[float]
) -> None:
    mock.fail(
        "PUT",
        "/domains/{id}/unsubscribe-page/logo",
        httpx.ReadError("connection reset"),
    )
    with pytest.raises(APIConnectionError):
        client.domains.unsubscribe_page.logo.upload(ID, {"file": PNG})
    assert len(mock.calls) == 1


def test_a_logo_upload_is_repeated_after_a_429(
    client: BillionSend, mock: ContractMock, sleeps: list[float]
) -> None:
    mock.error(
        "PUT", "/brand/logo", 429, "rate_limit_exceeded", "slow", headers={"retry-after": "2"}
    )
    mock.respond("PUT", "/brand/logo", json_body={"object": "logo", "logo_url": LOGO_URL})
    result = client.brand.logo.upload({"file": ("logo.png", PNG, "image/png")})
    assert result["logo_url"] == LOGO_URL
    assert len(mock.calls) == 2
    assert sleeps == [2.0]
    # Both attempts carry the whole file.
    assert [_parts(c.request)["file"][2] for c in mock.calls] == [PNG, PNG]


def test_deleting_a_logo_is_repeated_after_a_5xx(
    client: BillionSend, mock: ContractMock, sleeps: list[float]
) -> None:
    mock.error("DELETE", "/brand/logo", 503, "service_unavailable", "busy")
    mock.respond("DELETE", "/brand/logo", json_body={"object": "logo", "deleted": True})
    assert client.brand.logo.delete()["deleted"] is True
    assert len(mock.calls) == 2


# ------------------------------------------------------------------ async client


async def test_async_brand(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.respond("GET", "/brand", json_body=BRAND)
    assert await async_client.brand.get() == BRAND
    mock.respond("PATCH", "/brand", json_body={**BRAND, "show_topics": False})
    result = await async_client.brand.update({"show_topics": False})
    assert mock.last.body == {"show_topics": False}
    assert result["show_topics"] is False


async def test_async_logo_uploads(async_client: AsyncBillionSend, mock: ContractMock) -> None:
    mock.respond("PUT", "/brand/logo", json_body={"object": "logo", "logo_url": LOGO_URL})
    result = await async_client.brand.logo.upload({"file": ("logo.png", PNG, "image/png")})
    assert result["logo_url"] == LOGO_URL
    assert _parts(mock.last.request) == {"file": ("logo.png", "image/png", PNG)}

    await async_client.domains.unsubscribe_page.logo.upload(ID, {"file": ("d.png", PNG)})
    request = mock.last.request
    assert request.url.path == f"/domains/{ID}/unsubscribe-page/logo"
    assert _parts(request) == {"file": ("d.png", "image/png", PNG)}

    mock.respond("DELETE", "/brand/logo", json_body={"object": "logo", "deleted": True})
    assert (await async_client.brand.logo.delete())["deleted"] is True
    await async_client.domains.unsubscribe_page.logo.delete(ID)
    assert mock.last.request.method == "DELETE"


async def test_async_logo_upload_is_not_repeated_after_a_5xx(
    async_client: AsyncBillionSend, mock: ContractMock, sleeps: list[float]
) -> None:
    mock.error("PUT", "/brand/logo", 500, "internal_server_error", "oops")
    with pytest.raises(APIError):
        await async_client.brand.logo.upload({"file": PNG})
    assert len(mock.calls) == 1
    assert sleeps == []
