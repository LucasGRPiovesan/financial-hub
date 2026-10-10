# ADR-0008: Versioned Integration Manifest

- Status: Accepted · Date: 2026-10-10

## Context
Each product integrates differently (which business events are billable, how they are priced, who gets split shares, which webhooks it wants). Hard-coding per product does not scale; free-form config is unsafe for money.

## Decision
- A JSON document validated by a published JSON Schema (`manifest.v1`): `product`, `customers` (identity mapping), `business_events[]` (schema + mapping to usage/charges), `metrics[]`, `prices[]` (refs to price versions), `splits[]`, `wallet` policy, `webhooks[]` subscriptions.
- Lifecycle: `draft → validated → approved → active → superseded`; rejection allowed. Approval requires an operator with `integrations:approve`; four-eyes (approver ≠ author, step-up re-authentication) is **mandatory** when the diff changes splits, prices or fee policy, optional otherwise (SECURITY §3).
- Activation is atomic, audited and emits `manifest.activated`. Rollback = activate a previous version (history is never mutated).
- Runtime mapping is deterministic and evaluates only the declarative mapping language (JSON pointer selectors + a tiny whitelisted expression set); no arbitrary code.
- `simulate` (dry run) evaluates a sample business event against a version and returns would-be usage, charges and postings without side effects.

## Consequences
+ Safe self-service onboarding; diffable; AI-friendly target format. − Expression language must stay small; anything not expressible is a product feature request, not config.

## Alternatives
Per-product code plugins (unsafe, not reviewable by finance); generic rules engine (overkill).
