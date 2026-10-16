import { json, type Handler } from "./harness";

export const LIST = "?limit=200";

export function page<T>(data: T[], hasMore = false) {
  return { data, has_more: hasMore, next_cursor: null };
}

export function apiKey(overrides: Record<string, unknown> = {}) {
  return {
    id: "k-1",
    project_id: "p-1",
    name: "Server key",
    prefix: "fh_test_ab12",
    scopes: ["ledger:read"],
    environment: "test",
    status: "active",
    created_at: "2026-10-01T10:00:00Z",
    created_by: "op-1",
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    ...overrides,
  };
}

export function providerAccount(overrides: Record<string, unknown> = {}) {
  return {
    id: "pa-1",
    project_id: "p-1",
    provider: "asaas",
    environment: "sandbox",
    name: "Main Asaas",
    status: "active",
    has_credentials: true,
    credentials_last4: "9f2c",
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...overrides,
  };
}

export const CATALOG = [
  {
    provider: "asaas",
    enabled: true,
    environments: ["sandbox"],
    credential_fields: ["api_key"],
    optional_credential_fields: ["webhook_token"],
  },
  { provider: "stripe", enabled: false, environments: [], credential_fields: [], optional_credential_fields: [] },
];

/** Everything the Overview (and the shell) reads for a project, all healthy. */
export function overviewData(projectId: string): Record<string, Handler> {
  return {
    [`GET /v1/projects/${projectId}/ledger/trial-balance`]: () =>
      json(200, { as_of: null, balanced: true, lines: [], totals: [{ currency: "BRL", balanced: true, debits: 0, credits: 0 }] }),
    [`GET /v1/projects/${projectId}/api-keys${LIST}`]: () => json(200, page([apiKey()])),
    [`GET /v1/projects/${projectId}/provider-accounts${LIST}`]: () => json(200, page([providerAccount()])),
    ...dashboardData(projectId),
  };
}

export function problem(status: number, detail: string, errors: { pointer: string; detail: string }[] = []) {
  return json(status, { title: "Problem", status, detail, errors });
}

export const CUSTOMERS = "?limit=25";
export const AUDIT = "?limit=8";

export function member(overrides: Record<string, unknown> = {}) {
  return {
    operator_id: "op-1",
    email: "ana@example.test",
    display_name: "Ana Souza",
    role: "owner",
    added_at: "2026-10-01T10:00:00Z",
    ...overrides,
  };
}

export function customer(overrides: Record<string, unknown> = {}) {
  return {
    id: "c-1",
    project_id: "p-1",
    external_ref: "cust_1042",
    name: "Marina Alves",
    kind: "individual",
    email: "marina@example.test",
    phone: null,
    document_masked: "***.456.789-**",
    metadata: { plan: "pro" },
    anonymized: false,
    created_at: "2026-10-02T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    ...overrides,
  };
}

export function auditRecord(overrides: Record<string, unknown> = {}) {
  return {
    id: "au-1",
    project_id: "p-1",
    actor_type: "operator",
    actor_id: "op-1",
    action: "api_key.created",
    target_type: "api_key",
    target_id: "k-1",
    diff: {},
    ip: null,
    user_agent: null,
    request_id: null,
    correlation_id: null,
    occurred_at: "2026-10-03T09:00:00Z",
    ...overrides,
  };
}

/** What the Overview reads besides the ledger, keys and accounts: members, customers and the audit log. */
export function dashboardData(projectId: string): Record<string, Handler> {
  return {
    [`GET /v1/projects/${projectId}/members`]: () => json(200, { data: [member()] }),
    [`GET /v1/projects/${projectId}/customers${CUSTOMERS}`]: () => json(200, page([customer()])),
    [`GET /v1/projects/${projectId}/audit-records${AUDIT}`]: () => json(200, page([auditRecord()])),
  };
}
