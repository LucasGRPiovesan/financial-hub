"""A valid ``manifest.v1`` document for the unit suites (plain JSON, as an API caller sends it)."""

import copy
from typing import Any

SAMPLE: dict[str, Any] = {
    "schema_version": "manifest.v1",
    "description": "Demo Store consultations",
    "product": {"code": "demo-store", "name": "Demo Store"},
    "customers": {
        "external_id": "/customer/id",
        "name": "/customer/name",
        "email": {"op": "coalesce", "of": ["/customer/email", "/contact/email"]},
    },
    "business_events": [
        {
            "name": "consultation.completed",
            "payload_schema": {
                "type": "object",
                "required": ["customer", "consultation_id", "minutes"],
                "properties": {
                    "customer": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "maxLength": 128},
                            "name": {"type": "string"},
                            "email": {"type": "string"},
                        },
                    },
                    "consultation_id": {"type": "string", "maxLength": 64},
                    "minutes": {"type": "integer", "minimum": 1, "maximum": 600},
                    "finished_at": {"type": "string", "format": "date-time"},
                },
            },
            "usage": [
                {
                    "metric": "api_calls",
                    "quantity": {"op": "multiply", "of": "/minutes", "by": "1"},
                    "timestamp": "/finished_at",
                },
                {"metric": "consultations"},
            ],
        }
    ],
    "metrics": [
        {"source": "existing", "code": "api_calls"},
        {
            "source": "create",
            "code": "consultations",
            "name": "Consultations",
            "aggregation": "count",
        },
    ],
    "prices": [{"code": "demo-api-calls"}],
    "splits": [{"payee": "clinic-1", "basis_points": 7000}],
    "wallet": {"mode": "disabled"},
    "webhooks": [],
}

PAYLOAD: dict[str, Any] = {
    "customer": {"id": "cust-0001", "name": "Maria Silva", "email": "maria@example.com"},
    "consultation_id": "c-1",
    "minutes": 30,
}


def sample(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(SAMPLE)
    document.update(copy.deepcopy(changes))
    return document
