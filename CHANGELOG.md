# Changelog

## 0.2.0 (2026-11-19)

The OpenAPI snapshot is now the published document (https://billionsend.com/docs/api.openapi.json); the SDK had been built against an older one, so `usage.get()` and a number of newer calls did not match the API. Every operation of the published API (180) is covered.

- New operations: automations (`automations.create`, `list`, `get`, `update`, `remove`, `validate`, `duplicate`, `simulate`, `stop`, `metrics`; `automations.runs.list`, `get`, `cancel`), custom events (`events.create`, `list`, `get`, `update`, `remove`, `send`), contact imports (`contacts.imports.create` as a multipart CSV upload, `list`, `get`), `contacts.segments.list`, domain claims (`domains.claims.create`, `get`, `verify`), `emails.share` and `unshare`, `emails.attachments.list` and `get`, `emails.metrics.clients`, `countries` and `links` (`client.emails.metrics(...)` stays callable), `broadcasts.recipients` and `clicked_links`, `logs.list` and `get`, `oauth_grants.list` and `revoke`, `webhooks.events.list`, `get`, `replay` and `events.attempts.list`, `webhooks.rotate_signing_secret`, `api_keys.update` and `inboxes.agent.update`.
- Removed: `unsubscribes.delete` (a recipient's opt-out can no longer be deleted through the API) and `webhooks.rotate` (replaced by `webhooks.rotate_signing_secret`).
- `usage.get()` returns the new shape (`emails.daily`, `emails.monthly`, `contacts`, `segments`, `broadcasts`, `domains`, `inboxes`, `rate_limit`, …) instead of the old `plan` / `limits` / `today` / `period` shape.
- The request and response types of 26 schemas follow the published document (contacts, domains, broadcasts, webhooks, templates, inboxes and more).
- Multipart request bodies: a binary field (`FileUpload`) takes `bytes`, a binary file object or a `(file name, content[, content type])` tuple, and a JSON field (`JsonText`, such as `column_map` of a contact import) takes a `dict` or `list` (encoded by the SDK) or a `str` (sent as it is).
- `scripts/sync_openapi.py --check` fails when the snapshot differs from the published document; CI runs it on every push and pull request and once a day.

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
