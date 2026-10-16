# Security

> Baseline v2 (M0, revised after review F-25/26/27/33). The full STRIDE threat model and ASVS L2 checklist are completed in task 10.2.

## 1. Assets
Ledger integrity · provider credentials (Asaas API key, webhook tokens) · API keys · webhook signing secrets · customer PII (name, document, email) · operator accounts · audit trail.

## 2. Trust boundaries
Internet → API (products with API keys; operators with sessions) · Provider → inbound webhook endpoint · Hub → product webhook endpoints (SSRF surface) · Hub → Asaas · Hub → Google Gemini API (free tier: only synthetic/demo data and redacted samples may be sent) · MCP clients.

## 3. Controls

| Area | Control |
|---|---|
| Operator auth | Argon2id (OWASP params), server-side sessions in Postgres, `HttpOnly; Secure; SameSite=Lax` cookie, rotation on login, idle + absolute timeout, CSRF token header on unsafe methods, login throttling |
| Demo login | Off by default (`FH_DEMO_LOGIN`); evaluation only: lets any visitor open a session, without a password, as one of the seeded demo personas only (active operators whose email is in `demo.financial-hub.local`, the domain `seed-demo` creates; `DEMO_EMAIL_DOMAIN` in identity). The picker lists only them, and any other operator id answers exactly like an unknown one (404, no enumeration), so real operators cannot be impersonated; never register a real operator in that domain. Must never be enabled in production; the API logs a WARNING at startup when it is on, and each use is audited as `operator.login_succeeded` with `method: demo`. |
| Authorization | RBAC roles `owner`, `admin`, `finance`, `viewer` per project; each route declares a permission; a test fails if a route lacks one |
| Money movement (maker-checker) | **Mandatory four-eyes** for refunds, payouts, ledger adjustments (R12/R13), unapplied-cash resolution and any manifest version that changes splits or prices: requester ≠ approver, approval bound to the exact payload hash, re-authentication (password step-up) on approve. TOTP for operators is *Planned* |
| Provider data trust | Webhooks are notifications only: project binding check (`intent.project == provider_account.project`), refetch from the provider before any posting, amounts never taken from the body (ADR-0012) |
| Payout control | Asaas withdrawal-validation webhook enabled; the hub approves only transfers matching a `submitted` payout; everything else rejected and alerted |
| Database roles | `fh_owner` (migrations) vs `fh_app` (runtime): no `UPDATE`/`DELETE` on ledger tables, no DDL, cannot disable triggers; balances changed only via `ledger_apply` (ADR-0003) |
| API keys | `fh_<env>_<prefix>_<secret>`; store `HMAC-SHA256(pepper, secret)`; prefix for lookup; scopes; expiry; revoke; last-used; constant-time compare (details in 3.1) |
| Tenancy | ADR-0011; cross-project isolation suite that enumerates every `{project_id}` route (3.1) |
| Secrets at rest | AES-256-GCM with versioned data keys from env (`FH_ENCRYPTION_KEYS`), associated data = `project_id\|provider\|account_id`; key version stored per row; decryption importable only from the provider infrastructure layer (3.2). Automated re-encryption and API-key pepper rotation *Planned* |
| Secrets in config | `SecretStr`; `.env` git-ignored; gitleaks in CI; structlog redaction of known secret fields and `Authorization`/`access_token` headers |
| Inbound webhooks | Per-provider-account token, constant-time compare; size limit; raw body stored; idempotent processing |
| Outbound webhooks | Standard Webhooks signatures; SSRF guard (deny private/link-local/metadata ranges, resolve-then-connect pinning, no redirects to private ranges, https required outside dev) |
| Input | Pydantic strict models; request size limits; RFC 9457 errors without stack traces |
| Rate limiting | Postgres token buckets (one atomic upsert per check): per API key, per operator session, and per client IP for unauthenticated attempts (every login, every rejected API key or session). `429` problem+json with integer `Retry-After`, `RateLimit-Limit`/`RateLimit-Remaining`; health checks and SSE exempt; `X-Forwarded-For` honoured only from `FH_RATE_LIMIT_TRUSTED_PROXIES`. Complements the per-email/per-IP login throttle |
| AI / MCP | No money-moving tools (ADR-0009); prompts never include secrets; PII minimized (sample events are redacted before reaching the model); AI output treated as untrusted and schema-validated; AI-proposed manifests always need human approval (four-eyes if they change splits) |
| PII / LGPD | PII (name, CPF/CNPJ, email, phone) only in `customers` tables, encrypted at rest for documents; never in ledger metadata, event payloads to other projects, logs or traces (redaction processor + tests); erasure = anonymize the customer record while keeping ledger entries (legal retention) |
| Browser | CSP (`default-src 'self'`), `X-Content-Type-Options`, `Referrer-Policy`, frame-ancestors none |
| Audit | Append-only audit log for privileged actions, approvals, key and credential changes |
| Supply chain | Pinned dependencies (uv lock, npm lock), pip-audit, npm audit, container image scanning (Trivy) in CI, minimal non-root images |
| Transport | TLS terminated at the edge in deployment; HSTS; dev uses http on localhost only |

### 3.1 Project API keys

- **Format.** `fh_<env>_<prefix>_<secret>`: `env` is `test` or `live` (a deployment mints and accepts only its own: `prod`/`production`/`live` deployments are `live`, everything else `test`; a key whose environment segment is swapped is rejected), `prefix` is 8 public characters `[a-z0-9]` used to find the row and shown in lists, `secret` is 32 random bytes (`secrets`) as 43 base62 characters (about 256 bits). The full key is returned **once**, in the create response (`Cache-Control: no-store`), and can never be read again.
- **Storage.** Only `HMAC-SHA256(pepper, secret)` is stored (32 bytes). The secret is already high-entropy, so a fast keyed hash is the right primitive; the pepper (`FH_API_KEY_PEPPER`, `SecretStr`, at least 32 characters, held outside the database) means a database read alone can neither verify a guess nor brute-force a stolen digest. Outside `dev`/`test` the application refuses to start with the development default. Rotating the pepper invalidates every issued key (*Planned*: dual-pepper rollover).
- **Verification.** Constant-time (`hmac.compare_digest`); an unknown prefix is verified against a dummy digest so a miss costs the same as a wrong secret. Unknown, malformed, wrong-secret, revoked, expired, wrong-environment and archived-project keys all return the same `401` with the same body. A key is bound to exactly one project: any other project in the path is `404`, never `403`.
- **Scopes.** `projects:read`, `customers:read|write`, `ledger:read`, `payments:read|write|refund` (refunds are their own scope: a key that creates charges cannot refund them), `billing:read|write`, `events:read`, `usage:write` (record usage events through `POST /v1/usage`; it reads nothing) map to the permissions routes already declare. A key can never hold an administrative permission (`api_keys:manage`, `members:manage`, `projects:manage`, `integrations:read|manage`, `webhooks:manage`, `audit:read`): keys cannot mint keys or read provider accounts. Missing scope is `403`.
- **Lifecycle.** `created_by`, optional `expires_at`, `revoked_at` (revocation is final: a database trigger refuses to clear or change it, and `fh_app` cannot update the secret hash, scopes, prefix or owner). `last_used_at` is written at most once per `FH_API_KEY_TOUCH_INTERVAL_SECONDS` (default 60) so authentication does not write on every request. Only operators with `api_keys:manage` (owner, admin) create or revoke keys.
- **Authentication precedence.** The API-key authenticator runs **before** the session authenticator. `Authorization: Bearer fh_...` is judged on that token alone: an invalid key is `401` even when a valid session cookie is also sent (explicit credentials are never silently replaced by ambient ones). A request without a `fh_` bearer token falls through to the session cookie. Bearer tokens are not ambient (browsers never attach them), so they need no CSRF token; session-authenticated unsafe requests still do.
- **Logging.** Key creation and revocation are logged with the key id and prefix; the key, the secret and the digest never are, and neither is a rejected guess.
- **Known gaps.** The pepper has no online rotation. Repeated invalid keys are throttled per client IP (`auth_ip` bucket), not per targeted prefix, so a distributed guesser is limited only by the 256-bit secret.

### 3.2 Provider accounts and credentials

- **Providers.** `asaas` is enabled; `stripe` and `mercadopago` are in the catalog (`GET /v1/provider-catalog`, authenticated) with `enabled: false`: creating or activating one is `422` (`provider-not-enabled`). Environments are `sandbox` or `production`; an account name is unique per project, provider and environment.
- **Encryption.** Credentials are serialized and sealed with AES-256-GCM (`platform/crypto`): a fresh random 96-bit nonce per encryption, the key version stored in the row (`key_version`), and associated data `project_id|provider|account_id`, so a ciphertext copied to another account, provider or project no longer authenticates. Keys come from `FH_ENCRYPTION_KEYS` (`{"1": "<base64 of 32 bytes>"}`, required outside `dev`/`test`); the highest version encrypts and every configured version decrypts, which is how rotation works (add a version, keep the old ones). Re-encrypting old rows is *Planned*; until then an old key must stay configured. A deployment without keys fails closed: creating an account is a `500` and nothing is stored.
- **Never returned.** The API exposes `has_credentials` and, when the secret is at least 16 characters, its last four characters; nothing else. Validation errors never echo submitted values, and credentials never appear in logs (`SecretStr` for keys; tests assert it for success, failure and not-enabled paths).
- **Confinement.** Decryption (`credentials_vault`) is importable only from the provider infrastructure layer; import-linter contracts fail the build if the API, application or any other module imports it. The runtime role can update only the sealed columns and `name`/`status` of `provider_accounts` (not `provider`, `environment` or `project_id`, which are part of the associated data) and cannot delete rows (accounts are disabled).
- **Use by the payment adapter (PRO-41).** The only door to decrypted credentials is `projects.application.api.load_provider_secrets`: it answers for an account that belongs to the asking project **and** is active, otherwise `None`, so a disabled, foreign or unknown account is indistinguishable (the payment is refused as `not_found`/`credentials_unavailable`). It is wired into the payments service in `main.py` (composition root); payments cannot import decryption or the loader (import-linter contract 5). `ProviderSecrets` and `ProviderCredentials` hide their values in `repr`/`str`.
- **Asaas key handling.** The key travels only in the `access_token` header: never in a URL, exception message, repr, log line or span attribute (debug output goes through header redaction; error bodies are never echoed). Tests capture logs and tracebacks during failing calls and assert the fake key (`fake-` prefix, allowed by gitleaks) is absent.
- **Access.** Reading accounts needs `integrations:read`, changing them `integrations:manage` (owner, admin). API keys have neither.

### 3.3 Audit log

- **What is recorded.** One `audit_records` row per privileged action, written in the *same transaction* as the action (it commits or rolls back with it; the recorder never commits): operator login success/failure/lockout and logout, password change, project create/update, member added (project owner at creation), API key create/revoke, provider account create/credentials replaced/activated/disabled. Each row carries actor (`operator`, `api_key` or `system`), action (`api_key.created`), target, project (NULL for global actions such as login), correlation id, request id, client ip and user agent (truncated to 256).
- **Redaction.** `diff` holds before/after values of non-secret fields only: an API key shows name, prefix, environment and scopes, never the key or its hash; provider credentials appear as the fixed string `"[redacted]"`; login failures carry a masked email. The recorder also masks any diff key that names a secret (secret, hash, ciphertext, password, token, credential, nonce, document...) as defence in depth.
- **Immutability.** `fh_app` holds INSERT and SELECT only; a row trigger rejects UPDATE/DELETE and a statement trigger TRUNCATE for every role, the table owner included. Corrections are new records.
- **Reading.** `GET /v1/projects/{project_id}/audit-records` needs `audit:read` (owner, admin, finance; never viewers or API keys), is filterable by action, actor, target and time range, cursor-paginated, newest first. Global records (login) have no project and are not exposed by the API yet.
- **Known gaps.** Throttled (429) login attempts are logged, not audited, to keep an attack from flooding the table; member role change/removal are not audited because those endpoints do not exist yet (*Planned*, they must call the recorder when added).

### 3.4 Tests that guard this

`tests/integration/isolation` enumerates every served route containing `{project_id}` and proves that neither the owner session nor an all-scopes API key of project A can reach project B: always `404`, identical to the answer for a project that does not exist, with nothing of B changed. `tests/integration/projects` and `tests/integration/providers` cover key display-once, no plaintext in any table, wrong/revoked/expired/foreign keys, scopes, AES-GCM tampering (ciphertext, nonce, associated data), key rotation, log scrubbing, database privileges and the `0003 <-> 0004` migration round trip.


### 3.5 Customer documents (CPF/CNPJ)

- **Storage.** The document is validated (check digits), normalised to digits and stored only as `document_hash`, `document_ciphertext`, `document_last4`. The hash is HMAC-SHA256 under a subkey derived from the API-key pepper (`HMAC(pepper, "financial-hub/customer-document-hash/v1")`): it lets two projects recognise the same person without storing the number. The ciphertext is AES-GCM with associated data `customer|<fcus id>`, so a ciphertext copied to another row does not open. Only `customers.infrastructure.document_vault` decrypts (import-linter contract); no route does.
- **Exposure.** Responses show `document_masked` (last digits) only, never the document, `fcus_` ids or other projects' identities. Logs, events, audit diffs (`REDACTED` markers) and `external_ref`-keyed payloads carry ids and field names only; tests assert this against captured logs and the events/audit tables.
- **Erasure.** `POST .../customers/{id}/anonymize` (operators only, `customers:manage`) wipes the identity's name, contact data and metadata; the universal record is wiped when no live identity points to it. Rows stay (the ledger references them); the ledger is untouched. API keys scoped `customers:write` map to `customers:manage`.
- **Known gaps.** Rotating the pepper breaks cross-project recognition until documents are re-hashed; `external_ref` is chosen by the client and must not contain PII; erasure at the provider is a later task.

## 4. Out of scope (portfolio)
PCI DSS scope (no card data touches the hub — Asaas hosted checkout/tokenization only), KMS/HSM integration (*Planned*), SSO/OIDC for operators (*Planned*).
