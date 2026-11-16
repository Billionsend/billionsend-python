# Changelog

## 0.1.0 (Unreleased)

First release of the BillionSend Python SDK.

- `BillionSend` and `AsyncBillionSend` clients covering every operation of the BillionSend API (135), named after each operation's `operationId` and grouped into resources: emails (with receiving and attachments), domains, API keys, contacts, audiences, contact properties, segments, topics, broadcasts, templates, webhooks, suppressions, unsubscribes, verifications, usage, deliverability, inboxes (agent and safety settings, labels, threads, quarantine, codes, drafts, event streams) and agent signup.
- Request parameters and responses typed as `TypedDict`s generated from the OpenAPI document; mypy strict clean, `py.typed` included.
- Errors raised as `BillionSendError` subclasses carrying `status_code`, `name` and `message`.
- Retries on 429 and 5xx for idempotent methods and requests with an idempotency key, honouring `Retry-After`.
- Server-sent event streams (sync and async iterators) that resume with `Last-Event-ID` after the server closes them or the connection drops.
- `*_all` iterators that page through cursor lists.
