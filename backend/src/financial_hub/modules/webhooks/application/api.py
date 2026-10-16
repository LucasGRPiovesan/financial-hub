"""Public API of webhooks for the composition roots (``main``, ``workers``): import only from here.

Outbound webhooks (hub -> products, docs/INTEGRATIONS.md s3). Other modules never call webhooks:
they publish events, and endpoints subscribe to them through the event log.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.webhooks.application import events as _events  # registers webhook.test
from financial_hub.modules.webhooks.application.config import WebhookSettings
from financial_hub.modules.webhooks.application.sender import SendStatus, WebhookSender
from financial_hub.modules.webhooks.domain import TargetPolicy
from financial_hub.modules.webhooks.infrastructure.http_client import GuardedSender
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.settings import DEV_ENVIRONMENTS, Settings
from financial_hub.shared.clock import Clock

__all__ = [
    "SendStatus",
    "WebhookSender",
    "WebhookSettings",
    "build_sender",
    "target_policy",
]

del _events


def target_policy(
    settings: Settings, webhook_settings: WebhookSettings | None = None
) -> TargetPolicy:
    """https only and no private targets, except in dev/test (http + ``FH_WEBHOOK_ALLOW_HOSTS``)."""
    config = webhook_settings or WebhookSettings()
    return config.target_policy(dev=settings.app_env in DEV_ENVIRONMENTS)


def build_sender(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    keyring: Keyring,
    clock: Clock,
    webhook_settings: WebhookSettings | None = None,
) -> WebhookSender:
    config = webhook_settings or WebhookSettings()
    client = GuardedSender(target_policy(settings, config), timeout_seconds=config.timeout_seconds)
    return WebhookSender(
        session_factory,
        keyring=keyring,
        clock=clock,
        client=client,
        batch_size=config.batch_size,
        lease_seconds=config.lease_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
    )
