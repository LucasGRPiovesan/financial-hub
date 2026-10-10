"""OpenTelemetry: traces, metrics and logs over OTLP/HTTP (``FH_OTEL_EXPORTER_OTLP_ENDPOINT``).

With no endpoint configured (tests, offline runs) nothing is installed: the OTel API stays on its
no-op providers, so instrumented code keeps working without exporting anything.
"""

import logging
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyInstrumentor,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.platform.settings import Settings

# Liveness probes are high-frequency noise; readiness stays traced (it exercises the database).
EXCLUDED_URLS = "/health/live"
# Exporter/transport loggers must not feed the OTLP log handler (export -> log -> export loop).
_NOT_EXPORTED_LOGGERS = ("opentelemetry", "urllib3", "requests")


@dataclass(frozen=True, slots=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    log_handler: logging.Handler

    def shutdown(self) -> None:
        logging.getLogger().removeHandler(self.log_handler)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        self.logger_provider.shutdown()


class _ExcludeExporterLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_NOT_EXPORTED_LOGGERS)


_telemetry: Telemetry | None = None


def _service_version() -> str:
    try:
        return version("financial-hub")
    except PackageNotFoundError:  # pragma: no cover - only when running from a bare source tree
        return "unknown"


def configure_telemetry(settings: Settings) -> Telemetry | None:
    """Install global providers once per process; returns ``None`` when export is disabled."""
    global _telemetry
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None
    if _telemetry is not None:
        return _telemetry

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": _service_version(),
            "deployment.environment.name": settings.app_env,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))
        ],
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    set_logger_provider(logger_provider)

    log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    log_handler.addFilter(_ExcludeExporterLogs())
    logging.getLogger().addHandler(log_handler)

    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    _telemetry = Telemetry(tracer_provider, meter_provider, logger_provider, log_handler)
    return _telemetry


def instrument_app(app: FastAPI, telemetry: Telemetry) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
        excluded_urls=EXCLUDED_URLS,
    )


def instrument_engine(engine: AsyncEngine, telemetry: Telemetry) -> None:
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
    )
