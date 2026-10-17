"""``manifest.v1``: the published JSON Schema and validation (shape and cross-field rules)."""

import json
from pathlib import Path
from typing import Any

import pytest

from financial_hub.modules.integrations.domain import (
    canonical_json,
    content_hash,
    manifest_json_schema,
    parse_manifest,
)
from financial_hub.shared.errors import ValidationFailed
from tests.unit.integrations.samples import SAMPLE, sample


def pointers(error: ValidationFailed) -> set[str]:
    return {item.pointer for item in error.errors}


def invalid(content: Any) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as caught:
        parse_manifest(content)
    return caught.value


def test_the_sample_is_valid() -> None:
    manifest = parse_manifest(SAMPLE)
    assert manifest.event("consultation.completed") is not None
    assert manifest.metric("consultations") is not None


def test_schema_is_draft_2020_12_and_closed() -> None:
    schema = manifest_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("manifest.v1.json")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"schema_version", "product", "business_events"}
    assert '"type": "number"' not in json.dumps(schema)  # no floats anywhere


def test_checked_in_schema_matches_the_models() -> None:
    for parent in Path(__file__).resolve().parents:
        path = parent / "docs" / "schemas" / "manifest.v1.json"
        if path.is_file():
            break
    else:
        pytest.skip("docs/ is not mounted here (the schema is regenerated from the models)")
    assert json.loads(path.read_text(encoding="utf-8")) == manifest_json_schema()


@pytest.mark.parametrize(
    ("content", "pointer"),
    [
        ([], ""),
        (sample(schema_version="manifest.v2"), "/schema_version"),
        (sample(extra=True), "/extra"),
        (sample(business_events=[]), "/business_events"),
        (sample(splits=[{"payee": "a", "basis_points": 1.5}]), "/splits/0/basis_points"),
        (sample(splits=[{"payee": "a", "basis_points": 10001}]), "/splits/0/basis_points"),
        (sample(wallet={"mode": "credit"}), "/wallet/mode"),
    ],
)
def test_shape_errors_point_into_the_document(content: Any, pointer: str) -> None:
    assert pointer in pointers(invalid(content))


def test_cross_field_rules() -> None:
    error = invalid(
        sample(
            splits=[
                {"payee": "a", "basis_points": 6000},
                {"payee": "a", "basis_points": 5000},
            ],
            metrics=[{"source": "create", "code": "x_sum", "name": "X", "aggregation": "sum"}],
        )
    )
    found = pointers(error)
    assert "/splits" in found  # duplicate payee and total above 10000
    assert "/metrics/0/field" in found  # sum needs a field
    assert any(p.startswith("/business_events/0/usage") for p in found)  # undeclared metric


def test_payload_schema_rejects_number_and_pattern() -> None:
    event = json.loads(json.dumps(SAMPLE["business_events"][0]))
    event["payload_schema"]["properties"]["minutes"] = {"type": "number"}
    assert any("payload_schema" in p for p in pointers(invalid(sample(business_events=[event]))))
    event["payload_schema"]["properties"]["minutes"] = {"type": "string", "pattern": "^(a+)+$"}
    assert any("payload_schema" in p for p in pointers(invalid(sample(business_events=[event]))))


def test_content_hash_is_canonical() -> None:
    reordered = dict(reversed(list(SAMPLE.items())))
    assert canonical_json(reordered) == canonical_json(SAMPLE)
    assert content_hash(reordered) == content_hash(SAMPLE)
    assert len(content_hash(SAMPLE)) == 64
