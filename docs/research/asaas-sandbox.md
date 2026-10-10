# Asaas Sandbox API — Research (TASK R1)

Scope: factual notes for Financial Hub (provider-agnostic core; Asaas Sandbox is the only provider implemented).
Source: official docs at https://docs.asaas.com (pages fetched 2026-10-10, `.md` variants + embedded OpenAPI).
Convention: **UNVERIFIED** = not confirmed in the docs read; **DOC-CONFLICT** = docs disagree with themselves. Docs are in pt-BR; field names are as in the API.

---

## 1. Environments

| Item | Sandbox | Production |
|---|---|---|
| Base URL | `https://api-sandbox.asaas.com/v3` | `https://api.asaas.com/v3` |
| Account | independent account, must be created separately | real account |
| API key | independent per environment | independent per environment |

Source: https://docs.asaas.com/docs/faq-sandbox , https://docs.asaas.com/docs/sandbox

- **Auth**: header `access_token: <api key>`. Missing/invalid/wrong-environment key → `401 Unauthorized`. Send `Content-Type: application/json` and `User-Agent: <app name>` (User-Agent is documented as required in the header example; treat as mandatory). https://docs.asaas.com/docs/autenticação-1
- **API keys**: created only in the web UI by admin users, shown once, up to 10 keys per account, optional expiry, can be disabled. https://docs.asaas.com/docs/chaves-de-api
- **Key prefix/format**: UNVERIFIED (not stated in pages read; do not validate by prefix in code).
- **IP allowlist**: optional hardening: https://docs.asaas.com/docs/whitelist-de-ips
- **Sandbox data is not copied to production**: customers, charges, webhooks must be recreated at go-live. https://docs.asaas.com/docs/faq-sandbox
- **Sandbox account creation**: separate sign-up on the sandbox site (link in FAQ item 2). Sandbox auto-approves accounts when mandatory data is valid. https://docs.asaas.com/docs/sandbox . Balance for tests can be added per https://docs.asaas.com/docs/como-adicionar-dinheiro-para-testes
- **Rate limits**: `429 Too Many Requests` when exceeding a per-endpoint limit, **25,000 requests per 12 h**, or **50 concurrent GET requests**. Headers `RateLimit-Remaining` / `RateLimit-Reset` are returned; wait for reset, avoid short-interval polling, back off on retry. https://docs.asaas.com/docs/requisições-bloqueadas-por-ausência-de-controle-de-limites . Per-endpoint numeric limits: UNVERIFIED.
- **Pagination**: `limit` (1–100, default 10) + `offset` (from 0), response has `hasMore`. https://docs.asaas.com/reference/listagem-e-paginacao
- **Idempotency**: there is **no idempotency-key header** (none documented). Docs prescribe: assign an internal unique operation id, store it with the Asaas id, use `externalReference` when the resource has it, look up before retrying after a timeout, add DB unique constraint / lock. Blind retry of `POST /payments` can create duplicate charges. https://docs.asaas.com/docs/retries-e-idempotência , https://docs.asaas.com/docs/cobrança-duplicada-após-retry-sem-idempotência
- `externalReference` uniqueness enforced by Asaas: UNVERIFIED (described as a "free field for search", https://docs.asaas.com/reference/criar-nova-cobranca ). Assume **not unique**.

## 2. Customers

Endpoints (see https://docs.asaas.com/reference/criar-novo-cliente and sibling reference pages):
- `POST /v3/customers` create; `GET /v3/customers/{id}` get; `GET /v3/customers` list (filters incl. `externalReference`, `cpfCnpj`, `email`, `name` — filter names UNVERIFIED beyond the ones the list page shows; check reference page before use).
- Required to create: `name`, `cpfCnpj` (the guide https://docs.asaas.com/docs/criando-um-cliente states cpfCnpj is needed for charges; whether sandbox accepts any/fake CPF-CNPJ: UNVERIFIED — use valid-checksum test documents).
- `externalReference`: free text, maps customer to our tenant-side id. Not guaranteed unique (UNVERIFIED).
- Store the returned `cus_...` id as the provider customer id. Customer is per Asaas account (a subaccount has its own customers).
- Email/SMS notifications are sent by default to customers; in sandbox use only own/authorized emails and phones. https://docs.asaas.com/docs/faq-sandbox (items 9–10)

## 3. Payments (cobranças)

Create: `POST /v3/payments` — https://docs.asaas.com/reference/criar-nova-cobranca
Key request fields: `customer` (id), `billingType` (`BOLETO`, `CREDIT_CARD`, `PIX`, `UNDEFINED` = payer chooses on the invoice page), `value`, `dueDate`, `description`, `externalReference`, `installmentCount`/`installmentValue`, `discount`/`interest`/`fine`, `split[]`, `callback`, `creditCard` + holder data for direct card payment.

- **Status enum (REST, `GET /payments/{id}`)**: `PENDING`, `RECEIVED`, `CONFIRMED`, `OVERDUE`, `REFUNDED`, `RECEIVED_IN_CASH`, `REFUND_REQUESTED`, `REFUND_IN_PROGRESS`, `CHARGEBACK_REQUESTED`, `CHARGEBACK_DISPUTE`, `AWAITING_CHARGEBACK_REVERSAL`, `DUNNING_REQUESTED`, `DUNNING_RECEIVED`, `AWAITING_RISK_ANALYSIS`. https://docs.asaas.com/reference/recuperar-status-de-uma-cobranca
  - **DOC-CONFLICT**: webhook event `PAYMENT_AUTHORIZED` exists but `AUTHORIZED` is not in this enum; treat unknown statuses defensively.
- **Lifecycle (typical)**: `PENDING` → `CONFIRMED` (paid, balance not yet available; typical for card) → `RECEIVED` (funds available). PIX/boleto commonly jump to `RECEIVED`. `PENDING` → `OVERDUE` after due date. Refund path: `RECEIVED` → `REFUND_REQUESTED`/`REFUND_IN_PROGRESS` → `REFUNDED`. Exact transition matrix: UNVERIFIED; do not assume every step occurs. Event semantics: https://docs.asaas.com/docs/webhook-para-cobrancas
- **value vs netValue**: `value` = gross charge; `netValue` = "value after Asaas fee discount" (OpenAPI description). `originalValue` also returned (when interest/fine applied). The fee itself is not returned as a separate field on the payment (a derived `value - netValue` is the only per-charge fee signal read); fee detail per movement is in the extrato (section 5). Fee schedule per method: UNVERIFIED (account-specific).
- **Split is computed over `netValue`** (see section 4).
- **dueDate** (`YYYY-MM-DD`); `canBePaidAfterDueDate`, interest/fine/discount objects exist.
- **externalReference**: stored and searchable on payments; use it for our payment-intent id (not unique-enforced; UNVERIFIED).
- **Credit dates**: payment response has `estimatedCreditDate` (expected availability) and `creditDate` (date credit became available); also `confirmedDate`, `paymentDate`, `clientPaymentDate`. https://docs.asaas.com/reference/criar-nova-cobranca . Card settlement lead time (D+30 by default is common knowledge): UNVERIFIED in pages read.
- **Pix QR code**: `GET /v3/payments/{id}/pixQrCode` → `encodedImage` (base64), `payload` (copy-and-paste), `expirationDate`. https://docs.asaas.com/reference/obter-qr-code-para-pagamentos-via-pix . Field list should be re-checked against the reference page before coding.
- **Pix charge guide**: https://docs.asaas.com/docs/cobrancas-via-pix ; **card guide** (invoice URL vs direct card API, tokenization, installments): https://docs.asaas.com/docs/cobrancas-via-cartao-de-credito
- **Card in sandbox**: approve = `4444 4444 4444 4444`, CCV `123`, any future expiry; decline cards listed in FAQ item 11. https://docs.asaas.com/docs/faq-sandbox . Card flows can emit `PAYMENT_AUTHORIZED`, `PAYMENT_CREDIT_CARD_CAPTURE_REFUSED`, and risk-analysis events (`PAYMENT_AWAITING_RISK_ANALYSIS`, `..._APPROVED_..`, `..._REPROVED_..`).
- **Simulating payment in sandbox** (sandbox-only): the documented sandbox actions are **"Confirmar pagamento"** (confirm payment) — https://docs.asaas.com/reference/confirmar-pagamento — and **"Forçar vencimento"** (force overdue) — https://docs.asaas.com/reference/forcar-vencimento . Index: https://docs.asaas.com/reference/ações-em-sandbox . Exact path of confirm endpoint: read it on the reference page (UNVERIFIED here). No payment is truly settled in sandbox; it is a simulation. https://docs.asaas.com/docs/testando-no-sandbox
- **Refunds**: `POST /v3/payments/{id}/refund` — https://docs.asaas.com/reference/estornar-cobranca . Full or partial (`value` optional → full; partial supported per event `PAYMENT_PARTIALLY_REFUNDED`). Payment carries `refunds[]` (each: `status` `PENDING|CANCELLED|DONE`, `value`, `dateCreated`, `description`, `endToEndIdentifier`, `transactionReceiptUrl`, `refundedSplits[{id,value,done}]`). Only count `DONE` refunds; multiple refunds per payment possible; never overwrite the original value. https://docs.asaas.com/docs/estornos . Events: `PAYMENT_REFUNDED`, `PAYMENT_PARTIALLY_REFUNDED`, `PAYMENT_REFUND_IN_PROGRESS`, `PAYMENT_REFUND_DENIED` (boleto only). Refund time limits / balance requirement: UNVERIFIED.
- **Chargeback (card)**: payment carries `chargeback{status, reason, disputeStartDate, value, paymentDate…}`. `status`: `REQUESTED`, `IN_DISPUTE`, `DISPUTE_LOST`, `REVERSED`, `DONE`. `reason` is a large enum (fraud, commercial disagreement, process error, …). https://docs.asaas.com/docs/chargeback . Events: `PAYMENT_CHARGEBACK_REQUESTED`, `PAYMENT_CHARGEBACK_DISPUTE`, `PAYMENT_AWAITING_CHARGEBACK_REVERSAL`. Whether sandbox can simulate chargebacks: UNVERIFIED (assume no).
- Other: `PAYMENT_DELETED`/`PAYMENT_RESTORED` (charge deletion), `PAYMENT_ANTICIPATED`, dunning (`PAYMENT_DUNNING_*`), `PAYMENT_BANK_SLIP_*`, `PAYMENT_CHECKOUT_VIEWED`.

## 4. Split

Doc: https://docs.asaas.com/docs/split-de-pagamentos , FAQ https://docs.asaas.com/docs/faq-do-split

- Config on the charge: `split[]` of `{walletId, fixedValue | percentualValue}` (also `totalFixedValue` for installments, and `externalReference`/description fields per the reference page).
- `fixedValue`: max 2 decimals; `percentualValue`: max 4 decimals, **computed over `netValue`** (after Asaas fees), not gross. Fixed and percentual can be mixed; no priority order; sum must not exceed available net value; pure-percentual total ≤ 100%.
- Never put the issuing account's own `walletId` in the split; the undistributed net stays in the issuing account.
- No limit on number of recipients; no scheduled/future-dated payout (processed on receipt). Need delayed control → use transfers instead.
- Every recipient must have an Asaas account. `walletId` is returned on subaccount creation and retrievable via `GET /v3/wallets/` — https://docs.asaas.com/reference/recuperar-walletid
- **Divergence block**: if split exceeds available net at receipt/anticipation, funds and split are blocked → event `PAYMENT_SPLIT_DIVERGENCE_BLOCK`; **2 business days** to fix; otherwise cancelled with `PAYMENT_SPLIT_DIVERGENCE_BLOCK_FINISHED` (and `PAYMENT_SPLIT_CANCELLED`).
- **Settlement event**: `PAYMENT_SPLIT_DONE` fired per split; identify with `additionalInfo.splitId`.
- Split status values on the payment `split[]` items: UNVERIFIED (read the `LeanPaymentSplitGetResponseDTO` schema before coding).
- **Refund of split charge**: split transfers are reverted; see `refundedSplits[].done` (https://docs.asaas.com/docs/estornos).
- Subscriptions: split on the subscription is a template; changing it does not alter already-generated charges. Installments: `fixedValue` per installment vs `totalFixedValue`. Anticipation reduces net available (https://docs.asaas.com/docs/faq-do-split).
- **Subaccounts**: created via `POST /v3/accounts` (https://docs.asaas.com/reference/criar-subconta). Only parent accounts with CNPJ may create via API; two models: non-BaaS (holder uses Asaas UI) and BaaS (white-label). Response includes `walletId` and the subaccount's own `apiKey` (returned once; store encrypted). Regulatory evaluation period applies to new API customers. https://docs.asaas.com/docs/subcontas , https://docs.asaas.com/docs/faq-periodo-de-avaliacao . Whether subaccount creation is fully automatic in sandbox: UNVERIFIED (sandbox has "Aprovar conta" action: https://docs.asaas.com/reference/ações-em-sandbox ).

## 5. Transfers / payouts and reconciliation endpoints

- **Transfer to external bank account or Pix key**: `POST /v3/transfers` (`value`, `bankAccount{…}` or `pixAddressKey` + `pixAddressKeyType`, `operationType` `PIX|TED`, `scheduleDate`, `description`). https://docs.asaas.com/reference/transferir-para-conta-de-outra-instituicao-ou-chave-pix . Between Asaas accounts: `POST /v3/transfers` with `walletId` (internal) — see https://docs.asaas.com/docs/transferencias
- Transfer `type` enum in OpenAPI: `PIX | TED | INTERNAL`; **DOC-CONFLICT**: webhook example shows `type: BANK_ACCOUNT`. Do not hard-fail on unknown type values.
- Transfer REST status: `PENDING`, `BANK_PROCESSING`, `DONE`, `CANCELLED`, `FAILED` (+ `BLOCKED` via event). Webhook names differ: `TRANSFER_IN_BANK_PROCESSING` ↔ `BANK_PROCESSING`. https://docs.asaas.com/docs/webhook-para-transferencias
- **Transfer events**: `TRANSFER_CREATED`, `TRANSFER_PENDING`, `TRANSFER_IN_BANK_PROCESSING`, `TRANSFER_BLOCKED`, `TRANSFER_DONE` (use `transactionReceiptUrl`), `TRANSFER_FAILED` (`failReason`), `TRANSFER_CANCELLED`. Not a mandatory sequence. Payload object key is `transfer`.
- **Withdrawal validation webhook**: Asaas can call our endpoint to approve/refuse a payout before execution — https://docs.asaas.com/docs/mecanismo-para-validacao-de-saque-via-webhooks . (Strongly relevant: lets the ledger authorize payouts.) Details of its response contract: UNVERIFIED here; read before implementing.
- **Sandbox transfer tests**: https://docs.asaas.com/docs/testando-transferências ; Pix QR payment test: https://docs.asaas.com/docs/testar-pagamento-de-qrcodes-pix . Sandbox behaviour is simplified; real bank timing not reproduced.
- **Balance**: `GET /v3/finance/balance` — https://docs.asaas.com/reference/recuperar-saldo-da-conta (returns the account `balance`).
- **Extrato / statement**: `GET /v3/financialTransactions` — https://docs.asaas.com/reference/recuperar-extrato (paginated; filters by date range, `order`; each row has `id`, `value`, `balance`, `type`, `date`, `description`, and references to `paymentId`/`transferId` etc. — exact fields: re-check page). This is the **source of truth for reconciliation** (per-movement fees, split movements, refunds, transfers).
- Daily reconciliation recommendation: pull `financialTransactions` for the window and match to ledger by provider reference; compare closing `balance` with `/finance/balance`. Fee line type names: UNVERIFIED.

## 6. Webhooks

- **Config via API**: `POST /v3/webhooks` — https://docs.asaas.com/reference/criar-novo-webhook , guide https://docs.asaas.com/docs/criar-novo-webhook-pela-api . Fields: `name`, `url`, `email`, `enabled`, `interrupted`, `apiVersion`, `authToken`, `sendType`, `events[]`. Up to **10 webhooks per account**. Works for subaccounts too (created with the subaccount key).
- **Auth**: the `authToken` you set is sent back in header **`asaas-access-token`**; compare it in constant time and reject otherwise. (Header name confirmed in the webhook guide https://docs.asaas.com/docs/receba-eventos-do-asaas-no-seu-endpoint-de-webhook ; token constraints e.g. length: UNVERIFIED.) IP allowlisting of Asaas senders: UNVERIFIED.
- **Payload shape**: `{ "id": "evt_...", "event": "PAYMENT_RECEIVED", "dateCreated": "...", "payment": { ... } }`; transfers use `transfer`, split events use `additionalInfo.splitId`. Example id: `evt_05b708f961d739ea7eba7e4db318f621&368604920`. https://docs.asaas.com/docs/como-implementar-idempotencia-em-webhooks
- **Event list — payments** (https://docs.asaas.com/docs/webhook-para-cobrancas): `PAYMENT_CREATED`, `PAYMENT_AWAITING_RISK_ANALYSIS`, `PAYMENT_APPROVED_BY_RISK_ANALYSIS`, `PAYMENT_REPROVED_BY_RISK_ANALYSIS`, `PAYMENT_AUTHORIZED`, `PAYMENT_UPDATED`, `PAYMENT_CONFIRMED`, `PAYMENT_RECEIVED`, `PAYMENT_CREDIT_CARD_CAPTURE_REFUSED`, `PAYMENT_ANTICIPATED`, `PAYMENT_OVERDUE`, `PAYMENT_DELETED`, `PAYMENT_RESTORED`, `PAYMENT_REFUNDED`, `PAYMENT_PARTIALLY_REFUNDED`, `PAYMENT_REFUND_IN_PROGRESS`, `PAYMENT_REFUND_DENIED`, `PAYMENT_RECEIVED_IN_CASH_UNDONE`, `PAYMENT_CHARGEBACK_REQUESTED`, `PAYMENT_CHARGEBACK_DISPUTE`, `PAYMENT_AWAITING_CHARGEBACK_REVERSAL`, `PAYMENT_DUNNING_REQUESTED`, `PAYMENT_DUNNING_RECEIVED`, `PAYMENT_BANK_SLIP_CANCELLED`, `PAYMENT_BANK_SLIP_VIEWED`, `PAYMENT_CHECKOUT_VIEWED`, `PAYMENT_SPLIT_CANCELLED`, `PAYMENT_SPLIT_DIVERGENCE_BLOCK`, `PAYMENT_SPLIT_DIVERGENCE_BLOCK_FINISHED`, `PAYMENT_SPLIT_DONE`. (The split section was truncated in my read after `..._DIVERGENCE_BLOCK_FINISHED`; any further split events: UNVERIFIED.)
- **Event list — transfers**: see section 5 (`TRANSFER_*`, 7 events). Other families (invoices, subscriptions, account status, Pix automatic, bill payments): exist, see https://docs.asaas.com/docs/eventos-de-webhooks (not read in detail; UNVERIFIED).
- **Delivery**: **at-least-once**; same event may be delivered more than once with the **same `id`** → dedupe on `id` (unique constraint; persist first, answer 200, process async). https://docs.asaas.com/docs/como-implementar-idempotencia-em-webhooks
- **Ordering**: `sendType=SEQUENTIALLY` preserves order (an event waits for previous ones; a failing event blocks the queue); `NON_SEQUENTIALLY` has no ordering guarantee and parallel delivery. https://docs.asaas.com/docs/tipos-de-envio . Even in sequential mode, design handlers to be order-tolerant (state machine that ignores regressions) and re-fetch the resource by id when in doubt.
- **Success criterion / response**: only **HTTP 200** counts as success (201, 204, 4xx, 5xx, timeouts, connection errors = failure). https://docs.asaas.com/docs/penalização-de-filas . **DOC-CONFLICT**: the create-webhook guide says "2xx"; return exactly `200`.
- **Retry/penalty schedule** (attempt → wait): 1→0, 2→30 s, 3→1 min, 4→3.5 min, 5→5 min (1st alert email), 6→15 min, 7→25 min, 8–12→1 h (10th: 2nd alert email), 13–14→2 h, 15→3 h (3rd email, queue paused). Applies to both send types. https://docs.asaas.com/docs/penalização-de-filas
- **Queue pause**: after **15 consecutive failures** the queue is interrupted; new events keep being generated and stored but are not sent; **events older than 14 days are permanently deleted**. Re-enable only after fixing the cause (otherwise it pauses again); reactivation via UI/API per https://docs.asaas.com/docs/como-reativar-fila-interrompida and https://docs.asaas.com/docs/fila-pausada . Webhook logs: https://docs.asaas.com/docs/logs-de-webhooks
- **Recommended receiver**: verify token → insert `(asaas_event_id UNIQUE, payload JSONB, status)` → return 200 fast → process via worker. Run a periodic **reconciliation poll** (`GET /payments?...`, extrato) because events can be lost after the 14-day window or the pause.
- Only `id`-based dedup is documented; there is no documented monotonically increasing sequence number or event timestamp-ordering guarantee (UNVERIFIED).

## 7. Subscriptions (brief)

- Asaas subscription (`POST /v3/subscriptions`) generates future charges itself; each charge has its own id/status; charges are generated progressively, not all upfront; supports boleto/Pix/card. https://docs.asaas.com/docs/assinaturas , https://docs.asaas.com/docs/faq-assinaturas
- Differs from installments (single operation) and from Pix Automático (payer-authorized recurring debit): https://docs.asaas.com/docs/diferença-entre-pix-automático-e-assinaturas-1 , https://docs.asaas.com/docs/pix-automatico
- Split on a subscription is a template for future charges only.
- **Recommendation**: do NOT adopt Asaas subscriptions in the core. Financial Hub's billing engine should create one-off `POST /payments` charges (with our own `externalReference` = invoice/intent id). Reasons: provider-agnostic core, full control of proration/retries/dunning, deterministic duplicate protection, no dependency on provider-side schedule. Revisit only if card-on-file auto-retry is required (UNVERIFIED how Asaas retries failed card renewals).

## 8. Sandbox limitations and gotchas

1. Nothing is truly cleared; payment confirmation is simulated through sandbox-only actions (confirm, force overdue, approve account). https://docs.asaas.com/docs/testando-no-sandbox , https://docs.asaas.com/reference/ações-em-sandbox
2. Behaviour/coverage differs from production in places; use sandbox to validate payload shapes, error handling and webhooks, not timing/fees. https://docs.asaas.com/docs/sandbox
3. Email/SMS: restricted/disabled in some cases due to messaging cost; use only own contacts. https://docs.asaas.com/docs/faq-sandbox
4. Real bank boletos are unrecognized in sandbox (status `FAILED`); bill-payment tests must use a boleto created in the same sandbox account. https://docs.asaas.com/docs/testando-no-sandbox
5. Separate keys, accounts and data; nothing migrates to production. Never use a prod key against sandbox (or vice versa).
6. Webhooks do work in sandbox (idempotency/failure/retry can be exercised). https://docs.asaas.com/docs/faq-sandbox (item 8)
7. Do not rely on the create response alone; the status changes arrive asynchronously (webhook) — validate later changes.
8. Fee amounts and settlement dates in sandbox may not match production: UNVERIFIED but assume non-representative.
9. Chargeback/anticipation/dunning simulation in sandbox: UNVERIFIED (no documented simulator).
10. Docs inconsistencies listed above (2xx vs 200; `AUTHORIZED`; transfer `type`; `IN_BANK_PROCESSING` vs `BANK_PROCESSING`) → code defensively, log unknown enum values, never crash on them.
11. Newer accounts need the `User-Agent` header; absence may be rejected.
12. Regulatory "evaluation period" may cap API usage for new production accounts (limits not read): https://docs.asaas.com/docs/faq-periodo-de-avaliacao

## 9. Design implications for Financial Hub

Basis: sections 1–8 and the Asaas guidance on idempotent retries (https://docs.asaas.com/docs/retries-e-idempotência) and webhook idempotency (https://docs.asaas.com/docs/como-implementar-idempotencia-em-webhooks).

### 9.1 Concept mapping (provider-agnostic ⇄ Asaas)

| Core concept | Asaas | Notes |
|---|---|---|
| Customer (provider party) | `customers` (`cus_…`) | map via `externalReference` = our party id; store `provider_ref` |
| **Payment intent** (our business intent, owns idempotency) | none (no idempotency key) | our row + unique `(tenant, intent_key)`; sent as `externalReference` |
| **Provider payment** | `payment` (`pay_…`) | one intent → 1 provider payment (or N for retries/new method); keep raw JSON |
| Payment method | `billingType` | PIX / BOLETO / CARD (+ UNDEFINED → resolved on paid event) |
| **Provider event** | webhook `id` (`evt_…`) | immutable inbox table, UNIQUE(provider, event_id) |
| **Fee** | `value − netValue` and extrato fee lines | book as separate ledger postings, never netted into revenue |
| Split recipient | `walletId` (subaccount) | map to tenant/sub-merchant; stored in provider-account table |
| Settlement | `PAYMENT_RECEIVED` / `creditDate` / `PAYMENT_SPLIT_DONE` | moves funds pending→available |
| **Payout** | `transfers` (`PIX`/`TED`/`INTERNAL`) | our payout intent id kept locally; use withdrawal-validation webhook as a control |
| Refund | `refunds[]` on payment | separate entity with its own status (PENDING/DONE/CANCELLED) |
| Dispute | `chargeback{status,reason}` | separate entity |
| Reconciliation feed | `financialTransactions` + `/finance/balance` | daily job |

### 9.2 Recommendations
1. **Idempotent create**: before `POST /payments`, persist intent; on timeout/5xx do `GET /payments?externalReference=<intent_key>` (filter name to be confirmed on the list reference page: UNVERIFIED) before any retry. Put a DB lock/unique per intent. Never blind-retry.
2. **Webhook inbox pattern**: verify `asaas-access-token`, insert raw event into `provider_events` with UNIQUE(provider, external_event_id), return `200` immediately, process async. Return exactly 200 (not 201/204). Alert when queue paused (3rd email) and have an admin runbook to reactivate; run catch-up reconciliation after any outage > a few hours (14-day retention hard limit).
3. **Use `NON_SEQUENTIALLY` or `SEQUENTIALLY`?** Prefer `SEQUENTIALLY` (preserves order, but one poison event blocks the queue — so the handler must never return non-200 for business errors; always persist and 200). Regardless, make the payment state machine monotonic and re-fetch the payment by id on ambiguous transitions.
4. **Ledger postings must come from confirmed facts, not API create responses**: 
   - `PAYMENT_CONFIRMED` → record receivable as "pending settlement" (card).
   - `PAYMENT_RECEIVED` → post to available funds; post fee = `value − netValue` (confirm against extrato).
   - `PAYMENT_SPLIT_DONE` → post per-recipient settlement using `additionalInfo.splitId`.
   - `PAYMENT_REFUNDED` / refund `DONE` → reverse (including `refundedSplits` with `done=true`).
   - `TRANSFER_DONE` → payout settled; `TRANSFER_FAILED`/`CANCELLED` → reverse reservation.
5. **Events to normalize into canonical events**:
   | Canonical | Asaas source |
   |---|---|
   | `payment.created` | `PAYMENT_CREATED` |
   | `payment.authorized` | `PAYMENT_AUTHORIZED` |
   | `payment.confirmed` | `PAYMENT_CONFIRMED` |
   | `payment.settled` | `PAYMENT_RECEIVED` |
   | `payment.failed` | `PAYMENT_CREDIT_CARD_CAPTURE_REFUSED`, `PAYMENT_REPROVED_BY_RISK_ANALYSIS` |
   | `payment.overdue` | `PAYMENT_OVERDUE` |
   | `payment.cancelled` | `PAYMENT_DELETED` (restore → `payment.reopened` from `PAYMENT_RESTORED`) |
   | `payment.updated` | `PAYMENT_UPDATED` |
   | `refund.requested / completed / denied` | `PAYMENT_REFUND_IN_PROGRESS` / `PAYMENT_REFUNDED`, `PAYMENT_PARTIALLY_REFUNDED` / `PAYMENT_REFUND_DENIED` |
   | `dispute.opened / in_review / lost / won` | `PAYMENT_CHARGEBACK_REQUESTED` / `…_DISPUTE` / `PAYMENT_AWAITING_CHARGEBACK_REVERSAL` (ambiguous mapping: confirm with `chargeback.status`) |
   | `split.settled / blocked / cancelled` | `PAYMENT_SPLIT_DONE` / `…_DIVERGENCE_BLOCK` / `PAYMENT_SPLIT_CANCELLED`, `…_BLOCK_FINISHED` |
   | `payout.created/pending/processing/blocked/paid/failed/cancelled` | `TRANSFER_*` |
   Ignore-but-log: `*_VIEWED`, dunning, bank-slip cancel (unless boleto is in scope).
6. **Provider adapter boundary**: the adapter returns canonical DTOs (`ProviderPayment`, `ProviderEvent`, `ProviderPayout`, `ProviderFee`) and keeps the raw payload; no Asaas enums beyond the adapter. Unknown status/event/type → `unknown` canonical + persisted raw + metric/alert.
7. **Money**: Asaas uses JSON decimal numbers (e.g. `124.9`). Parse as `Decimal` from the raw string, store minor units (centavos) in the ledger; never use float. Fees only visible post-facto; do not precompute.
8. **Multi-tenancy**: one parent Asaas account + one subaccount (own `walletId` + own `apiKey`) per tenant is the model that maps to split; store subaccount api keys encrypted; webhook config is per account (≤10 each) — use the tenant id in the webhook URL path or look up by payment/account to resolve the tenant (the event carries no tenant field that I verified: UNVERIFIED).
9. **Payout control**: enable the withdrawal-validation webhook so Asaas asks the Hub before executing a transfer; the ledger approves only when balance and the intent match.
10. **Billing**: keep our own billing engine → one-off charges; skip Asaas subscriptions (section 7).
11. **Rate limits**: central limiter ≤ well under 50 concurrent GETs and 25k/12h; honor `RateLimit-Reset`; prefer webhooks over polling; batch reconciliation via the extrato.
12. **Testing**: build a contract-test suite on sandbox using: create customer → create PIX payment → confirm via sandbox action → assert webhook `PAYMENT_RECEIVED` → assert extrato row; plus duplicate-delivery test (replay same `id`) and failing-endpoint test (return 500 five times, verify penalty/backoff handling).

### 9.3 Open items to verify before implementation (all UNVERIFIED above)
- Exact `GET /payments` filters (`externalReference`), split status enum, extrato field names / fee line types, withdrawal-validation response contract, tenant resolution field on events, sandbox chargeback/anticipation simulation, card settlement timing and refund limits, `externalReference` uniqueness, key format/prefix, per-endpoint rate limits.
