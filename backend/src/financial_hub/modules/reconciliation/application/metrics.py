"""OpenTelemetry gauges of reconciliation (exported via OTLP).

- ``reconciliation_breaks_open{kind}``: active (open + investigating) breaks of every project.
- ``reconciliation_ledger_drift_minor_units``: sum of ``|difference|`` of active ``ledger_drift``
  and ``balance_drift`` breaks (integer minor units; currencies are not converted).

Gauges read a snapshot the service refreshes after every run and every break change.
"""

from collections.abc import Iterable, Mapping

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation

METER_NAME = "financial_hub.reconciliation"


class ReconciliationMetrics:
    def __init__(self, meter: Meter | None = None) -> None:
        meter = meter or metrics.get_meter(METER_NAME)
        self._by_kind: Mapping[str, int] | None = None
        self._drift: int | None = None
        meter.create_observable_gauge(
            "reconciliation.breaks_open",
            callbacks=[self._observe_open],
            unit="{break}",
            description="Open or investigating reconciliation breaks, by kind.",
        )
        meter.create_observable_gauge(
            "reconciliation.ledger_drift_minor_units",
            callbacks=[self._observe_drift],
            unit="{minor_unit}",
            description="Sum of |difference| of active ledger_drift and balance_drift breaks.",
        )

    def update(self, by_kind: Mapping[str, int], drift_minor_units: int) -> None:
        self._by_kind, self._drift = dict(by_kind), drift_minor_units

    @property
    def breaks_open(self) -> Mapping[str, int] | None:
        return self._by_kind

    @property
    def drift_minor_units(self) -> int | None:
        return self._drift

    def _observe_open(self, _options: CallbackOptions) -> Iterable[Observation]:
        if self._by_kind is None:
            return []
        return [Observation(count, {"kind": kind}) for kind, count in self._by_kind.items()]

    def _observe_drift(self, _options: CallbackOptions) -> Iterable[Observation]:
        return [] if self._drift is None else [Observation(self._drift)]


_shared: ReconciliationMetrics | None = None


def shared_metrics() -> ReconciliationMetrics:
    """The process-wide instruments (created once: OTel warns on duplicate registrations)."""
    global _shared
    if _shared is None:
        _shared = ReconciliationMetrics()
    return _shared
