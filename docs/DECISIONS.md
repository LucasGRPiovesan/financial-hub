# Decisions

Architecture Decision Records live in [`docs/adr/`](adr/). Format: Context · Decision · Consequences · Alternatives.
A decision changes only through a new ADR that supersedes the old one.

| ADR | Title | Status |
|---|---|---|
| [0001](adr/0001-ids-errors-pagination.md) | Identifiers, error format and pagination | Accepted |
| [0002](adr/0002-money-representation.md) | Money representation and rounding | Accepted |
| [0003](adr/0003-double-entry-ledger.md) | Double-entry ledger design | Accepted |
| [0004](adr/0004-idempotency.md) | Idempotency strategy | Accepted |
| [0005](adr/0005-event-log-outbox.md) | Event log as transactional outbox, Postgres-only messaging | Accepted |
| [0006](adr/0006-payment-provider-abstraction.md) | Provider-agnostic payments; Asaas Sandbox as the only real adapter | Accepted |
| [0007](adr/0007-own-billing-engine.md) | Own billing engine; providers only collect one-off charges | Accepted |
| [0008](adr/0008-integration-manifest.md) | Versioned Integration Manifest | Accepted |
| [0009](adr/0009-ai-and-mcp-boundary.md) | AI and MCP boundary | Accepted |
| [0010](adr/0010-realtime-sse.md) | Realtime via SSE + LISTEN/NOTIFY | Accepted |
| [0011](adr/0011-tenancy-isolation.md) | Tenancy and project isolation | Accepted |
| [0012](adr/0012-payment-state-and-external-calls.md) | Payment state model and money-movement protocol | Accepted |
| [0013](adr/0013-reconciliation.md) | Reconciliation | Accepted |

## Reviews
- [0.1 Architecture review](reviews/0.1-architecture-review.md) — independent review of the M0 docs; findings triaged and applied in FINANCIAL_MODEL v2 and ADR-0003/0005/0012/0013.

## Research inputs
- [Asaas Sandbox](research/asaas-sandbox.md) — provider facts (sourced; UNVERIFIED items listed in §9.3).
- [Stack evaluation](research/stack-evaluation.md) — versions and infra options (sourced).
- [Financial patterns benchmark](research/financial-patterns-benchmark.md) — written from documentation knowledge without live fetching; treat as orientation, UNVERIFIED register at the end.
