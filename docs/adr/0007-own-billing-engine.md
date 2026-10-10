# ADR-0007: Own billing engine; providers only collect one-off charges

- Status: Accepted · Date: 2026-10-10

## Context
Pricing models required (usage, tiers, volume, graduated, percentage, base + usage, minimums, wallet-first credits) exceed PSP subscription features. Provider-side subscriptions would split the truth between two systems.

## Decision
- Pricing, metering, invoicing and subscriptions live in the hub (modeled after Lago/Orb/Metronome concepts — see benchmark research).
- Pricing is a **pure, deterministic** rating library over versioned price definitions with effective dates.
- Every charge line **pins** the `price_version_id`, metric definition version and rounding policy it used, so any invoice can be re-derived and audited (benchmark Top-10 #10). Price versions carry `effective_from/effective_to` with a non-overlap exclusion constraint.
- Each finalized invoice (amount due after wallet credits) becomes one PaymentIntent → one provider one-off charge.
- Period close is idempotent and driven by the scheduler; late usage after close goes to the next period (documented policy; adjustments via credit notes are *Planned*).

## Consequences
+ Single source of truth, provider-independent, demonstrable. − More code to own; mitigated by property tests and golden examples.

## Alternatives
Asaas subscriptions (fixed amounts only, lock-in); external billing SaaS (not demonstrable).
