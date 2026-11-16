# Changelog

## 0.1.0 (Unreleased)

First release of the BillionSend Python SDK.

- `BillionSend` and `AsyncBillionSend` clients covering every operation of the BillionSend API (135), named after each operation's `operationId` and grouped into resources: emails (with receiving and attachments), domains, API keys, contacts, audiences, contact properties, segments, topics, broadcasts, templates, webhooks, suppressions, unsubscribes, verifications, usage, deliverability, inboxes (agent and safety settings, labels, threads, quarantine, codes, drafts, event streams) and agent signup.
- Request parameters and responses typed as `TypedDict`s generated from the OpenAPI document; mypy strict clean, `py.typed` included.
- Errors raised as `BillionSendError` subclasses carrying `status_code`, `name`, `message` and `request_id` (the API's `x-request-id`).
- Sending methods always send an `Idempotency-Key`: the caller's, or a UUID made up for the call and shared by its retries.
- Retries for requests safe to repeat (GET, PUT, DELETE and keyed requests) on 429, 5xx, failures without an answer and 409 `concurrent_idempotent_requests`, honouring `Retry-After` up to 60 seconds, else exponential backoff with jitter.
- Server-sent event streams (sync and async iterators) yielding typed `InboxStreamEvent` and `StreamGapEvent` objects, resuming with `Last-Event-ID` after the server closes them or the connection drops.
- `*_all` iterators that page through every cursor list.
- `verify_webhook` checks a webhook delivery's signature and timestamp and returns the parsed event.
