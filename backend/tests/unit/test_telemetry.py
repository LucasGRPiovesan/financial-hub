import io
import logging
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from financial_hub.platform import telemetry
from financial_hub.platform.settings import Settings


class _FakeHttpxInstrumentor:
    calls: ClassVar[list[dict[str, Any]]] = []

    def instrument(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def endpoints(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Swap OTLP exporters and global setters so the test exports nothing and leaks no state."""
    seen: dict[str, str] = {}

    def fake(kind: str, exporter: Any) -> Any:
        def build(*, endpoint: str) -> Any:
            seen[kind] = endpoint
            return exporter

        return build

    monkeypatch.setattr(telemetry, "_telemetry", None)
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", fake("traces", InMemorySpanExporter()))
    monkeypatch.setattr(
        telemetry, "OTLPMetricExporter", fake("metrics", ConsoleMetricExporter(out=io.StringIO()))
    )
    monkeypatch.setattr(telemetry, "OTLPLogExporter", fake("logs", InMemoryLogRecordExporter()))
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda _p: None)
    monkeypatch.setattr(telemetry.metrics, "set_meter_provider", lambda _p: None)
    monkeypatch.setattr(telemetry, "set_logger_provider", lambda _p: None)
    monkeypatch.setattr(telemetry, "HTTPXClientInstrumentor", _FakeHttpxInstrumentor)
    return seen


def settings() -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,
        database_url="postgresql+psycopg://u:p@db/x",
        otel_exporter_otlp_endpoint="http://lgtm:4318/",
        otel_service_name="financial-hub-test",
        app_env="test",
    )


def test_configures_providers_once_with_signal_endpoints(endpoints: dict[str, str]) -> None:
    configured = telemetry.configure_telemetry(settings())
    assert configured is not None
    try:
        assert telemetry.configure_telemetry(settings()) is configured
        assert endpoints == {
            "traces": "http://lgtm:4318/v1/traces",
            "metrics": "http://lgtm:4318/v1/metrics",
            "logs": "http://lgtm:4318/v1/logs",
        }
        attributes = configured.tracer_provider.resource.attributes
        assert attributes["service.name"] == "financial-hub-test"
        assert attributes["deployment.environment.name"] == "test"
        assert configured.log_handler in logging.getLogger().handlers
        assert _FakeHttpxInstrumentor.calls[-1]["tracer_provider"] is configured.tracer_provider
    finally:
        configured.shutdown()

    assert configured.log_handler not in logging.getLogger().handlers


def test_exporter_logs_are_not_exported_back(endpoints: dict[str, str]) -> None:
    configured = telemetry.configure_telemetry(settings())
    assert configured is not None
    try:
        handler_filter = configured.log_handler.filters[0]
        record = logging.makeLogRecord({"name": "opentelemetry.exporter.otlp"})
        assert isinstance(handler_filter, logging.Filter)
        assert not handler_filter.filter(record)
        assert handler_filter.filter(logging.makeLogRecord({"name": "financial_hub.main"}))
    finally:
        configured.shutdown()


def test_instrument_app_excludes_liveness(endpoints: dict[str, str]) -> None:
    configured = telemetry.configure_telemetry(settings())
    assert configured is not None
    app = FastAPI()
    try:
        telemetry.instrument_app(app, configured)
        assert getattr(app, "_is_instrumented_by_opentelemetry", False)
    finally:
        configured.shutdown()
