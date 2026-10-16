# Metering: usage events and billable metrics

Metering records what a customer consumed (API calls, GB, seats...) and aggregates it per customer,
metric and time range. It does not price or invoice: `pricing` and `billing` read it through
`metering.application.api.aggregate_usage` (PRO-46).

## Billable metrics

A metric belongs to one project and is addressed by its `code` (`^[a-z][a-z0-9_]{0,62}$`, unique per
project). `aggregation` and `field` are fixed at creation; the only change afterwards is **archive**
(final: usage for an archived metric is rejected, recorded usage stays readable and summarizable).

| aggregation    | reads                          | value                              |
|----------------|--------------------------------|------------------------------------|
| `sum`          | `properties[field]`, a decimal | `SUM(value)`, 0 when empty         |
| `max`          | `properties[field]`, a decimal | `MAX(value)`, `null` when empty    |
| `count`        | nothing (`field` must be null) | `COUNT(*)` events                  |
| `unique_count` | `properties[field]`, scalar    | `COUNT(DISTINCT value)`            |

## Exact numbers

No floats anywhere. A `sum`/`max` property must be a JSON **integer** or a **decimal string**
(`"12.345"`) with at most 9 decimal places and 21 integer digits (`numeric(30,9)` column); a JSON
float such as `12.5` is rejected because it is already inexact. A `unique_count` property is a
string or integer (compared as text, `7` and `"7"` are the same value). Summaries return `value` as
a decimal string, computed by PostgreSQL.

## Schema (migration `0010_metering`)

- `billable_metrics(id, project_id, code, name, description, aggregation, field, status, created_at,
  updated_at, archived_at)`; unique `(project_id, code)`; `fh_app` may INSERT and UPDATE only
  `status`, `archived_at`, `updated_at`; a trigger refuses to un-archive.
- `usage_events(id, project_id, customer_id, metric_code, transaction_id, occurred_at, properties
  jsonb, value numeric(30,9), unique_value text, ingested_at)`; unique `(project_id,
  transaction_id)`; composite FK `(project_id, metric_code)` to the metric; trigger ties the customer
  to the project (ADR-0011). **Append-only**: `fh_app` has INSERT and SELECT only, and triggers
  refuse UPDATE, DELETE and TRUNCATE for every role.
- Index `(project_id, customer_id, metric_code, occurred_at)` serves the aggregation.

## Ingestion

`POST /v1/usage` (one event) and `POST /v1/usage/batch` (`{"events": [...]}`, 1..100), authenticated
by a **project API key with the `usage:write` scope**. The project is the key's. Operators do not
ingest, and a `usage:write` key can read nothing (summaries are `metering:read`, operators only).

```json
{ "transaction_id": "evt-9f2", "metric": "api_calls", "customer_external_ref": "acme",
  "timestamp": "2026-10-16T12:00:00Z", "properties": { "tokens": "1500" } }
```

The customer is `customer_id` (`cus_...`) or `customer_external_ref` (both must agree if both are
sent); unknown, other-project and anonymized customers are rejected (422, `/customer_id`).

**Accepted window**: `timestamp` must carry a timezone offset and lie within the last **30 days** and
at most **5 minutes** in the future (clock skew). It defaults to the time of receipt. Older usage is
rejected with 422 (`out_of_window`): late data beyond 30 days needs an operator adjustment, not
ingestion.

### Idempotency (by `transaction_id`)

`(project, transaction_id)` is unique. Order of decisions for each event:

1. shape and customer are validated;
2. if the transaction id exists: **same payload** (metric, customer, properties, and the timestamp
   if the retry sends one) -> **200** with the original event, nothing written, no new
   `usage.recorded`; **different payload** -> **409** `usage-transaction-conflict`;
3. otherwise the metric (known, active), the measure and the window are checked (422 on failure),
   then the event is inserted (**201**) and `usage.recorded` is published in the same transaction.

Because step 2 precedes step 3, a retry still succeeds after the metric was archived or the event
left the window. Concurrent senders of the same id are serialized by the unique index: the loser
re-reads the winner and is decided as in step 2. This route does not use the `Idempotency-Key`
header: the `transaction_id` is the key.

### Batch semantics: per item

Events of a batch are independent. Each one is accepted (`created`), recognised (`duplicate`) or
`rejected` with the same problem a single call would return (its `errors[].pointer` is prefixed with
`/events/<index>`); a rejected event writes nothing and does not stop the others. The response is
always **200** with `results` in request order plus `created`/`duplicates`/`rejected` counters. A
body that is not even well-formed (not JSON, an `events` list that is empty or over 100, a wrongly
typed field) is a 422 for the whole request. Retrying a whole batch is safe: accepted events come
back as `duplicate`. A transaction id repeated inside one batch is compared with its first
occurrence (same payload `duplicate`, different `rejected` with 409).

## Operator API

| route | permission |
|---|---|
| `POST /v1/projects/{id}/metrics` | `metering:manage` (admin, finance, owner) |
| `GET /v1/projects/{id}/metrics`, `GET .../metrics/{code}` | `metering:read` (every role) |
| `POST /v1/projects/{id}/metrics/{code}/archive` | `metering:manage` |
| `GET /v1/projects/{id}/usage/summary?customer_id&metric&from&to` | `metering:read` |

`from` is inclusive and `to` exclusive (both with timezone; `from < to`). The summary returns
`{customer_id, metric, aggregation, start, end, value, events}`. Metric creation and archiving are
audited (`metric.created`, `metric.archived`, target `billable_metric`). Usage events themselves are
not audited (the log is the table; `usage.recorded` goes to the event log).

## Event

`usage.recorded` v1: `{transaction_id, customer_id, metric, timestamp, value}` (`value` is the exact
decimal string for sum/max, otherwise null; properties are not included). Subject: `transaction_id`.
