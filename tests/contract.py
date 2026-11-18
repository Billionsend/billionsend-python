"""A contract mock of the BillionSend API built from the OpenAPI snapshot.

It sits at the transport layer (``httpx.MockTransport``) under both the sync
and async clients. For every request it:

1. finds the operation (method + path template) in the snapshot, or fails;
2. checks the API key header (absent only where the operation's security is empty);
3. validates path, query and header parameters and the JSON body against the
   operation's schemas (JSON Schema 2020-12, ``$ref`` resolved through the snapshot);
4. answers with a queued response, or else with an instance of the operation's
   success schema generated from the schema itself (and validated against it).

Every hit is recorded so the coverage test can compare what the SDK called with
what the snapshot declares.
"""

from __future__ import annotations

import copy
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi" / "api.openapi.json"
SPEC: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
BASE = "urn:billionsend:openapi"
HTTP_METHODS = ("get", "put", "post", "patch", "delete")
API_KEY = "bs_test_key"
BASE_URL = "https://api.test.billionsend.com"

OpKey = tuple[str, str]


def _absolute_refs(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (
                BASE + v
                if k == "$ref" and isinstance(v, str) and v.startswith("#")
                else _absolute_refs(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_absolute_refs(v) for v in node]
    return node


_DOC = _absolute_refs(copy.deepcopy(SPEC))
REGISTRY: Registry[Any] = Registry().with_resource(
    BASE, Resource.from_contents(_DOC, default_specification=DRAFT202012)
)


def absolute(schema: Any) -> Any:
    """A schema from the snapshot with its references made resolvable."""
    return _absolute_refs(schema)


FORMATS = FormatChecker()


def validate(instance: Any, schema: Any, where: str) -> None:
    """Check ``instance`` against ``schema``, formats (uuid, date-time, email…) included."""
    validator = Draft202012Validator(absolute(schema), registry=REGISTRY, format_checker=FORMATS)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
        raise AssertionError(f"{where} does not match the contract: {details}")


def schema_by_ref(ref: str) -> dict[str, Any]:
    node: Any = SPEC
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return dict(node)


def resolve(schema: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(schema)
    while "$ref" in out:
        out = schema_by_ref(out["$ref"])
    return out


# ------------------------------------------------------------------- operations


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    spec: dict[str, Any]
    regex: re.Pattern[str]

    @property
    def key(self) -> OpKey:
        return (self.method, self.path)

    def params(self, where: str) -> list[dict[str, Any]]:
        return [p for p in self.spec.get("parameters", []) if p["in"] == where]

    @property
    def needs_auth(self) -> bool:
        return self.spec.get("security", SPEC.get("security")) != []

    def success(self) -> tuple[int, str, dict[str, Any] | None]:
        for code, response in sorted(self.spec["responses"].items()):
            if code.startswith("2"):
                content = response.get("content", {})
                for media, body in content.items():
                    return int(code), media, body.get("schema")
                return int(code), "application/json", None
        raise LookupError(f"{self.method} {self.path} declares no success response")

    def has_success(self) -> bool:
        return any(code.startswith("2") for code in self.spec["responses"])


def _compile(path: str) -> re.Pattern[str]:
    pattern = re.sub(r"\\{(\w+)\\}", r"(?P<\1>[^/]+)", re.escape(path))
    return re.compile(f"^{pattern}$")


OPERATIONS: list[Operation] = sorted(
    (
        Operation(method.upper(), path, op, _compile(path))
        for path, item in SPEC["paths"].items()
        for method, op in item.items()
        if method in HTTP_METHODS
    ),
    # Literal segments win over templated ones (/emails/metrics before /emails/{id}).
    key=lambda o: (-sum(1 for s in o.path.split("/") if s and "{" not in s), o.path),
)
OPERATION_KEYS: set[OpKey] = {o.key for o in OPERATIONS}


def find_operation(method: str, path: str) -> tuple[Operation, dict[str, str]]:
    matched_path = False
    for op in OPERATIONS:
        m = op.regex.match(path)
        if not m:
            continue
        matched_path = True
        if op.method == method:
            return op, m.groupdict()
    reason = "method not allowed" if matched_path else "no such path"
    raise AssertionError(f"{method} {path}: not an operation of the contract ({reason})")


# ------------------------------------------------------------------- examples

_FORMATS = {
    "uuid": "0b6f5a2e-7c1d-4e8a-9f3b-2d4c6e8a0b1c",
    "date-time": "2026-01-01T00:00:00.000Z",
    "date": "2026-01-01",
    "email": "user@example.com",
    "uri": "https://example.com/",
}


def example(schema: Mapping[str, Any] | None, *, full: bool = True) -> Any:
    """A deterministic instance of ``schema``.

    Objects get every property when ``full``, else only the required ones.
    """
    if schema is None:
        return {}
    s = resolve(schema)
    if len(s.get("allOf", [])) == 1:
        return example(s["allOf"][0], full=full)
    if not ({"type", "enum", "anyOf", "oneOf", "properties"} & set(s)):
        return {}  # an unconstrained schema: any JSON value is valid
    if "anyOf" in s or "oneOf" in s:
        variants = s.get("anyOf") or s["oneOf"]
        non_null = [v for v in variants if resolve(v).get("type") != "null"]
        return example((non_null or variants)[0], full=full)
    if "enum" in s:
        values = [v for v in s["enum"] if v is not None]
        return values[0] if values else None
    types = s.get("type", "object" if "properties" in s else None)
    types = [t for t in (types if isinstance(types, list) else [types]) if t != "null"] or ["null"]
    kind = types[0]
    if kind == "string":
        if s.get("format") in _FORMATS:
            return _FORMATS[s["format"]]
        text = "value"
        return text.ljust(int(s.get("minLength", 0)), "x")[: s.get("maxLength", 10_000)]
    if kind == "integer":
        return int(s.get("minimum", 1 if s.get("maximum", 1) >= 1 else 0))
    if kind == "number":
        return float(s.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    if kind == "array":
        count = max(int(s.get("minItems", 1)), 1)
        count = min(count, int(s.get("maxItems", count)))
        return [example(s.get("items", {}), full=full) for _ in range(count)]
    if kind == "object":
        required = set(s.get("required", []))
        return {
            k: example(v, full=full)
            for k, v in s.get("properties", {}).items()
            if full or k in required
        }
    raise AssertionError(f"cannot build an example of {s!r}")


# ------------------------------------------------------------------- the mock


@dataclass
class Recorded:
    operation: Operation
    request: httpx.Request
    path_params: dict[str, str]
    query: dict[str, list[str]]
    body: Any


class ChunkStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A response body served in the given chunks, optionally failing at the end."""

    def __init__(self, chunks: Iterable[bytes], error: Exception | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks
        if self.error is not None:
            raise self.error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


def check_request(request: httpx.Request, *, base_url: str, api_key: str | None = None) -> Recorded:
    """Check a request against the snapshot and describe it.

    The URL must lie under ``base_url`` and name an operation of the contract; the
    path, query and header parameters and the JSON or multipart body must match the
    operation's schemas; the key must be sent where the operation takes one (and
    equal ``api_key`` when that is given).
    """
    assert str(request.url).startswith(base_url), f"unexpected host in {request.url}"
    raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    op, encoded = find_operation(request.method, raw_path)
    path_params = {k: unquote(v) for k, v in encoded.items()}
    where = f"{op.method} {op.path}"

    # Authentication
    auth = request.headers.get("authorization")
    if op.needs_auth:
        assert auth, f"{where}: missing Authorization header"
        assert auth.startswith("Bearer "), f"{where}: Authorization is not a Bearer token"
        if api_key is not None:
            assert auth == f"Bearer {api_key}", f"{where}: wrong Authorization header"
    else:
        assert auth is None, f"{where}: must be sent without an API key"

    # Path parameters
    for p in op.params("path"):
        assert p["name"] in path_params, f"{where}: path parameter {p['name']} missing"
        validate(path_params[p["name"]], p["schema"], f"{where} path parameter {p['name']}")

    # Query parameters
    query: dict[str, list[str]] = {}
    for name, value in request.url.params.multi_items():
        query.setdefault(name, []).append(value)
    declared = {p["name"]: p for p in op.params("query")}
    unknown = set(query) - set(declared)
    assert not unknown, f"{where}: undeclared query parameters {sorted(unknown)}"
    for name, p in declared.items():
        if name not in query:
            assert not p.get("required"), f"{where}: required query parameter {name} missing"
            continue
        validate(_coerce(query[name], p), p["schema"], f"{where} query {name}")

    # Header parameters
    for p in op.params("header"):
        value = request.headers.get(p["name"])
        if value is None:
            assert not p.get("required"), f"{where}: required header {p['name']} missing"
            continue
        validate(value, p["schema"], f"{where} header {p['name']}")

    # Body
    body: Any = None
    content = request.content
    request_body = op.spec.get("requestBody")
    if request_body is None:
        assert not content, f"{where}: takes no request body"
    elif content:
        media = request.headers.get("content-type", "")
        if "multipart/form-data" in request_body["content"]:
            assert media.startswith("multipart/form-data; boundary="), (
                f"{where}: body must be multipart/form-data with a boundary"
            )
            body = parse_multipart(media, content)
            schema = request_body["content"]["multipart/form-data"]["schema"]
        else:
            assert media.startswith("application/json"), f"{where}: body must be JSON"
            body = json.loads(content)
            schema = request_body["content"]["application/json"]["schema"]
        validate(body, schema, f"{where} request body")
    else:
        assert request_body.get("required") is not True, f"{where}: request body missing"

    return Recorded(op, request, path_params, query, body)


@dataclass
class ContractMock:
    calls: list[Recorded] = field(default_factory=list)
    queued: dict[OpKey, deque[httpx.Response | Exception]] = field(default_factory=dict)

    # -- configuration

    def respond(
        self,
        method: str,
        path: str,
        status: int = 200,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        stream: ChunkStream | None = None,
    ) -> None:
        """Queue one answer for an operation (FIFO), instead of the generated example."""
        key = (method, path)
        assert key in OPERATION_KEYS, f"{method} {path} is not in the contract"
        op = next(o for o in OPERATIONS if o.key == key)
        if json_body is not None and status >= 400:
            validate(json_body, {"$ref": "#/components/schemas/ErrorResponse"}, "queued error")
        elif json_body is not None and op.has_success():
            _, media, schema = op.success()
            if media == "application/json" and schema is not None:
                success_schema = op.spec["responses"].get(str(status), {})
                schema = success_schema.get("content", {}).get(media, {}).get("schema", schema)
                validate(json_body, schema, f"queued {method} {path} answer")
        if stream is not None:
            response = httpx.Response(status, headers=headers, stream=stream)
        elif content is not None:
            response = httpx.Response(status, headers=headers, content=content)
        else:
            response = httpx.Response(status, headers=headers, json=json_body)
        self.queued.setdefault(key, deque()).append(response)

    def error(
        self, method: str, path: str, status: int, name: str, message: str, **kw: Any
    ) -> None:
        self.respond(
            method,
            path,
            status,
            json_body={"statusCode": status, "name": name, "message": message},
            **kw,
        )

    def fail(self, method: str, path: str, error: Exception) -> None:
        """Queue a transport failure (the request reached the server, no answer came back)."""
        key = (method, path)
        assert key in OPERATION_KEYS, f"{method} {path} is not in the contract"
        self.queued.setdefault(key, deque()).append(error)

    # -- transport

    @property
    def hits(self) -> set[OpKey]:
        return {c.operation.key for c in self.calls}

    @property
    def last(self) -> Recorded:
        assert self.calls, "no request was sent"
        return self.calls[-1]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        recorded = check_request(request, base_url=BASE_URL, api_key=API_KEY)
        op = recorded.operation
        self.calls.append(recorded)

        queued = self.queued.get(op.key)
        if queued:
            answer = queued.popleft()
            if isinstance(answer, Exception):
                raise answer
            return answer
        return self.default_response(op)

    @staticmethod
    def default_response(op: Operation) -> httpx.Response:
        if not op.has_success():
            # The operation has no success answer at all; answer 422 (or its first error).
            codes = sorted(int(c) for c in op.spec["responses"])
            code = 422 if 422 in codes else codes[0]
            body = {"statusCode": code, "name": "validation_error", "message": "Not supported"}
            return httpx.Response(code, json=body)
        status, media, schema = op.success()
        if media == "text/event-stream":
            payload = (
                b'id: 1\nevent: inbox.email.received\ndata: {"type":"inbox.email.received"}\n\n'
            )
            return httpx.Response(
                status,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([payload]),
            )
        instance = example(schema)
        if schema is not None:
            validate(instance, schema, f"generated {op.method} {op.path} answer")
        return httpx.Response(status, json=instance)


def parse_multipart(content_type: str, content: bytes) -> dict[str, Any]:
    """The fields of a multipart body: a file as its content, anything else as text."""
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + content
    )
    fields: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        assert isinstance(name, str), "a multipart part without a name"
        assert name not in fields, f"the multipart field {name} was sent twice"
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        fields[name] = payload.decode("utf-8")
    return fields


def _coerce(values: list[str], param: Mapping[str, Any]) -> Any:
    s = resolve(param["schema"])
    types = s.get("type")
    types = types if isinstance(types, list) else [types]
    if "array" in types:
        # A list without explode is one comma-separated value; with it (the default), repeated keys.
        if not param.get("explode", param.get("style", "form") == "form"):
            assert len(values) == 1, f"a comma-separated list was sent {len(values)} times"
            values = values[0].split(",")
        return [_scalar(v, resolve(s.get("items", {}))) for v in values]
    assert len(values) == 1, f"a scalar parameter was sent {len(values)} times"
    return _scalar(values[0], s)


def _scalar(value: str, schema: Mapping[str, Any]) -> Any:
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "integer" in types:
        assert re.fullmatch(r"-?\d+", value), f"{value!r} is not an integer"
        return int(value)
    if "number" in types:
        return float(value)
    if "boolean" in types:
        assert value in {"true", "false"}, f"{value!r} is not a boolean"
        return value == "true"
    return value
