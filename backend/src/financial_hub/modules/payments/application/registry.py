"""Provider registry: which adapter serves a provider account (ADR-0012, ARCHITECTURE s5).

Adapters are registered by provider *name* with a factory that receives the account id and its
decrypted credentials. Credentials are passed in by the caller (this module never reads the
vault, import-linter contract 5) and are never logged or kept beyond the adapter they build. The
``fake`` provider needs no account or credentials and exists only when settings enable it.
"""

import secrets
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.payments.domain import (
    FAKE_PROVIDER,
    PaymentProvider,
    ProviderCredentials,
    ProviderUnavailable,
)
from financial_hub.modules.payments.infrastructure.asaas_provider import (
    PROVIDER_NAME as ASAAS_PROVIDER,
)
from financial_hub.modules.payments.infrastructure.asaas_provider import asaas_factory
from financial_hub.modules.payments.infrastructure.fake_provider import FakePaymentProvider
from financial_hub.modules.projects.application.api import load_provider_secrets
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.settings import Settings

type ProviderFactory = Callable[[UUID, ProviderCredentials], PaymentProvider]
# Loads one account's decrypted credentials on the caller's session (``None``: not available).
type CredentialsLoader = Callable[[AsyncSession, UUID, UUID], Awaitable[ProviderCredentials | None]]


class ProviderRegistry:
    def __init__(
        self,
        *,
        fake: PaymentProvider | None = None,
        factories: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._fake = fake
        self._factories: dict[str, ProviderFactory] = dict(factories or {})

    @property
    def fake(self) -> PaymentProvider | None:
        """The fake provider instance (tests script it), or ``None`` when disabled."""
        return self._fake

    @property
    def fake_enabled(self) -> bool:
        return self._fake is not None

    def register(self, provider: str, factory: ProviderFactory) -> None:
        if provider == FAKE_PROVIDER:
            raise ValueError("the fake provider is configured by settings, not registered")
        self._factories[provider] = factory

    def supports(self, provider: str) -> bool:
        return self.fake_enabled if provider == FAKE_PROVIDER else provider in self._factories

    def resolve(
        self,
        provider: str,
        account_id: UUID | None,
        credentials: ProviderCredentials | None,
    ) -> PaymentProvider:
        """The adapter for one account; :class:`ProviderUnavailable` when it cannot be built."""
        if provider == FAKE_PROVIDER:
            if self._fake is None:
                raise ProviderUnavailable("fake_provider_disabled")
            return self._fake
        factory = self._factories.get(provider)
        if factory is None:
            raise ProviderUnavailable("provider_not_supported")
        if account_id is None or credentials is None:
            raise ProviderUnavailable("credentials_unavailable")
        return factory(account_id, credentials)


def build_registry(
    settings: Settings, *, asaas_transport: httpx.AsyncBaseTransport | None = None
) -> ProviderRegistry:
    """The process registry: the ``asaas`` adapter (sandbox only) and, when allowed, the fake.

    ``asaas_transport`` replaces the network in tests (``httpx.MockTransport``).
    """
    factories = {ASAAS_PROVIDER: asaas_factory(transport=asaas_transport)}
    if not settings.fake_payment_provider_enabled:
        return ProviderRegistry(factories=factories)
    # a per-process namespace: fake ids restart at 1 in each process but are unique in the database
    fake = FakePaymentProvider(namespace=secrets.token_hex(4))
    return ProviderRegistry(fake=fake, factories=factories)


def projects_credentials_loader(keyring: Keyring) -> CredentialsLoader:
    """A :class:`CredentialsLoader` backed by the projects module's single credentials door.

    Payments never decrypts anything itself (import-linter contract 5): the secrets arrive from
    ``projects.application.api`` already limited to *active* accounts of the asking project, with
    the account's ``environment`` added so an adapter can refuse an environment it is not wired for.
    """

    async def load(
        session: AsyncSession, project_id: UUID, account_id: UUID
    ) -> ProviderCredentials | None:
        loaded = await load_provider_secrets(
            session, keyring, project_id=project_id, account_id=account_id
        )
        if loaded is None:
            return None
        return ProviderCredentials({**loaded.values, "environment": loaded.environment})

    return load
