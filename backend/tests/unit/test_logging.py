import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider

from financial_hub.platform.logging import (
    REDACTED,
    add_trace_context,
    bind_project_id,
    bind_request_context,
    configure_logging,
    log_context,
    redact_sensitive,
)


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def redact(event: dict[str, Any]) -> dict[str, Any]:
    return redact_sensitive(None, "info", event)


@pytest.mark.parametrize(
    "key",
    [
        "password", "Password", "db_password", "client_secret", "SECRET", "token", "refresh_token",
        "Authorization", "access_token", "api_key", "apiKey", "API-KEY", "x_api_key", "cpf",
        "customer_CPF", "cnpj", "CnpjNumber",
    ],
)  # fmt: skip
def test_sensitive_keys_are_masked_case_insensitively(key: str) -> None:
    assert redact({"event": "x", key: "s3cr3t"})[key] == REDACTED


def test_nested_dicts_and_lists_are_masked_without_mutating_input() -> None:
    event: dict[str, Any] = {
        "event": "provider.call",
        "request": {
            "headers": {"Authorization": "Bearer abc", "Accept": "json"},
            "customers": [
                {"name": "Ana", "cpf": "123.456.789-00"},
                {"name": "Org", "documents": [{"cnpj": "00.000.000/0001-00"}]},
            ],
            "pairs": ({"token": "t"}, "plain"),
        },
        "amount": 100,
    }
    original = json.dumps(event, sort_keys=True)

    result = redact(event)

    assert result == {
        "event": "provider.call",
        "request": {
            "headers": {"Authorization": REDACTED, "Accept": "json"},
            "customers": [
                {"name": "Ana", "cpf": REDACTED},
                {"name": "Org", "documents": [{"cnpj": REDACTED}]},
            ],
            "pairs": [{"token": REDACTED}, "plain"],
        },
        "amount": 100,
    }
    assert json.dumps(event, sort_keys=True) == original


def test_whole_subtree_under_sensitive_key_is_masked() -> None:
    assert redact({"secret": {"nested": "value"}})["secret"] == REDACTED


def test_non_string_keys_and_safe_values_pass_through() -> None:
    assert redact({1: "one", "count": 3, "items": {7}}) == {1: "one", "count": 3, "items": [7]}


def test_trace_context_is_added_inside_a_valid_span() -> None:
    tracer = TracerProvider().get_tracer("test")

    assert add_trace_context(None, "info", {}) == {}
    with tracer.start_as_current_span("span") as span:
        event = add_trace_context(None, "info", {})

    context = span.get_span_context()
    assert event == {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def test_configured_logger_emits_redacted_json_with_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    configure_logging("INFO")  # idempotent: one handler, latest level wins

    bind_request_context(correlation_id="corr-1", request_id="req-1")
    bind_project_id("prj_1")
    logger = structlog.get_logger("test")
    with log_context(operation="charge"):
        logger.info("payment.created", api_key="sk_live_123", payload={"password": "p"})
    logger.debug("hidden")
    logger.info("after", only_correlation=True)

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 2
    first, second = lines
    assert first["event"] == "payment.created"
    assert first["level"] == "info"
    assert first["correlation_id"] == "corr-1"
    assert first["request_id"] == "req-1"
    assert first["project_id"] == "prj_1"
    assert first["operation"] == "charge"
    assert first["api_key"] == REDACTED
    assert first["payload"] == {"password": REDACTED}
    assert "timestamp" in first
    assert "operation" not in second


def test_bind_request_context_without_request_id() -> None:
    bind_request_context(correlation_id="c")

    assert structlog.contextvars.get_contextvars() == {"correlation_id": "c"}
