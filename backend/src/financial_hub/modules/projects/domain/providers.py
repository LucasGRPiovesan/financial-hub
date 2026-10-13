"""Provider accounts: which payment providers exist and what credentials each one needs (pure).

Credentials are validated here, serialised to canonical JSON and encrypted by the application
(``platform.crypto``); they are never part of any domain value that leaves the process.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

CREDENTIAL_VALUE_MAX_LENGTH: Final = 512
HINT_LENGTH: Final = 4
# A hint is only safe when it reveals a small part of the secret.
HINT_MIN_SECRET_LENGTH: Final = 16
ACCOUNT_NAME_MAX_LENGTH: Final = 100


class Provider(StrEnum):
    ASAAS = "asaas"
    STRIPE = "stripe"
    MERCADOPAGO = "mercadopago"


class ProviderEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class ProviderAccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider: Provider
    enabled: bool
    # Credential fields; the first required one feeds the last-4 hint.
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


CATALOG: Final[Mapping[Provider, ProviderSpec]] = {
    Provider.ASAAS: ProviderSpec(
        Provider.ASAAS, True, required=("api_key",), optional=("webhook_token",)
    ),
    Provider.STRIPE: ProviderSpec(Provider.STRIPE, False, required=("secret_key",)),
    Provider.MERCADOPAGO: ProviderSpec(Provider.MERCADOPAGO, False, required=("access_token",)),
}


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """Stored metadata of an account; the encrypted credentials are not part of it."""

    id: UUID
    project_id: UUID
    provider: Provider
    environment: ProviderEnvironment
    name: str
    status: ProviderAccountStatus
    key_version: int
    credentials_hint: str | None
    created_at: datetime
    updated_at: datetime


class ProviderNotEnabledError(ValueError):
    """The provider is in the catalog but not available yet."""

    def __init__(self, provider: Provider) -> None:
        super().__init__(f"provider {provider.value!r} is not enabled")
        self.provider = provider


def require_enabled(provider: Provider) -> ProviderSpec:
    spec = CATALOG[provider]
    if not spec.enabled:
        raise ProviderNotEnabledError(provider)
    return spec


def validate_account_name(name: str) -> str:
    value = name.strip()
    if not 1 <= len(value) <= ACCOUNT_NAME_MAX_LENGTH:
        raise ValueError(f"name must have 1 to {ACCOUNT_NAME_MAX_LENGTH} characters")
    return value


def validate_credentials(provider: Provider, credentials: Mapping[str, str]) -> dict[str, str]:
    """Exactly the fields the provider defines; values non-empty and bounded.

    Messages name fields only, never values.
    """
    spec = CATALOG[provider]
    allowed = set(spec.required) | set(spec.optional)
    unknown = sorted(set(credentials) - allowed)
    if unknown:
        raise ValueError(f"unknown credential fields: {', '.join(unknown)}")
    missing = [name for name in spec.required if name not in credentials]
    if missing:
        raise ValueError(f"missing credential fields: {', '.join(missing)}")
    cleaned: dict[str, str] = {}
    for name, value in credentials.items():
        stripped = value.strip()
        if not 1 <= len(stripped) <= CREDENTIAL_VALUE_MAX_LENGTH:
            raise ValueError(
                f"credential field {name!r} must have 1 to {CREDENTIAL_VALUE_MAX_LENGTH} characters"
            )
        cleaned[name] = stripped
    return cleaned


def encode_credentials(credentials: Mapping[str, str]) -> bytes:
    return json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()


def decode_credentials(payload: bytes) -> dict[str, str]:
    decoded: object = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("credentials payload is not an object")
    fields = cast("dict[object, object]", decoded)
    return {str(key): str(value) for key, value in fields.items()}


def credentials_hint(provider: Provider, credentials: Mapping[str, str]) -> str | None:
    """Last 4 characters of the main credential, only when it is long enough to be safe."""
    main = credentials[CATALOG[provider].required[0]]
    return main[-HINT_LENGTH:] if len(main) >= HINT_MIN_SECRET_LENGTH else None


def credentials_aad(project_id: UUID, provider: Provider, account_id: UUID) -> bytes:
    """AES-GCM associated data: binds a ciphertext to its row (``project|provider|account``)."""
    return f"{project_id}|{provider.value}|{account_id}".encode()
