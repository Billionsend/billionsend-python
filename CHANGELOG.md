# Changelog

## 0.2.1 (2026-11-19)

The OpenAPI snapshot follows the published document again (the API's 2026-11-19 release). No method or type signature changes.

- 403 `restricted_api_key` is declared on every operation a restricted key can be refused on (103 operations gained a 403 response, 74 had the refused action appended to its description); only the public `POST /agent/signup` has none. The contract tests check it for both clients.
- `emails.receiving.list(inbox_id=...)` takes an inbox ID or the inbox's email address, as the `/inboxes/{inbox_id}` paths do. An unknown address, or one of another team, answers 404; a malformed one 422. A test holds the request shape for both forms.
- API behaviour, not an SDK change: any path ending in `/` answers 404 `not_found` instead of 401 `missing_api_key`. The SDK still refuses an empty path parameter before sending.

## 0.2.0 (2026-11-19)

The OpenAPI snapshot is now the published document (https://billionsend.com/docs/api.openapi.json); the SDK had been built against an older one, so `usage.get()` and a number of newer calls did not match the API. Every operation of the published API (180) is covered.

- New operations: automations (`automations.create`, `list`, `get`, `update`, `remove`, `validate`, `duplicate`, `simulate`, `stop`, `metrics`; `automations.runs.list`, `get`, `cancel`), custom events (`events.create`, `list`, `get`, `update`, `remove`, `send`), contact imports (`contacts.imports.create` as a multipart CSV upload, `list`, `get`), `contacts.segments.list`, domain claims (`domains.claims.create`, `get`, `verify`), `emails.share` and `unshare`, `emails.attachments.list` and `get`, `emails.metrics.clients`, `countries` and `links` (`client.emails.metrics(...)` stays callable), `broadcasts.recipients` and `clicked_links`, `logs.list` and `get`, `oauth_grants.list` and `revoke`, `webhooks.events.list`, `get`, `replay` and `events.attempts.list`, `webhooks.rotate_signing_secret`, `api_keys.update` and `inboxes.agent.update`.
- Removed: `unsubscribes.delete` (a recipient's opt-out can no longer be deleted through the API) and `webhooks.rotate` (replaced by `webhooks.rotate_signing_secret`).
- `usage.get()` returns the new shape (`emails.daily`, `emails.monthly`, `contacts`, `segments`, `broadcasts`, `domains`, `inboxes`, `rate_limit`, …) instead of the old `plan` / `limits` / `today` / `period` shape.
- The request and response types of 26 schemas follow the published document (contacts, domains, broadcasts, webhooks, templates, inboxes and more).
- Multipart request bodies: a binary field (`FileUpload`) takes `bytes`, a binary file object or a `(file name, content[, content type])` tuple, and a JSON field (`JsonText`, such as `column_map` of a contact import) takes a `dict` or `list` (encoded by the SDK) or a `str` (sent as it is).
- `scripts/sync_openapi.py --check` fails when the snapshot differs from the published document; CI runs it on every push and pull request and once a day.
- An empty path parameter (`client.emails.get("")`) raises `ValueError` before any request. It used to send `GET /emails/`, which the API answers with 401 `missing_api_key` even for a valid key, so the error sent you to check the key instead of the empty id.
- Event streams: an `id:` line only counts once its block is complete (so a block cut off midway never skips an event), and the position follows an id sent without data as soon as it is read, so a connection that drops before the first event, or between two, resumes from the server's own starting position and loses nothing; a stream's read timeout never waits forever (an `httpx.Timeout` with `read=None` gets the one-minute idle limit), so a connection that went silent is reconnected.
- A success with no body (a 204, or an empty 200) returns `None` instead of raising `application_error`; a body that cannot be decoded (a broken gzip) raises `APIConnectionError` and is retried like a lost answer, instead of leaking `httpx.DecodingError`.
- Without an API key, the call raises `AuthenticationError` (`name` `"missing_api_key"`, no `status_code`, nothing sent), not the base `BillionSendError`.
- An API key with surrounding whitespace (a trailing newline from a file or `.env`) is trimmed before it goes into the header, a key of only whitespace counts as none (`AuthenticationError`), and a control character inside a key raises `ValueError` instead of reaching the header.
- A path parameter of `.` or `..` raises `ValueError` before any request, like an empty one (a URL folds them away and the request would go to another route).
- `verify_webhook` accepts a secret and headers with surrounding whitespace (a secret read from a file ends in a newline) and a signature list separated by any whitespace; a file name with a line break in an upload becomes `%0D` / `%0A` instead of ending its header line.
- The README's agent example no longer fails with a `KeyError` when the inbox it watches is deleted or expires: those events reach every stream whatever `types=` asks for.
- A 429 is retried for every method, and waits as its `Retry-After` says (seconds or an HTTP date; more than 60 seconds raises at once, none means the usual backoff): the API answers it before it writes anything. It used to fail at once for a POST, PATCH or DELETE without an idempotency key. 5xx and lost connections are still retried only for safe methods and keyed requests.
- A header value with a non-ASCII or control character (`Idempotency-Key: welcome/订单-1`, a line break inside a key) raises `BillionSendError` (`name` `"invalid_header"`, the message names the header and never its value) before any request, and is not retried, instead of surfacing as a connection error and retrying a keyed send.
- Tests: every operation is checked through the sync and the async client against the published schemas (formats included) for its request, its answers and each documented error; every Python example of the README is run; CI fails when line or branch coverage of the hand-written runtime is under 95 % or a generated method is never called; `e2e/` is a live suite (never run by CI) that checks every real answer against the published document.

## 0.1.1 (2026-11-17)

- Cursor iterators (`list_all`, `search_all`) raise `BillionSendError` (`application_error`, no status) "The list page has no data array" for a page without a `data` array, instead of a `KeyError`; a page with `has_more` whose edge item (the last one forward, the first one backward) has no non-empty string `id` hands out its items, then raises "The last (first) item of the list page has no id to continue from", instead of paging from a wrong cursor.
- Event streams: a reconnect refused with a `retry-after` (a 429 for the stream cap, a 5xx) waits as it asks, in full up to a minute (it was cut to 30 seconds and read only on a 429); one asking for more than a minute raises that error at once.
- `APIError` (5xx) carries `retry_after`, like `RateLimitError`.

## 0.1.0 (2026-11-17)

First release of the BillionSend Python SDK.

- `BillionSend` and `AsyncBillionSend` clients covering every operation of the BillionSend API (136), named after each operation's `operationId` and grouped into resources: emails (with receiving, attachments and their extracted text), domains, API keys, contacts, audiences, contact properties, segments, topics, broadcasts, templates, webhooks, suppressions, unsubscribes, verifications, usage, deliverability, inboxes (agent and safety settings, labels, threads, quarantine, codes, drafts, event streams) and agent signup.
- Request parameters and responses typed as `TypedDict`s generated from the OpenAPI document; mypy strict clean, `py.typed` included.
- Errors raised as `BillionSendError` subclasses carrying `status_code`, `name`, `message` and `request_id` (the API's `x-request-id`).
- Sending methods always send an `Idempotency-Key`: the caller's, or a UUID made up for the call and shared by its retries.
- Retries for requests safe to repeat (GET, PUT, DELETE and keyed requests) on 429, 5xx, failures without an answer and 409 `concurrent_idempotent_requests`, honouring `Retry-After` up to 60 seconds, else exponential backoff with jitter.
- Server-sent event streams (sync and async iterators) yielding typed `InboxStreamEvent` and `StreamGapEvent` objects, resuming with `Last-Event-ID` after the server closes them or the connection drops.
- `*_all` iterators that page through every cursor list.
- `verify_webhook` checks a webhook delivery's signature and timestamp and returns the parsed event.
