# Integrations

## 1. Integrating a product (inbound to the hub)

| Step | How |
|---|---|
| 1. Create project + API key | Console → Projects. Key format `fh_<env>_<prefix>_<secret>` (shown once). Scopes: `ingest`, `read`, `mcp`, `admin`. |
| 2. Describe the product | Write a manifest by hand, or let the AI Integration Analyst propose one (Console or MCP). |
| 3. Validate & simulate | `POST /v1/projects/{p}/manifests/{v}/validate`, then `.../simulate` with a sample business event (dry run, no side effects). |
| 4. Approve & activate | Operator with `integrations:approve` (owner, finance). Four-eyes on money changes. Audited. Rollback = activate a previous version. |
| 5. Send facts | `POST /v1/business-events` (key scope `events:write`) or `POST /v1/usage` / `/v1/usage/batch`, `PUT /v1/projects/{p}/customers/{external_ref}`; always with an `idempotency_key` / `transaction_id`. |
| 6. Receive outcomes | Subscribe to outbound webhooks (`invoice.finalized`, `payment.confirmed`, `wallet.low_balance`, …). |

Manifest schema: `manifest.v1` (JSON Schema draft 2020-12, served at `GET /v1/manifest-schema/v1` and checked in at
[`schemas/manifest.v1.json`](schemas/manifest.v1.json)). See [ADR-0008](adr/0008-integration-manifest.md).

### The integration manifest (W6.1): what the code does

Module `modules/integrations`; tables `integration_manifests`, `integration_manifest_activations`,
`integration_webhook_endpoints`, `business_events` (migration `0015_integrations`).

- **Document.** A `manifest.v1` declares the product, how customers are read from a payload, the business events (each
  with a bounded `payload_schema` and its `usage` mappings), the metrics (`existing` or `create`), the prices it rates
  with, the splits (basis points; the platform keeps the remainder), the wallet policy and outbound webhooks. Models are
  strict pydantic (unknown keys are refused); the JSON Schema is generated from them, so there is no `jsonschema`
  dependency. Payloads are checked by a small validator of the schema subset the manifest allows (`type` without `number`, `required`,
  `properties`, `items`, `enum`, length/range bounds, `format` `decimal`/`date-time`; no `pattern`, no `$ref`), never by a regex engine. Floats are refused anywhere
  in a manifest or payload: amounts and factors are decimal strings, money is integer minor units.
- **Mapping language.** Bounded, no `eval`, no loops: a selector is a JSON Pointer (`/customer/id`), or
  `{"op": "literal", "value": ...}`, `{"op": "pointer", "pointer": "/a"}`, `{"op": "multiply", "of": <selector>, "by": "<decimal>"}`,
  `{"op": "coalesce", "of": [<selector>, ...]}` (first present value), `{"op": "to_decimal", "of": <selector>}`. Nesting
  depth, pointer length and list sizes are capped; violations are 422 with a pointer to the offending node.
- **Lifecycle.** `draft → validated → approved → active → superseded`, plus `rejected`. A draft may be edited (`PUT`)
  and may even be incomplete; `validate` checks the schema and that every referenced metric and price exists, and
  freezes the content (`content_hash` = SHA-256 of canonical JSON). `approve` takes the `content_hash` the reviewer saw
  and stores the diff against the active version. **Four-eyes** (approver ≠ author) is mandatory when the diff touches
  `splits`, `prices` or `wallet`, and optional otherwise (`FH_INTEGRATIONS_FOUR_EYES_ALWAYS=true` makes it universal).
  `activate` is atomic: it supersedes the previous active version, creates the declared metrics and webhook endpoints
  (an endpoint created by an earlier activation is reused), records the activation, audits and emits
  `manifest.activated`. Webhook signing secrets are returned once, in the activation response, to the activating
  operator; they are never stored in the manifest. Rollback = activate a superseded version; history is never rewritten.
- **Ingestion.** `POST /v1/business-events` `{name, payload, idempotency_key}` with an API key holding `events:write`:
  validated against the active manifest (no active manifest → 409, unknown name or bad payload → 422), mapped to
  usage events (`transaction_id` derived from the key, so each usage event is recorded once), and stored with its
  outcome (personal data redacted). A retry with the same payload answers 200 with the original record; the same key
  with another payload is 409.
- **Simulate.** `POST /v1/projects/{p}/manifests/{v}/simulate` returns the usage drafts, the charge lines per price and
  the split allocation per currency (largest remainder, platform last). It only reads.
- **Demo.** `seed-demo` creates v1 (active; authored by Ana, approved and activated by Carla) mapping
  `consultation.completed` to `api_calls`, and a draft v2 that changes the clinic split from 70% to 65%.

## 2. Payment providers

| Provider | Status | Notes |
|---|---|---|
| Asaas | **Sandbox** | `https://api-sandbox.asaas.com/v3`; PIX, boleto (card refused by the adapter, see below); refunds; split by `walletId`; transfers; statement |
| Stripe | Planned — *not enabled* | Port + capability flags only |
| Mercado Pago | Planned — *not enabled* | Port + capability flags only |

### Asaas → canonical event mapping (every documented event is mapped or explicitly ignored; see `research/asaas-sandbox.md`)

The canonical event names come from one registry in code (`modules/events/registry.py`, ADR-0005). Every money-affecting
event is processed per ADR-0012: project binding check, **refetch** of the payment/transfer, orthogonal state transition,
recipe under the aggregate lock. Amounts never come from the webhook body.

| Asaas webhook event | State transition (ADR-0012) | Canonical event | Ledger recipe |
|---|---|---|---|
| `PAYMENT_CREATED` | — | `payment.created` | — |
| `PAYMENT_AUTHORIZED` (card) | — | `payment.authorized` | — |
| `PAYMENT_AWAITING_RISK_ANALYSIS` / `PAYMENT_APPROVED_BY_RISK_ANALYSIS` | — | `payment.updated` | — |
| `PAYMENT_CONFIRMED` | capture `pending → captured` | `payment.confirmed` | R2 + R3 (or R6 top-up; R2b if already captured/canceled) |
| `PAYMENT_RECEIVED` | settlement `none → settled` (capture first if still pending, key `payment:{pp}:captured`) | `payment.settled` (+ `payment.confirmed` if not yet emitted) | R4 (+ R2/R3 first), R2a if `netValue` changed |
| `PAYMENT_OVERDUE` | — | `payment.overdue` | — |
| `PAYMENT_DELETED` / `PAYMENT_BANK_SLIP_CANCELLED` | capture `pending → canceled` | `payment.canceled` | — (R11 only by operator void) |
| `PAYMENT_RESTORED` / `PAYMENT_UPDATED` | refetch and re-derive | `payment.updated` | as derived |
| `PAYMENT_CREDIT_CARD_CAPTURE_REFUSED` / `PAYMENT_REPROVED_BY_RISK_ANALYSIS` | capture `pending → failed` | `payment.failed` | — |
| `PAYMENT_REFUND_IN_PROGRESS` | refund `requested` | `refund.pending` | R8 phase 1 (pending) |
| `PAYMENT_REFUNDED` / `PAYMENT_PARTIALLY_REFUNDED` | refund `done` (delta from `refunds[]`) | `refund.succeeded` | R8 phase 2 (post) |
| `PAYMENT_REFUND_DENIED` | refund `denied` | `refund.failed` | R8 void |
| `PAYMENT_CHARGEBACK_REQUESTED` / `PAYMENT_CHARGEBACK_DISPUTE` | dispute `none → open` | `dispute.opened` | R9 opened |
| `PAYMENT_AWAITING_CHARGEBACK_REVERSAL` | dispute stays `open` | `dispute.updated` | — |
| dispute resolution (refetched `chargeback.status`) | dispute `open → won|lost` | `dispute.won` / `dispute.lost` | R9 won / lost |
| `PAYMENT_SPLIT_DONE` | — | `split.done` | R5 (after R4) |
| `PAYMENT_SPLIT_DIVERGENCE_BLOCK` / `_FINISHED` | — | `split.blocked` / `split.unblocked` | R5 held / release |
| `PAYMENT_SPLIT_CANCELLED` | — | `split.canceled` | — (alert; shares remain internal liabilities) |
| `PAYMENT_RECEIVED_IN_CASH` (status) / `PAYMENT_RECEIVED_IN_CASH_UNDONE` | — | — | **rejected**: parked + alert (no money at the provider) |
| `TRANSFER_CREATED` / `TRANSFER_PENDING` / `TRANSFER_IN_BANK_PROCESSING` | payout `submitted` | `payout.updated` | — |
| `TRANSFER_DONE` | payout `paid` | `payout.paid` | R10 post (+ transfer fee) |
| `TRANSFER_FAILED` / `TRANSFER_CANCELLED` / `TRANSFER_BLOCKED` | payout `failed` | `payout.failed` | R10 void |
| `PAYMENT_ANTICIPATED`, `PAYMENT_DUNNING_*`, `PAYMENT_BANK_SLIP_VIEWED`, `PAYMENT_CHECKOUT_VIEWED` | — | — | ignored (stored as receipt, status `ignored`) |
| any other / unknown | — | — | parked for review |

PIX settles immediately (CONFIRMED and RECEIVED may arrive together or out of order); card moves to RECEIVED on credit date.
Dedup key: Asaas event `id` (`evt_…`). Webhook auth: `asaas-access-token` header equals the per-provider-account token we configured.
Transfers are authorized through the Asaas **withdrawal-validation webhook** (ADR-0012 §3.6).
Exact chargeback event names and dispute resolution fields are tracked as UNVERIFIED in the research note and confirmed in task 4.2.

### Asaas adapter (PRO-41): what the code does

`modules/payments/infrastructure/asaas_provider.py` implements the `PaymentProvider` port against Asaas v3 (sandbox
only: a `production` account is refused with `environment_not_supported`). Status normalization is the pure table in
`asaas_status.py`; a test fails when a documented status is unmapped.

| Asaas payment `status` | capture | settlement | dispute |
|---|---|---|---|
| `PENDING`, `OVERDUE`, `AWAITING_RISK_ANALYSIS`, `AUTHORIZED`, `DUNNING_REQUESTED` | pending | none | none |
| `CONFIRMED`, `RECEIVED_IN_CASH`, `REFUNDED`, `REFUND_REQUESTED`, `REFUND_IN_PROGRESS` | captured | none | none |
| `RECEIVED`, `DUNNING_RECEIVED` | captured | settled | none |
| `CHARGEBACK_REQUESTED`, `CHARGEBACK_DISPUTE`, `AWAITING_CHARGEBACK_REVERSAL` | captured | none | open |
| deleted (our own `DELETE`, or `deleted: true`) | canceled (raw status `DELETED`) | none | none |
| anything else | pending (warning with the status name only; fixed by the next refetch) | none | none |

Refund `status`: `PENDING → submitted`, `DONE → done`, `CANCELLED → cancelled`.

| Situation | Port error |
|---|---|
| connect error / connect or pool timeout (nothing was sent) | `Retryable` (`connect_error`) |
| timeout or broken connection **after** a `POST`/`DELETE` was sent | `Uncertain` (`timeout`, `connection_error`): never retried blindly, converged by lookup |
| read error or `5xx` on a `GET` | `Retryable` (`timeout`, `provider_unavailable`) |
| `5xx` on a `POST`/`DELETE` | `Uncertain` (`provider_error`) |
| `2xx` whose body is not a JSON object | `Uncertain` (`invalid_response`) |
| `429` | `Retryable` (`rate_limited`) |
| `401` / `403` / `404` | `Rejected` (`invalid_credentials` / `forbidden` / `not_found`) |
| other `4xx` | `Rejected` with `errors[0].code` when it is a plain token, else `invalid_request`; Asaas' description is never echoed |
| card payments | `Rejected` (`method_not_supported`): no way to pay without real card data |
| a charge for the reference already exists | `Duplicate` (`duplicate_external_reference`) |
| more than one object for one `externalReference` | `Uncertain` (`ambiguous_external_reference`) + a warning with ids only |

- **Correlation.** `externalReference` is our payment intent id (`pi_…`); a customer's is the hub `cus_…` id. Lookups
  filter server-side and match the reference exactly client-side (Asaas does not enforce uniqueness).
- **Refunds** carry our `rf_…` id in the refund `description` (Asaas refunds have no external reference); the adapter
  reads the payment first and answers `Uncertain` when the refund cannot be found afterwards.
- **Amounts.** Decimal reais in JSON ↔ integer minor units through `Decimal` only (no `float` is ever created: the JSON
  text is built by hand and responses are parsed with `parse_float=Decimal`); more than two decimals from Asaas is
  `Uncertain`.
- **Timeouts.** connect 5 s, read 15 s; one short-lived client per call.
- **Customers (W4.4).** Before the first charge on an account, `PaymentService` provisions the Asaas customer under the
  same external-call protocol as charges: a lease row in `provider_customer_provisions` (fencing token, so two workers
  never send concurrent creates), `externalReference` = the hub `cus_…` id, and `create_customer` looks that reference
  up before it creates, so a lost answer or a crash converges on the customer Asaas already holds. The mapping is
  stored in `provider_customers` and reused by every later charge. The CPF/CNPJ is decrypted by the customers module
  only for the call (`customers.application.api.load_customer_contact`) and never logged; E.164 phones (`+55…`) are
  sent as `mobilePhone` without the country code. A refused customer rejects the payment
  (`customer_<asaas code>`); an unknown outcome leaves the payment `unknown` for the sweeper, which provisions again
  (lookup first) and only then sends the charge. The charge is never sent without a linked customer.
- **Live smoke test.** `tests/integration/payments/test_asaas_live.py` (`@pytest.mark.asaas_live`) is skipped unless
  `FH_ASAAS_API_KEY` is set.

### Inbound Asaas webhooks and the sweeper (W4.4): what the code does

`POST /v1/webhooks/asaas/{provider_account_id}` (`modules/payments/api/webhooks.py`) is public: no session, API key or
CSRF. One URL per provider account, so the token and the project binding are per account.

1. **Size.** A body over `FH_PAYMENTS_WEBHOOK_MAX_BODY_BYTES` (default 256 KiB) is `413` before it is read.
2. **Auth.** The `asaas-access-token` header must equal the account's `webhook_token` credential (stored encrypted in
   the provider-account vault with the API key; see below). The compare is constant-time over SHA-256 digests. An
   unknown, disabled or non-Asaas account, a missing or wrong header, and a configured token shorter than 32
   characters all get the same `401` problem (`Invalid webhook credentials.`; `instance` only echoes the path).
3. **Inbox.** The raw body is stored in `provider_webhook_events` with dedup key (account, Asaas event `id`). A
   redelivery inserts nothing, enqueues nothing and answers `200`. A body that is not a notification is `422`.
4. **Fast `200`, async processing.** A new row publishes the internal `payments.webhook_received` event in the same
   transaction (under the nil project id, so no customer endpoint or realtime stream sees it). The worker's
   `payments.webhooks` handler queues an after-commit hook (no provider call inside the dispatcher's transaction).
5. **Processing.** The body only names the payment: the hub finds the intent by the Asaas payment id (or the
   `externalReference` `pi_…`), checks the binding (same project, provider and account as the token that
   authenticated it), converges an unknown creation first, then **refetches** through `PaymentService.refresh`.
   Amounts and statuses always come from the refetch, so a forged body, a replay or an out-of-order delivery cannot
   move money (a late `PAYMENT_CREATED` after `PAYMENT_RECEIVED` refetches `RECEIVED` and changes nothing).

| Notification | Action | Row status / outcome |
|---|---|---|
| every `PAYMENT_*` lifecycle event in the table above (created, authorized, risk analysis, confirmed, received, overdue, deleted, restored, updated, boleto cancelled, capture refused, refunds, chargebacks) | refetch | `processed` / `refreshed:<public status>` |
| `PAYMENT_DUNNING_RECEIVED` | refetch (**deviation** from "`PAYMENT_DUNNING_*` ignored": the refetched status `DUNNING_RECEIVED` settles the payment) | `processed` |
| `PAYMENT_ANTICIPATED`, `PAYMENT_DUNNING_REQUESTED`, `PAYMENT_BANK_SLIP_VIEWED`, `PAYMENT_CHECKOUT_VIEWED` | none | `ignored` / `ignored_event_type` |
| a payment the hub does not know | none | `ignored` / `unknown_payment` |
| `PAYMENT_RECEIVED_IN_CASH_UNDONE`, splits, transfers, any other or unknown type, or a payment event with no payment id | none | `parked` / `unsupported_event_type` |
| the payment belongs to another account than the URL's | none (warning) | `parked` / `binding_mismatch` |
| a creation the hub recorded as rejected | none (warning) | `parked` / `creation_rejected` |
| refetch failed / creation still pending | retried by the sweeper | `failed` / `refetch_failed`, `creation_pending` |

Today the refetch emits the existing `payment.status_changed` (and the ledger recipes of the transition); the finer
canonical names in the mapping table above are the target, not separate events yet.

**Sweeper.** The worker creates the internal schedule `payments.sweeper` (event `payments.sweep_due`) every
`FH_PAYMENTS_SWEEPER_INTERVAL_SECONDS` (default 60; `FH_PAYMENTS_SWEEPER_ENABLED=false` disables it, the row is kept
disabled). Each run resolves unknown/stuck creations and refunds (`resolve_pending`, at most
`FH_PAYMENTS_SWEEPER_BATCH_SIZE`) and retries inbox rows left `received`/`failed` for more than
`FH_PAYMENTS_WEBHOOK_RETRY_AFTER_SECONDS` (default 60), up to `FH_PAYMENTS_WEBHOOK_MAX_ATTEMPTS` (default 10; then
the row stays `failed` for a person). Only the worker registers the schedule: test runs have no sweeper unless a test
creates it.

#### Pointing the Asaas sandbox at a local hub

Asaas must reach the hub over public HTTPS, so expose the local API with a tunnel, for example
`cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`, and note the public URL.

1. Generate a token of at least 32 characters (`openssl rand -hex 32`) and store it on the provider account as the
   `webhook_token` credential, next to `api_key` (console: provider account → credentials, or
   `PUT /v1/projects/{project}/provider-accounts/{account}/credentials` with `{"credentials": {"api_key": "…",
   "webhook_token": "…"}}`: a replace sends the whole set, so resend the API key too).
2. In the Asaas sandbox (Integrações → Webhooks, or `POST /v3/webhooks`), create a webhook with URL
   `https://<public-host>/v1/webhooks/asaas/<pa_… account id>`, the same token as *Token de autenticação*, API
   version v3, sequential delivery, and **only the payment events** (`PAYMENT_*`). Customer and other event
   families are not processed and their bodies would be stored with personal data.
3. Pay a sandbox charge (Asaas sandbox: "Confirmar pagamento" on the charge) and watch the row move:
   `SELECT event_type, status, outcome FROM provider_webhook_events ORDER BY received_at DESC LIMIT 5`. The worker
   must be running (`docker compose up worker`) for rows to be processed.

Asaas pauses a webhook queue after repeated non-2xx answers; fix the cause (token, URL) and resume it in the panel.
Without a tunnel, post the fixture instead: `tests/integration/payments/webhooks.py` builds a notification from
`tests/integration/payments/fixtures/asaas_payment_received.json` and doubles as a CLI
(`FH_WEBHOOK_TOKEN=… python -m tests.integration.payments.webhooks --url http://localhost:8000 --account pa_…
--payment pay_… --reference pi_…`; the token is read from the environment, never from the command line).

## 3. Outbound webhooks (hub → products)

Follows the [Standard Webhooks](https://www.standardwebhooks.com/) specification (signing tested
against the specification's known vector). Code: `backend/src/financial_hub/modules/webhooks`.

### What the code does (W6.3)

- **Endpoints per project**: `POST/GET /v1/projects/{project_id}/webhook-endpoints`,
  `GET/PATCH .../{endpoint_id}` (url, description, `event_types` filter: exact `payment.succeeded`,
  prefix `payment.*` or `*`; `enabled`), `POST .../{endpoint_id}/disable`,
  `POST .../{endpoint_id}/rotate-secret`, `POST .../{endpoint_id}/test` (publishes a `webhook.test`
  event delivered to that endpoint only) and `GET .../{endpoint_id}/attempts`. Reading needs
  `webhooks:read` (owner, admin, finance); every change needs `webhooks:manage` (owner, admin).
  Both are operator-only (no API key scope). Create/update/rotate/disable are audited, never with
  the secret.
- **Secret**: `whsec_` + base64 of 32 random bytes, returned only by create and rotate
  (`Cache-Control: no-store`; an idempotent replay answers `409` instead of repeating it), stored
  AES-256-GCM sealed with `FH_ENCRYPTION_KEYS`. After a rotation the previous secret stays valid for
  **24 h**: deliveries carry both signatures, space-separated (new first).
- **Fan-out**: each endpoint is an `event_subscriptions` row of kind `webhook`, so the event log's
  fan-out creates one delivery per matching enabled endpoint. A disabled endpoint gets no new
  deliveries; ones queued before the disable wait (unsent) until it is enabled again.
- **Request**: `POST` of the event envelope exactly as the event explorer shows it
  (`GET /v1/projects/{project_id}/events/{event_id}`), `Content-Type: application/json`, headers
  `webhook-id` (the event id, stable across retries: dedupe on it), `webhook-timestamp` (unix
  seconds of this attempt) and `webhook-signature` (`v1,<base64 HMAC-SHA256(id.timestamp.body)>`).
  Receivers should reject timestamps outside ±5 minutes.
- **Outcome**: any 2xx is delivered; anything else (3xx included: redirects are never followed),
  a timeout (10 s) or a connection error is a failed attempt. Each attempt stores status code,
  latency and an error truncated to 2000 characters (`webhook_delivery_attempts`).
- **Retries**: 8 attempts; the delay after attempt *n* is 30 s, 2 min, 10 min, 30 min, 2 h, 8 h,
  24 h (each ±20% jitter, about 35 h in total), then the delivery is `dead`. Manual redelivery:
  `POST /v1/projects/{project_id}/events/{event_id}/deliveries/{delivery_id}/retry`
  (`webhooks:manage`). Endpoints are not auto-disabled yet.
- **Sender**: runs in the worker (`FH_WEBHOOK_SENDER_ENABLED`, default on), claims due deliveries
  with a fenced lease (`FH_WEBHOOK_BATCH_SIZE`, `FH_WEBHOOK_LEASE_SECONDS`,
  `FH_WEBHOOK_POLL_INTERVAL_SECONDS`, `FH_WEBHOOK_TIMEOUT_SECONDS`).
- **SSRF guard**: see docs/SECURITY.md, "Outbound webhooks". In dev/test only, `http` is allowed
  and `FH_WEBHOOK_ALLOW_HOSTS` (comma-separated names or IP literals) may point at a private
  address such as a local receiver; outside dev the setting is ignored and private targets are
  always refused.

## 4. MCP

Endpoint `/mcp` (streamable HTTP). Auth: API key with scope `mcp`. Tools and their safety boundary: [ADR-0009](adr/0009-ai-and-mcp-boundary.md).
