# ADR-0006: Provider-agnostic payments; Asaas Sandbox as the only real adapter

- Status: Accepted · Date: 2026-10-10

## Context
The hub must support multiple PSPs over time. Only Asaas Sandbox is implemented now; Stripe and Mercado Pago must appear as honest "not enabled" options. Asaas facts: `docs/research/asaas-sandbox.md`.

## Decision
- Domain objects: `PaymentIntent` (ours) → `ProviderPayment` (attempt at a provider) → `ProviderEvent` (raw) → canonical events (`payment.confirmed`, `payment.settled`, `payment.refunded`, `dispute.*`, `split.*`, `payout.*`).
- `PaymentProvider` port: `ensure_customer`, `create_payment`, `get_payment`, `find_by_external_reference`, `refund`, `create_transfer`, `get_balance`, `list_statement`, `parse_webhook`, `normalize`.
- Registry of providers with capability flags; Stripe/Mercado Pago registered as `enabled=false` with no adapter code paths reachable.
- **Asaas specifics isolated in the adapter**: `access_token` + `User-Agent` headers; no idempotency header → persist-first + `externalReference` lookup; fee = `value − netValue`; split over `netValue`; webhook auth via `asaas-access-token` (constant-time compare); at-least-once with event id dedup; respond 200 only after durable receipt.
- Status normalization maps provider events to transitions of orthogonal state fields with a precedence table (ADR-0012 §1); posting amounts always come from a refetch, never from the webhook body (ADR-0012 §2).
- Asaas subscriptions are **not** used (ADR-0007).

## Consequences
+ Adding a provider = adapter + normalization table + contract tests. − Lowest-common-denominator risk; mitigated by capability flags.

## Alternatives
Use Asaas objects as domain model (lock-in); a payments orchestrator (Hyperswitch) as dependency (heavy, hides the design).
