# Integrations

## 1. Integrating a product (inbound to the hub)

| Step | How |
|---|---|
| 1. Create project + API key | Console → Projects. Key format `fh_<env>_<prefix>_<secret>` (shown once). Scopes: `ingest`, `read`, `mcp`, `admin`. |
| 2. Describe the product | Write a manifest by hand, or let the AI Integration Analyst propose one (Console or MCP). |
| 3. Validate & simulate | `POST /v1/manifests/{id}/versions/{v}:validate`, `:simulate` with sample business events (dry run, no side effects). |
| 4. Approve & activate | Operator with `integrations:approve`. Audited. Rollback = activate previous version. |
| 5. Send facts | `POST /v1/business-events` or `POST /v1/usage-events` (batch), `PUT /v1/customers/{external_ref}`; always with `Idempotency-Key` / `transaction_id`. |
| 6. Receive outcomes | Subscribe to outbound webhooks (`invoice.finalized`, `payment.confirmed`, `wallet.low_balance`, …). |

Manifest schema: `manifest.v1` (published at `GET /v1/manifests/schema`). See [ADR-0008](adr/0008-integration-manifest.md).

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
- **Customers.** `create_customer` finds by `externalReference` first. The port carries no tax id yet, so the adapter
  takes an optional `cpf_cnpj`; `PaymentService` does not provision Asaas customers, so a payment whose customer is
  unknown at Asaas is `Rejected` (`customer_not_provisioned`). Known gap.
- **Live smoke test.** `tests/integration/payments/test_asaas_live.py` (`@pytest.mark.asaas_live`) is skipped unless
  `FH_ASAAS_API_KEY` is set.

## 3. Outbound webhooks (hub → products)

Follows the [Standard Webhooks](https://www.standardwebhooks.com/) specification:
- Headers `webhook-id`, `webhook-timestamp`, `webhook-signature` (`v1,<base64 HMAC-SHA256(id.timestamp.body)>`).
- Secrets `whsec_<base64>`; rotation keeps old + new secret valid for a grace window and sends both signatures.
- Retries with exponential backoff + jitter (schedule approx. 5s, 5m, 30m, 2h, 5h, 10h, 10h); endpoint auto-disabled after sustained failure (all attempts failing for 5 days or 3 consecutive dead deliveries — configurable); manual replay per delivery or by time range.
- Receivers must dedupe on `webhook-id` and reject timestamps outside ±5 minutes.
- The Python SDK ships a `verify()` helper.

## 4. MCP

Endpoint `/mcp` (streamable HTTP). Auth: API key with scope `mcp`. Tools and their safety boundary: [ADR-0009](adr/0009-ai-and-mcp-boundary.md).
