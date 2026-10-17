"""Public facade of integrations for other modules (the CLI seed, docs tooling).

The schema of ``manifest.v1`` is generated from the pydantic models (no ``jsonschema`` dependency)
and checked in at ``docs/schemas/manifest.v1.json``; a unit test keeps the two equal.
"""

from financial_hub.modules.integrations.application.config import IntegrationsSettings
from financial_hub.modules.integrations.application.service import (
    ActivationResult,
    BusinessEventConflict,
    BusinessEventService,
    Ingestion,
    ManifestService,
    NoActiveManifest,
    Simulation,
    manifest_public_id,
)
from financial_hub.modules.integrations.domain import (
    SCHEMA_VERSION,
    Activation,
    BusinessEventRecord,
    ManifestStatus,
    ManifestVersion,
    manifest_json_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "Activation",
    "ActivationResult",
    "BusinessEventConflict",
    "BusinessEventRecord",
    "BusinessEventService",
    "Ingestion",
    "IntegrationsSettings",
    "ManifestService",
    "ManifestStatus",
    "ManifestVersion",
    "NoActiveManifest",
    "Simulation",
    "manifest_json_schema",
    "manifest_public_id",
]
