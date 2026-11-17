# Changelog

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
