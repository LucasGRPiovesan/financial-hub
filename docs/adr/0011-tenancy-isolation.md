# ADR-0011: Tenancy and project isolation

- Status: Accepted · Date: 2026-10-10

## Context
Projects (products) must never see each other's customers, money or events. A FinancialCustomer is shared across projects but each project only sees its own identities, wallets and history.

## Decision
- Every tenant-owned table carries `project_id NOT NULL`, included in unique keys and leading index columns.
- Repositories require an explicit `ProjectScope` (derived from API key or operator membership); there is no unscoped repository method outside admin/system code paths, which are separately named and audited.
- Composite foreign keys `(project_id, id)` where cross-entity references must stay in-project.
- An **isolation test suite** generates two projects with mirrored data and asserts every API route and repository returns only scoped data.
- Postgres **RLS** is deferred (*Planned*): it adds defense-in-depth but complicates pooled connections (`SET LOCAL` per tx) and migrations; revisit after M10.

## Consequences
+ Simple, testable, explicit. − Relies on code discipline; mitigated by tests, import-linter, and reviews.

## Alternatives
Schema-per-tenant (heavy migrations), database-per-tenant (ops cost), RLS now (complexity before value).
