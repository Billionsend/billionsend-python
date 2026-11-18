"""The BillionSend clients."""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType

import httpx

from ._base_client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    AsyncAPIClient,
    SyncAPIClient,
    Timeout,
)
from .resources import (
    AgentResource,
    ApiKeysResource,
    AsyncAgentResource,
    AsyncApiKeysResource,
    AsyncAudiencesResource,
    AsyncAutomationsResource,
    AsyncBroadcastsResource,
    AsyncContactPropertiesResource,
    AsyncContactsResource,
    AsyncDeliverabilityResource,
    AsyncDomainsResource,
    AsyncEmailsResource,
    AsyncEventsResource,
    AsyncInboxesResource,
    AsyncLogsResource,
    AsyncOauthGrantsResource,
    AsyncSegmentsResource,
    AsyncSuppressionsResource,
    AsyncTemplatesResource,
    AsyncTopicsResource,
    AsyncUnsubscribesResource,
    AsyncUsageResource,
    AsyncVerificationsResource,
    AsyncWebhooksResource,
    AudiencesResource,
    AutomationsResource,
    BroadcastsResource,
    ContactPropertiesResource,
    ContactsResource,
    DeliverabilityResource,
    DomainsResource,
    EmailsResource,
    EventsResource,
    InboxesResource,
    LogsResource,
    OauthGrantsResource,
    SegmentsResource,
    SuppressionsResource,
    TemplatesResource,
    TopicsResource,
    UnsubscribesResource,
    UsageResource,
    VerificationsResource,
    WebhooksResource,
)

__all__ = ["AsyncBillionSend", "BillionSend"]


class BillionSend(SyncAPIClient):
    """Synchronous client of the BillionSend API.

    Args:
        api_key: an API key (``bs_…``); defaults to ``BILLIONSEND_API_KEY``. Only
            ``agent.signup`` works without one.
        base_url: defaults to ``BILLIONSEND_BASE_URL``, then ``https://api.billionsend.com``.
        timeout: seconds (or an ``httpx.Timeout``) for each request; 60 by default.
        max_retries: retries of a request answered 429 or 5xx, for idempotent methods
            and requests carrying an idempotency key; 2 by default.
        default_headers: headers sent with every request.
        http_client: an ``httpx.Client`` to send requests with (proxies, transports…).
            The SDK does not close a client it did not create.
    """

    emails: EmailsResource
    domains: DomainsResource
    api_keys: ApiKeysResource
    contacts: ContactsResource
    audiences: AudiencesResource
    contact_properties: ContactPropertiesResource
    segments: SegmentsResource
    topics: TopicsResource
    broadcasts: BroadcastsResource
    templates: TemplatesResource
    webhooks: WebhooksResource
    suppressions: SuppressionsResource
    unsubscribes: UnsubscribesResource
    verifications: VerificationsResource
    usage: UsageResource
    deliverability: DeliverabilityResource
    inboxes: InboxesResource
    agent: AgentResource
    automations: AutomationsResource
    events: EventsResource
    logs: LogsResource
    oauth_grants: OauthGrantsResource

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            http_client=http_client,
        )
        self.emails = EmailsResource(self)
        self.domains = DomainsResource(self)
        self.api_keys = ApiKeysResource(self)
        self.contacts = ContactsResource(self)
        self.audiences = AudiencesResource(self)
        self.contact_properties = ContactPropertiesResource(self)
        self.segments = SegmentsResource(self)
        self.topics = TopicsResource(self)
        self.broadcasts = BroadcastsResource(self)
        self.templates = TemplatesResource(self)
        self.webhooks = WebhooksResource(self)
        self.suppressions = SuppressionsResource(self)
        self.unsubscribes = UnsubscribesResource(self)
        self.verifications = VerificationsResource(self)
        self.usage = UsageResource(self)
        self.deliverability = DeliverabilityResource(self)
        self.inboxes = InboxesResource(self)
        self.agent = AgentResource(self)
        self.automations = AutomationsResource(self)
        self.events = EventsResource(self)
        self.logs = LogsResource(self)
        self.oauth_grants = OauthGrantsResource(self)

    def __enter__(self) -> BillionSend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncBillionSend(AsyncAPIClient):
    """Asynchronous client of the BillionSend API; same resources and methods as
    :class:`BillionSend`, awaited. Takes the same arguments, with an
    ``httpx.AsyncClient`` as ``http_client``."""

    emails: AsyncEmailsResource
    domains: AsyncDomainsResource
    api_keys: AsyncApiKeysResource
    contacts: AsyncContactsResource
    audiences: AsyncAudiencesResource
    contact_properties: AsyncContactPropertiesResource
    segments: AsyncSegmentsResource
    topics: AsyncTopicsResource
    broadcasts: AsyncBroadcastsResource
    templates: AsyncTemplatesResource
    webhooks: AsyncWebhooksResource
    suppressions: AsyncSuppressionsResource
    unsubscribes: AsyncUnsubscribesResource
    verifications: AsyncVerificationsResource
    usage: AsyncUsageResource
    deliverability: AsyncDeliverabilityResource
    inboxes: AsyncInboxesResource
    agent: AsyncAgentResource
    automations: AsyncAutomationsResource
    events: AsyncEventsResource
    logs: AsyncLogsResource
    oauth_grants: AsyncOauthGrantsResource

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
            http_client=http_client,
        )
        self.emails = AsyncEmailsResource(self)
        self.domains = AsyncDomainsResource(self)
        self.api_keys = AsyncApiKeysResource(self)
        self.contacts = AsyncContactsResource(self)
        self.audiences = AsyncAudiencesResource(self)
        self.contact_properties = AsyncContactPropertiesResource(self)
        self.segments = AsyncSegmentsResource(self)
        self.topics = AsyncTopicsResource(self)
        self.broadcasts = AsyncBroadcastsResource(self)
        self.templates = AsyncTemplatesResource(self)
        self.webhooks = AsyncWebhooksResource(self)
        self.suppressions = AsyncSuppressionsResource(self)
        self.unsubscribes = AsyncUnsubscribesResource(self)
        self.verifications = AsyncVerificationsResource(self)
        self.usage = AsyncUsageResource(self)
        self.deliverability = AsyncDeliverabilityResource(self)
        self.inboxes = AsyncInboxesResource(self)
        self.agent = AsyncAgentResource(self)
        self.automations = AsyncAutomationsResource(self)
        self.events = AsyncEventsResource(self)
        self.logs = AsyncLogsResource(self)
        self.oauth_grants = AsyncOauthGrantsResource(self)

    async def __aenter__(self) -> AsyncBillionSend:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
