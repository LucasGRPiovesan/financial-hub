import os

os.environ.setdefault(
    "FH_DATABASE_URL", "postgresql+psycopg://financial_hub:financial_hub@db:5432/financial_hub"
)
# Tests never export telemetry, even when run inside the compose `api` service.
os.environ["FH_OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
