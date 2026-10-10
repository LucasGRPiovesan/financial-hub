# ADR-0002: Money representation and rounding

- Status: Accepted · Date: 2026-10-10

## Context
Floating point cannot represent decimal currency exactly. Usage pricing needs sub-cent unit prices (AI tokens). Splits must sum exactly.

## Decision
- Amounts are integer **minor units** (`BIGINT`) + ISO 4217 code; exponent from a static ISO table.
- `Decimal` only for rates/unit prices (`NUMERIC(38,18)` in DB); context with sufficient precision; never mixed with float.
- Quantize **once per charge line** with `ROUND_HALF_EVEN` (banker's rounding avoids systematic upward bias across many lines). Totals are integer sums.
- Allocation (splits, proportional refunds) uses **largest remainder**; ties broken by declared order for determinism.
- A `Money` value object rejects cross-currency arithmetic.

## Consequences
+ Exactness; trivially comparable with provider integer/decimal values. − Every boundary converts provider decimals (Asaas returns decimals like `100.5`) with explicit parsing (`Decimal(str)`), tested.

## Alternatives
`NUMERIC` amounts everywhere (risk of unquantized intermediate values leaking into postings); half-up rounding (Brazilian retail convention; acceptable but biased — revisit per-project if needed).
