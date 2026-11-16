# BillionSend Python SDK

The official Python client for the [BillionSend](https://billionsend.com) API: send and receive email, run inboxes for agents and people, manage domains, contacts, broadcasts, templates, webhooks and more.

- Sync (`BillionSend`) and async (`AsyncBillionSend`) clients with the same resources and methods.
- Fully typed: request parameters and responses are `TypedDict`s, checked by mypy in strict mode.
- One dependency: [`httpx`](https://www.python-httpx.org/).
- Every operation of the API, server-sent event streams with automatic resume, auto-paging iterators and safe retries.

Documentation: https://billionsend.com/docs (Python guide: https://billionsend.com/docs/sdks/python).

## Install

```bash
pip install billionsend
# or
uv add billionsend
```

Python 3.11 or newer.

## Quick start

```python
from billionsend import BillionSend

client = BillionSend()  # reads BILLIONSEND_API_KEY

email = client.emails.send(
    {
        "from": "Acme <hello@acme.dev>",
        "to": ["ada@example.com"],
        "subject": "Welcome to Acme",
        "html": "<p>Thanks for signing up!</p>",
    },
    idempotency_key="welcome/ada",
)
print(email["id"])
```

Responses are plain `dict`s typed as `TypedDict`s (`billionsend.types`), so editors complete the fields and mypy checks them.

### Async

```python
import asyncio

from billionsend import AsyncBillionSend


async def main() -> None:
    async with AsyncBillionSend() as client:
        email = await client.emails.get("6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f")
        print(email["subject"])


asyncio.run(main())
```

### An inbox and its live events

```python
from billionsend import BillionSend

client = BillionSend()
inbox = client.inboxes.create({"username": "support", "kind": "agent"})

with client.inboxes.stream_inbox_events(inbox["id"], types=["inbox.email.received"]) as stream:
    for event in stream:
        print(event.id, event.event, event.data)
```

A stream reconnects on its own when the server ends it (it does so every few minutes) or the connection drops, sending `Last-Event-ID` so no event is lost. `max_reconnects` bounds consecutive failed reconnects (default 5, `0` turns reconnecting off), and `stream.last_event_id` holds the position to resume from later (`last_event_id=`). `client.inboxes.stream_events()` streams every inbox of the team. With the async client, `await` the method and `async for` the stream.

### An agent signs itself up

```python
from billionsend import BillionSend

signup = BillionSend().agent.signup({"human_email": "owner@example.com", "name": "Scout"})
client = BillionSend(signup["api_key"])
print(signup["inbox"]["email_address"], client.agent.status()["status"])
```

`agent.signup` is the one call that needs no API key.

## Resources

| Attribute | Covers |
| --- | --- |
| `emails` | send, send_batch, list, get, update, cancel, delete, insights, metrics |
| `emails.receiving` (`.attachments`) | received mail: list, get, delete, reply, forward; attachments |
| `domains` | create, list, get, update, delete, verify |
| `api_keys` | create, list, delete |
| `contacts` (`.topics`, `.segments`) | create, list, get, update, delete, create_batch, get_batch, remove_batch, preferences_link |
| `audiences.contacts` | contacts addressed through an audience |
| `contact_properties`, `segments` (`.contacts`), `topics` | contact data and grouping |
| `broadcasts` | create, list, get, update, delete, send, duplicate, cancel |
| `templates` | create, list, get, update, delete, publish, duplicate |
| `webhooks` | create, list, get, update, delete, rotate |
| `suppressions`, `unsubscribes` | suppression list (with batch add / remove) and opt-outs |
| `verifications` | availability, estimate, create, list, get |
| `usage`, `deliverability` | account usage and sending health |
| `inboxes` | create, list, get, update, delete, send, stream_events, stream_inbox_events |
| `inboxes.agent`, `.safety`, `.labels`, `.codes`, `.quarantine`, `.drafts` | inbox settings, labels, verification codes, held mail, drafts |
| `inboxes.threads` (`.labels`, `.emails`) | list, search, get, update, delete; thread labels; reply and forward |
| `agent` | signup, status, claim_email |

Path parameters are positional (`client.domains.get(domain_id)`), the request body is a `dict` (`client.domains.create({"name": "acme.dev"})`) and query parameters are keyword arguments (`client.emails.list(limit=50)`). A parameter named after a Python keyword takes a trailing underscore (`from_=`). Every method also takes `extra_headers=` and `timeout=`.

### Pagination

List methods return one page (`{"object": "list", "data": [...], "has_more": ...}`). Each cursor list also has a `*_all` iterator that fetches the following pages as you go:

```python
for contact in client.contacts.list_all(limit=100):
    print(contact["email"])

# async
async for domain in async_client.domains.list_all():
    print(domain["name"])
```

### Idempotency

Sending methods (`emails.send`, `emails.send_batch`, replies, forwards, inbox sends and draft sends) take `idempotency_key=`, sent as the `Idempotency-Key` header: a retried request with the same key is not sent twice.

## Errors

Every error is a `BillionSendError` with the API's error shape: `status_code`, `name` and `message`.

```python
from billionsend import BillionSend, NotFoundError, RateLimitError, BillionSendError

client = BillionSend()
try:
    client.domains.get("6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f")
except NotFoundError:
    print("no such domain")
except RateLimitError as err:
    print("retry in", err.retry_after)
except BillionSendError as err:
    print(err.status_code, err.name, err.message)
```

| Status | Exception |
| --- | --- |
| 400, 422 | `ValidationError` |
| 401 | `AuthenticationError` |
| 403 | `PermissionDeniedError` |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 429 | `RateLimitError` (`retry_after` in seconds) |
| 5xx | `APIError` |
| no answer | `APIConnectionError`, `APITimeoutError` (`status_code` is `None`) |

An answer that is not JSON keeps the same shape, with `name` set to `"application_error"`.

## Configuration

| Argument | Environment | Default |
| --- | --- | --- |
| `api_key` | `BILLIONSEND_API_KEY` | none (only `agent.signup` works without one) |
| `base_url` | `BILLIONSEND_BASE_URL` | `https://api.billionsend.com` |
| `timeout` | | `60` seconds, or an `httpx.Timeout` |
| `max_retries` | | `2` |
| `default_headers` | | none |
| `http_client` | | an `httpx.Client` / `httpx.AsyncClient` of your own (proxies, transports); the SDK does not close it |

Retries apply to answers 429 and 5xx, only for idempotent methods (GET, PUT, DELETE) and for requests that carry an idempotency key, with exponential backoff that honours `Retry-After`. Close the client with `client.close()` (`await client.close()`) or use it as a context manager.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv build
```

The SDK is built from a snapshot of the API's OpenAPI document, `openapi/api.openapi.json`. The snapshot is the contract: `scripts/generate.py` writes `src/billionsend/types.py` and the resource modules from it, and the tests validate every request the SDK sends and every response it receives against it through a contract mock (`tests/contract.py`). A coverage test fails when an operation of the snapshot has no SDK method, or when the SDK calls something the snapshot does not declare.

When the API changes:

```bash
uv run python scripts/sync_openapi.py            # or: --from path/to/api.openapi.json
uv run python scripts/generate.py
uv run pytest
```

Whatever fails is what the SDK has to follow (a new operation also needs a line in `OPERATIONS` in `scripts/generate.py`). CI runs `scripts/generate.py --check` so the generated code never drifts from the snapshot.

## License

MIT
