# BillionSend Python SDK

The official Python client for the [BillionSend](https://billionsend.com) API: send and receive email, run inboxes for agents and people, manage domains, contacts, broadcasts, templates, webhooks and more.

- Sync (`BillionSend`) and async (`AsyncBillionSend`) clients with the same resources and methods.
- Fully typed: request parameters and responses are `TypedDict`s, checked by mypy in strict mode.
- One dependency: [`httpx`](https://www.python-httpx.org/).
- Every operation of the API, sends that never go out twice, retries, auto-paging iterators, live event streams that resume where they stopped, and webhook signature verification.

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
    }
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

## Authentication

Pass an API key (`bs_…`) as the first argument or set `BILLIONSEND_API_KEY`. Keys are made in the dashboard or with `client.api_keys.create`. A key scoped to one inbox can only reach that inbox.

An agent can also create its own account, the one call that needs no key:

```python
from billionsend import BillionSend

signup = BillionSend().agent.signup({"human_email": "owner@example.com", "name": "Scout"})
client = BillionSend(signup["api_key"])
print(signup["inbox"]["email_address"], client.agent.status()["status"])
```

## Sending once

Every sending method (`emails.send`, `emails.batch`, `emails.receiving.reply` and `.forward`, `inboxes.send`, `inboxes.threads.emails.reply` and `.forward`, `inboxes.drafts.send`) sends an `Idempotency-Key`. Without `idempotency_key=` the call makes up one (a UUID) and reuses it for its own retries, so a retry after a dropped connection never sends the email twice. Pass your own key to stay safe across calls too, for example when a job that sends may run again:

```python
client.emails.send(message, idempotency_key=f"welcome/{user_id}")
```

Repeating a key with the same request returns the first answer; the same key with a different request is refused (`ConflictError`, `invalid_idempotent_request`). The API keeps keys for 24 hours.

## An agent that answers its inbox

Wait for new mail, read it, reply in the thread:

```python
from billionsend import BillionSend, InboxStreamEvent

client = BillionSend()
inbox = client.inboxes.create({"username": "support", "kind": "agent"})


def answer(question: str) -> str:
    return f"Thanks, we got your message:\n\n> {question}"  # your agent goes here


with client.inboxes.events.stream_inbox(inbox["id"], types="inbox.email.received") as stream:
    for event in stream:
        if not isinstance(event, InboxStreamEvent):
            continue  # a StreamGapEvent: see below
        thread_id = event.data["data"]["thread_id"]
        email_id = event.data["data"]["email_id"]
        email = client.inboxes.threads.emails.get(inbox["id"], thread_id, email_id)
        if email["direction"] != "inbound":
            continue
        question = email["extracted_text"] or email["text"] or ""
        client.inboxes.threads.emails.reply(
            inbox["id"], thread_id, email_id, {"text": answer(question)}
        )
```

`extracted_text` is the new part of a message without the quoted history. Everything in a received message was written by its sender: treat it as untrusted input. An inbox's rules may hold a reply as a draft for a person to approve; the reply then answers with the draft (`"object": "inbox_draft"`) instead of the sent email.

Waiting for a sign-in code sent to the inbox is one call: `client.inboxes.codes.latest(inbox["id"], from_="example.com", wait=30)`.

## Live events

`client.inboxes.events.stream_inbox(inbox_id)` streams one inbox's events and `client.inboxes.events.stream()` every inbox event of the team; `types=` narrows them (a name or a list). Iterating yields two kinds of events:

- `InboxStreamEvent`: `id` (the stream position), `event` (such as `"inbox.email.received"`) and `data`, a dict with `type`, `created_at`, `inbox_id` and `data` (the webhook payload of the same event).
- `StreamGapEvent`: `event == "stream.gap"`, `id` is `None` and `data["reason"]` says why. The stream was resumed from a position older than the 24 hours the API replays, so some events may be missing: catch up through the lists (`client.inboxes.threads.list_all(inbox_id, read=False)`) before going on. It is handed out once per position.

The server ends a stream every few minutes and connections drop; the stream reconnects on its own with `Last-Event-ID`, so no event is lost or repeated. `max_reconnects` bounds consecutive failed reconnects (default 5, `0` turns reconnecting off; 429 answers wait for their `Retry-After`). An error that reconnecting cannot fix, such as a revoked key, is raised from the loop. One inbox's stream ends after that inbox's `inbox.deleted` or `inbox.expired` event.

To pick up where a process left off, keep `stream.last_event_id` and pass it back:

```python
stream = client.inboxes.events.stream(last_event_id=saved_position)
```

With the async client, `await` the method and `async for` the stream; `async with` closes it.

## Webhooks

Verify a delivery before trusting it. Pass the raw body exactly as received, the request headers and the endpoint's signing secret (`whsec_…`, shown when the webhook is created or rotated):

```python
from billionsend import WebhookVerificationError, verify_webhook


# Flask; any framework works the same way
@app.post("/webhooks/billionsend")
def billionsend_webhook():
    try:
        event = verify_webhook(request.get_data(), request.headers, WEBHOOK_SECRET)
    except WebhookVerificationError:
        return "", 400
    if event["type"] == "email.bounced":
        mark_bounced(event["data"]["email_id"])
    return "", 204
```

`verify_webhook` checks the `webhook-id`, `webhook-timestamp` and `webhook-signature` headers (Standard Webhooks: HMAC-SHA256 over `id.timestamp.body`), accepts any of several signatures (both secrets sign during a rotation's overlap) and refuses a timestamp more than `tolerance` seconds (default 300) from now, which bounds replays. It returns the parsed event (`type`, `created_at`, `data`). Parse the body only after verifying: re-serialised JSON does not verify. A secret that is not a `whsec_` secret raises `ValueError`.

## Resources

| Attribute | Covers |
| --- | --- |
| `emails` | send, batch, list, get, update, cancel, delete, insights, metrics |
| `emails.receiving` (`.attachments`) | received mail: list, get, delete, reply, forward; attachments |
| `domains` | create, list, get, update, delete, verify |
| `api_keys` | create, list, delete |
| `contacts` (`.topics`, `.segments`, `.batch`) | create, list, get, update, delete, preferences_link; `batch.create`, `batch.get`, `batch.remove` |
| `audiences.contacts` | contacts addressed through an audience |
| `contact_properties`, `segments` (`.contacts`), `topics` | contact data and grouping |
| `broadcasts` | create, list, get, update, delete, send, duplicate, cancel |
| `templates` | create, list, get, update, delete, publish, duplicate |
| `webhooks` | create, list, get, update, delete, rotate |
| `suppressions` (`.batch`), `unsubscribes` | suppression list (`batch.add`, `batch.remove`) and opt-outs |
| `verifications` | availability, estimate, create, list, get |
| `usage`, `deliverability` | account usage and sending health |
| `inboxes` (`.events`) | create, list, get, update, delete, send; `events.stream`, `events.stream_inbox` |
| `inboxes.agent`, `.safety`, `.labels`, `.codes`, `.quarantine`, `.drafts` | inbox settings, labels, verification codes, held mail, drafts |
| `inboxes.threads` (`.labels`, `.emails`) | list, search, get, update, delete; thread labels; messages, reply and forward |
| `agent` | signup, status, claim_email |

Each method is named after its operation's `operationId` in the API document: `emails.receiving.attachments.list` is `client.emails.receiving.attachments.list`, with camelCase written in snake_case (`agent.claimEmail` is `client.agent.claim_email`).

Path parameters are positional (`client.domains.get(domain_id)`), the request body is a `dict` (`client.domains.create({"name": "acme.dev"})`) and query parameters are keyword arguments (`client.emails.list(limit=50)`). A parameter named after a Python keyword takes a trailing underscore (`from_=`). Every method also takes `extra_headers=` and `timeout=`.

## Pagination

List methods return one page (`{"object": "list", "data": [...], "has_more": ...}`). Every cursor list also has a `*_all` iterator (`search_all` for thread search) that fetches the following pages as you go:

```python
for contact in client.contacts.list_all(limit=100):
    print(contact["email"])

# async
async for domain in async_client.domains.list_all():
    print(domain["name"])
```

An iterator walks in the list's own order from `after` (or the first page); start it with `before` to walk the other way. It raises the same errors as the methods.

## Errors

Every error is a `BillionSendError` with the API's error shape, `status_code`, `name` and `message`, plus `request_id`, the API's id of the request (its `x-request-id` header; quote it when asking for support).

```python
from billionsend import BillionSend, BillionSendError, NotFoundError, RateLimitError

client = BillionSend()
try:
    client.domains.get("6a1f1d38-3c55-4a4e-9a57-0f2b3c4d5e6f")
except NotFoundError:
    print("no such domain")
except RateLimitError as err:
    print("retry in", err.retry_after)
except BillionSendError as err:
    print(err.status_code, err.name, err.message, err.request_id)
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
| no answer | `APIConnectionError`, and `APITimeoutError` (a subclass) for timeouts; `status_code` and `request_id` are `None` |

An answer that is not JSON keeps the same shape, with `name` set to `"application_error"`. `WebhookVerificationError` (`name` `"webhook_verification_error"`) is a `BillionSendError` too.

## Retries

A request is retried when repeating it is safe: GET, PUT and DELETE, and any request carrying an idempotency key (every send does). It is retried on 429, on 5xx, when no answer came back (connection failure, timeout), and, for a keyed request, on 409 `concurrent_idempotent_requests` (a retry that caught the first attempt still running). The wait is the answer's `Retry-After` when there is one, otherwise exponential backoff with jitter (0.5 s, 1 s, 2 s… up to 8 s). An answer asking to wait more than 60 seconds, such as a used-up daily quota, is raised at once. `max_retries` (default 2) bounds the retries of one call; `0` turns them off.

## Configuration

| Argument | Environment | Default |
| --- | --- | --- |
| `api_key` | `BILLIONSEND_API_KEY` | none (only `agent.signup` works without one) |
| `base_url` | `BILLIONSEND_BASE_URL` | `https://api.billionsend.com` |
| `timeout` | | `60` seconds per attempt, or an `httpx.Timeout` |
| `max_retries` | | `2` |
| `default_headers` | | none |
| `http_client` | | an `httpx.Client` / `httpx.AsyncClient` of your own (proxies, transports); the SDK does not close it |

Close the client with `client.close()` (`await client.close()`) or use it as a context manager.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv build
```

The SDK is built from a snapshot of the API's OpenAPI document, `openapi/api.openapi.json`. The snapshot is the contract: `scripts/generate.py` writes `src/billionsend/types.py` and the resource modules from it (method names from each `operationId`, idempotency keys where an operation declares the `Idempotency-Key` header, iterators for cursor lists), and the tests validate every request the SDK sends and every response it receives against it through a contract mock (`tests/contract.py`). A coverage test fails when an operation of the snapshot has no SDK method, or when the SDK calls something the snapshot does not declare.

When the API changes:

```bash
uv run python scripts/sync_openapi.py            # or: --from path/to/api.openapi.json
uv run python scripts/generate.py
uv run pytest
```

Whatever fails is what the SDK has to follow. CI runs `scripts/generate.py --check` so the generated code never drifts from the snapshot.

## Releasing

Publishing is manual; nothing is published on push.

1. On PyPI, the owner of the `billionsend` project adds a Trusted Publisher (for the first version, a "pending publisher" under Account → Publishing, which reserves the name until the first upload): PyPI project name `billionsend`, owner `Billionsend`, repository `billionsend-python`, workflow `release.yml`, environment `pypi`. The owner is the GitHub organization the repository lives in when the workflow runs: while it is still under another organization, the publisher names that one, or the upload is refused. In this repository's GitHub settings, create the `pypi` environment (Settings → Environments); add required reviewers there to have each release approved before it uploads.
2. Set `version` in `pyproject.toml`, turn the changelog's `Unreleased` heading into that version, commit, then tag and push: `git tag v0.1.0 && git push origin main v0.1.0`.
3. Actions → `release` → Run workflow, on the version tag. The workflow refuses a branch or a tag that differs from `pyproject.toml`, runs every check, builds the sdist and wheel, and uploads them with PyPI Trusted Publishing (no token stored).

Without Trusted Publishing, the same release from a clean checkout of the tag is `uv build && uv publish` with an API token scoped to the `billionsend` project in `UV_PUBLISH_TOKEN`.

## License

MIT
