"""Every operation of the published API, through the sync and the async client.

The expectations are written here, from the contract alone (the OpenAPI snapshot),
not derived from the SDK's own code: where each argument must land on the wire, what
a success answer returns unchanged, and which exception each documented error becomes.
The contract mock additionally checks every request and every queued answer against
the snapshot's schemas.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest

from billionsend import (
    APIError,
    AuthenticationError,
    BillionSendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)

from .api import Api, snake
from .contract import HTTP_METHODS, SPEC, ContractMock, example, resolve

AWKWARD = "a b/c+d@e.com é#?%25&=;,:"


@dataclass
class Spec:
    method: str
    path: str
    operation_id: str
    raw: dict[str, Any]
    path_params: list[dict[str, Any]] = field(default_factory=list)
    query_params: list[dict[str, Any]] = field(default_factory=list)
    header_params: list[dict[str, Any]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def stream(self) -> bool:
        return any(
            "text/event-stream" in r.get("content", {}) for r in self.raw["responses"].values()
        )

    @property
    def json_body(self) -> dict[str, Any] | None:
        body = self.raw.get("requestBody")
        if body and "application/json" in body["content"]:
            return dict(body["content"]["application/json"]["schema"])
        return None

    @property
    def is_form(self) -> bool:
        body = self.raw.get("requestBody") or {}
        return "multipart/form-data" in body.get("content", {})

    @property
    def body_required(self) -> bool:
        body = self.raw.get("requestBody") or {}
        schema = resolve(self.json_body or {})
        return bool(body.get("required")) or bool(
            schema.get("required") or "array" in _types(schema)
        )

    def success_codes(self) -> list[tuple[int, dict[str, Any] | None]]:
        out: list[tuple[int, dict[str, Any] | None]] = []
        for code, response in sorted(self.raw["responses"].items()):
            if code.startswith("2"):
                media = response.get("content", {}).get("application/json")
                out.append((int(code), media["schema"] if media else None))
        return out

    def error_codes(self) -> list[int]:
        return sorted(int(c) for c in self.raw["responses"] if not c.startswith("2"))

    @property
    def cursor_list(self) -> bool:
        names = {p["name"] for p in self.query_params}
        if self.method != "GET" or not {"after", "before"} <= names:
            return False
        for _, schema in self.success_codes():
            page = resolve(schema or {})
            if "has_more" in page.get("properties", {}):
                return True
        return False


def _types(schema: dict[str, Any]) -> list[str]:
    t = schema.get("type")
    return list(t) if isinstance(t, list) else [t] if t else []


def _specs() -> list[Spec]:
    out: list[Spec] = []
    for path, item in SPEC["paths"].items():
        for method, raw in item.items():
            if method not in HTTP_METHODS:
                continue
            spec = Spec(method.upper(), path, raw["operationId"], raw)
            for p in raw.get("parameters", []):
                {
                    "path": spec.path_params,
                    "query": spec.query_params,
                    "header": spec.header_params,
                }[p["in"]].append(p)
            order = re.findall(r"{(\w+)}", path)
            spec.path_params.sort(key=lambda p: order.index(p["name"]))
            out.append(spec)
    return out


SPECS = _specs()
IDS = [s.id for s in SPECS]
NON_FORM = [s for s in SPECS if not s.is_form]


def native(schema: dict[str, Any]) -> Any:
    """A valid value for a parameter's schema."""
    return example(schema)


def wire(value: Any) -> str:
    """How a query value must read on the wire."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(wire(v) for v in value)
    return str(value)


def kwarg_name(method: Any, name: str) -> str:
    candidates = [name, name + "_", snake(name), snake(name.removeprefix("x-")).replace("-", "_")]
    parameters = inspect.signature(method).parameters
    for candidate in candidates:
        if candidate in parameters:
            return candidate
    raise AssertionError(f"{method} takes no argument for {name}: {list(parameters)}")


def path_values(spec: Spec) -> list[str]:
    values = []
    for i, p in enumerate(spec.path_params):
        schema = resolve(p["schema"])
        values.append(native(schema) if "format" in schema else f"{AWKWARD}{i}")
    return values


def required_query(spec: Spec) -> dict[str, Any]:
    return {p["name"]: native(p["schema"]) for p in spec.query_params if p.get("required")}


def invoke(
    api: Api,
    spec: Spec,
    *,
    query: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    body: Any = None,
    values: list[str] | None = None,
    suffix: str = "",
) -> Any:
    method = api.method(spec.operation_id)
    if suffix:
        method = getattr(method.__self__, method.__name__ + suffix)
    positional: list[Any] = list(values if values is not None else path_values(spec))
    if spec.json_body is not None and not suffix:
        positional.append(body if body is not None else example(spec.json_body, full=False))
    elif spec.is_form:
        positional.append({"file": ("a.csv", b"Email\na@example.com\n")})
    kwargs: dict[str, Any] = {}
    for name, value in {**required_query(spec), **(query or {}), **(headers or {})}.items():
        if spec.stream and name == "last_event_id":
            kwargs["last_event_id"] = value
        else:
            kwargs[kwarg_name(method, name)] = value
    if spec.stream:
        kwargs["max_reconnects"] = 0
    return api.settle(method(*positional, **kwargs))


# ------------------------------------------------------------------ the request


@pytest.mark.parametrize("spec", NON_FORM, ids=lambda s: s.id)
def test_the_request_lands_where_the_contract_puts_it(
    api: Api, mock: ContractMock, spec: Spec
) -> None:
    values = path_values(spec)
    body = example(spec.json_body, full=False) if spec.json_body is not None else None
    invoke(api, spec, values=values, body=body)

    request = mock.last.request
    assert request.method == spec.method
    # Each path parameter is one segment that reads back as the value given (a "/" in an
    # id must be escaped, a "?" or "#" must not end the path), and nothing else is added.
    raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    expected: list[str] = []
    given = iter(values)
    for segment in spec.path.split("/")[1:]:
        expected.append(next(given) if re.fullmatch(r"{\w+}", segment) else segment)
    assert [unquote(s) for s in raw_path.split("/")[1:]] == expected
    assert mock.last.path_params == {
        p["name"]: v for p, v in zip(spec.path_params, values, strict=True)
    }
    # No query parameter that was not asked for, the required ones as the contract writes them.
    assert mock.last.query == {k: [wire(v)] for k, v in required_query(spec).items()}
    # The body is exactly what was passed, as JSON.
    if spec.json_body is not None:
        assert mock.last.body == body
        assert request.headers["content-type"] == "application/json"
    else:
        assert request.content == b""
        assert "content-type" not in request.headers
    # Authorization is the key, except on the one call that takes none.
    if spec.path == "/agent/signup":
        assert "authorization" not in request.headers
    else:
        assert request.headers["authorization"].startswith("Bearer bs_")
    assert request.headers["user-agent"].startswith("billionsend-python/")
    accept = "text/event-stream" if spec.stream else "application/json"
    assert request.headers["accept"] == accept


def _parameter_cases() -> list[Any]:
    cases = []
    for spec in NON_FORM:
        for p in spec.query_params:
            cases.append(pytest.param(spec, p, id=f"{spec.id} ?{p['name']}"))
        for p in spec.header_params:
            cases.append(pytest.param(spec, p, id=f"{spec.id} {p['name']}:"))
    return cases


@pytest.mark.parametrize(("spec", "param"), _parameter_cases())
def test_every_query_and_header_parameter_is_sent(
    api: Api, mock: ContractMock, spec: Spec, param: dict[str, Any]
) -> None:
    name = param["name"]
    value = native(param["schema"])
    if name == "last_event_id" and spec.stream:
        invoke(api, spec, query={name: value})
        assert mock.last.request.headers["last-event-id"] == str(value)
        return
    if param["in"] == "query":
        invoke(api, spec, query={name: value})
        assert mock.last.query[name] == [wire(value)]
        assert set(mock.last.query) == {name, *required_query(spec)}
    else:
        invoke(api, spec, headers={name: value})
        assert mock.last.request.headers[name] == str(value)


_MAIL = {
    "from": "Acme <onboarding@acme.dev>",
    "to": ["success@simulator.amazonses.com", "success+2@simulator.amazonses.com"],
    "cc": ["cc@example.com"],
    "bcc": ["bcc@example.com"],
    "reply_to": ["reply@example.com"],
    "subject": "Héllo ✓",
    "html": "<p>Hi</p>",
    "text": "Hi",
    "scheduled_at": "2026-12-01T09:00:00.000Z",
    "tags": [{"name": "kind", "value": "welcome"}],
    "headers": {"X-Entity-Ref-ID": "42"},
    "attachments": [{"filename": "a.txt", "content": "aGVsbG8="}],
}
# The send bodies cannot carry every field at once (html next to a template is refused),
# so these two are written out.
CURATED_BODIES: dict[str, Any] = {"POST /emails": _MAIL, "POST /emails/batch": [_MAIL, _MAIL]}


@pytest.mark.parametrize(
    "spec", [s for s in NON_FORM if s.json_body is not None], ids=lambda s: s.id
)
def test_a_full_body_goes_out_unchanged(api: Api, mock: ContractMock, spec: Spec) -> None:
    body = CURATED_BODIES.get(spec.id) or example(spec.json_body, full=True)
    invoke(api, spec, body=body)
    assert mock.last.body == body


@pytest.mark.parametrize(
    "spec",
    [s for s in NON_FORM if s.json_body is not None and not s.body_required],
    ids=lambda s: s.id,
)
def test_an_optional_body_may_be_left_out(api: Api, mock: ContractMock, spec: Spec) -> None:
    method = api.method(spec.operation_id)
    api.settle(
        method(
            *path_values(spec),
            **{kwarg_name(method, n): v for n, v in required_query(spec).items()},
        )
    )
    assert mock.last.request.content in (b"", b"{}")


@pytest.mark.parametrize(
    "spec",
    [s for s in SPECS if any(p["name"] == "Idempotency-Key" for p in s.header_params)],
    ids=lambda s: s.id,
)
def test_sending_operations_always_carry_a_key(api: Api, mock: ContractMock, spec: Spec) -> None:
    invoke(api, spec)
    generated = mock.last.request.headers["idempotency-key"]
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", generated
    )
    invoke(api, spec)
    assert mock.last.request.headers["idempotency-key"] != generated
    invoke(api, spec, headers={"Idempotency-Key": "mine/1"})
    assert mock.last.request.headers.get_list("idempotency-key") == ["mine/1"]


def test_the_only_header_parameters_are_the_ones_the_sdk_knows() -> None:
    names = {p["name"] for s in SPECS for p in s.header_params}
    assert names == {"Idempotency-Key", "x-batch-validation"}


# ----------------------------------------------------------------- the answer


@pytest.mark.parametrize("spec", [s for s in SPECS if not s.stream], ids=lambda s: s.id)
def test_every_success_answer_comes_back_unchanged(
    api: Api, mock: ContractMock, spec: Spec
) -> None:
    for status, schema in spec.success_codes():
        body = example(schema)
        mock.respond(spec.method, spec.path, status, json_body=body)
        assert invoke(api, spec) == body
    # Fields the SDK has no type for and null values survive the trip.
    first = dict(example(spec.success_codes()[0][1]))
    if isinstance(first, dict) and first:
        first["a_field_added_later"] = {"nested": [1, None]}
        first[next(iter(first))] = None
        mock.respond(
            spec.method,
            spec.path,
            spec.success_codes()[0][0],
            content=json.dumps(first).encode(),
            headers={"content-type": "application/json"},
        )
        assert invoke(api, spec) == first


EXPECTED_ERROR: dict[int, type[BillionSendError]] = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    410: BillionSendError,
    422: ValidationError,
    429: RateLimitError,
    503: APIError,
}


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.id)
def test_every_documented_error_becomes_its_exception(
    api: Api, mock: ContractMock, spec: Spec
) -> None:
    api.client.max_retries = 0
    for status in spec.error_codes():
        mock.error(
            spec.method,
            spec.path,
            status,
            f"name_{status}",
            f"message {status}",
            headers={"x-request-id": f"req-{status}", "retry-after": "7"},
        )
        with pytest.raises(BillionSendError) as info:
            invoke(api, spec)
        err = info.value
        assert type(err) is EXPECTED_ERROR[status], status
        assert (err.status_code, err.name, err.message) == (
            status,
            f"name_{status}",
            f"message {status}",
        )
        assert err.request_id == f"req-{status}"
        if isinstance(err, RateLimitError | APIError):
            assert err.retry_after == 7.0


# ----------------------------------------------------------------- pagination

LISTS = [s for s in SPECS if s.cursor_list]


def test_the_cursor_lists_are_the_ones_the_contract_declares() -> None:
    assert len(LISTS) == 33


def _page(spec: Spec, ids: list[str], has_more: bool) -> dict[str, Any]:
    schema = next(s for _, s in spec.success_codes() if s)
    body: dict[str, Any] = example(schema)
    body["data"] = [{**body["data"][0], "id": i} for i in ids]
    body["has_more"] = has_more
    return body


@pytest.mark.parametrize("spec", LISTS, ids=lambda s: s.id)
def test_every_cursor_list_can_be_walked_both_ways(
    api: Api, mock: ContractMock, spec: Spec
) -> None:
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 6)]
    for direction, cursor_name in (("forward", "after"), ("backward", "before")):
        mock.calls.clear()
        mock.respond(spec.method, spec.path, json_body=_page(spec, ids[:2], True))
        mock.respond(spec.method, spec.path, json_body=_page(spec, ids[2:5], False))
        start = {} if direction == "forward" else {"before": ids[4]}
        items = invoke(api, spec, query={"limit": 2, **start}, suffix="_all")
        assert [i["id"] for i in items] == ids
        assert len(mock.calls) == 2
        extra = required_query(spec)
        first = {k: [wire(v)] for k, v in {**extra, "limit": 2, **start}.items()}
        assert mock.calls[0].query == first
        next_cursor = ids[1] if direction == "forward" else ids[0]
        assert mock.calls[1].query == {**first, cursor_name: [next_cursor]}


# ----------------------------------------------------- path values in the middle


def test_an_empty_path_parameter_is_refused_before_any_request(
    api: Api, mock: ContractMock
) -> None:
    spec = next(s for s in SPECS if s.id == "GET /contacts/{id}")
    with pytest.raises(ValueError, match="path parameter"):
        invoke(api, spec, values=[""])
    assert mock.calls == []
