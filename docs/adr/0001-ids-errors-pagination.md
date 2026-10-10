# ADR-0001: Identifiers, error format and pagination

- Status: Accepted · Date: 2026-10-10

## Context
Public APIs are consumed by N products and by AI/MCP clients. IDs appear in logs, webhooks and support tickets; errors must be machine-readable; lists can be large and are appended concurrently.

## Decision
- **IDs:** TypeID-style `prefix_` + UUIDv7 encoded in base32 (e.g. `inv_01j9…`). Stored as native `uuid`; prefix is derived from the entity type at the API boundary. UUIDv7 gives time-ordered index locality.
- **Errors:** RFC 9457 `application/problem+json` with stable `type` URIs (`https://financial-hub.dev/problems/<slug>`), `title`, `status`, `detail`, `instance`, plus `errors[]` (`pointer`, `detail`) for validation and `correlation_id`.
- **Pagination:** opaque cursor (keyset on `(created_at, id)`), `limit` ≤ 200, response `{data, next_cursor, has_more}`.

## Consequences
+ Self-describing IDs prevent cross-type mixups; no offset scans. − Custom encoding code (small, property-tested). Cursor pagination cannot jump to page N (acceptable for operational UIs).

## Alternatives
Plain UUIDv4 (no locality, no type hint); ULID columns as text (bigger indexes); offset pagination (unstable under inserts).
