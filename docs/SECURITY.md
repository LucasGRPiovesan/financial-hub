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
| Authorization | RBAC roles `owner`, `admin`, `finance`, `viewer` per project; each route declares a permission; a test fails if a route lacks one |
| Money movement (maker-checker) | **Mandatory four-eyes** for refunds, payouts, ledger adjustments (R12/R13), unapplied-cash resolution and any manifest version that changes splits or prices: requester ≠ approver, approval bound to the exact payload hash, re-authentication (password step-up) on approve. TOTP for operators is *Planned* |
| Provider data trust | Webhooks are notifications only: project binding check (`intent.project == provider_account.project`), refetch from the provider before any posting, amounts never taken from the body (ADR-0012) |
| Payout control | Asaas withdrawal-validation webhook enabled; the hub approves only transfers matching a `submitted` payout; everything else rejected and alerted |
| Database roles | `fh_owner` (migrations) vs `fh_app` (runtime): no `UPDATE`/`DELETE` on ledger tables, no DDL, cannot disable triggers; balances changed only via `ledger_apply` (ADR-0003) |
| API keys | `fh_<env>_<prefix>_<secret>`; store `HMAC-SHA256(pepper, secret)`; prefix for lookup; scopes; expiry; revoke; last-used; constant-time compare |
| Tenancy | ADR-0011; isolation test suite |
| Secrets at rest | AES-256-GCM with versioned data keys from env (`FH_ENCRYPTION_KEYS`), associated data = row id; key version stored per row (automated re-encryption and API-key pepper rotation *Planned*) |
| Secrets in config | `SecretStr`; `.env` git-ignored; gitleaks in CI; structlog redaction of known secret fields and `Authorization`/`access_token` headers |
| Inbound webhooks | Per-provider-account token, constant-time compare; size limit; raw body stored; idempotent processing |
| Outbound webhooks | Standard Webhooks signatures; SSRF guard (deny private/link-local/metadata ranges, resolve-then-connect pinning, no redirects to private ranges, https required outside dev) |
| Input | Pydantic strict models; request size limits; RFC 9457 errors without stack traces |
| Rate limiting | Per API key and per login identity (Postgres token bucket) |
| AI / MCP | No money-moving tools (ADR-0009); prompts never include secrets; PII minimized (sample events are redacted before reaching the model); AI output treated as untrusted and schema-validated; AI-proposed manifests always need human approval (four-eyes if they change splits) |
| PII / LGPD | PII (name, CPF/CNPJ, email, phone) only in `customers` tables, encrypted at rest for documents; never in ledger metadata, event payloads to other projects, logs or traces (redaction processor + tests); erasure = anonymize the customer record while keeping ledger entries (legal retention) |
| Browser | CSP (`default-src 'self'`), `X-Content-Type-Options`, `Referrer-Policy`, frame-ancestors none |
| Audit | Append-only audit log for privileged actions, approvals, key and credential changes |
| Supply chain | Pinned dependencies (uv lock, npm lock), pip-audit, npm audit, container image scanning (Trivy) in CI, minimal non-root images |
| Transport | TLS terminated at the edge in deployment; HSTS; dev uses http on localhost only |

## 4. Out of scope (portfolio)
PCI DSS scope (no card data touches the hub — Asaas hosted checkout/tokenization only), KMS/HSM integration (*Planned*), SSO/OIDC for operators (*Planned*).
