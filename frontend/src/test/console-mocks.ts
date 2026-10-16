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

/** Everything the Overview (and the shell) reads for a project, all empty/healthy. */
export function overviewData(projectId: string): Record<string, Handler> {
  return {
    [`GET /v1/projects/${projectId}/ledger/trial-balance`]: () =>
      json(200, { as_of: null, balanced: true, lines: [], totals: [{ currency: "BRL", balanced: true, debits: 0, credits: 0 }] }),
    [`GET /v1/projects/${projectId}/api-keys${LIST}`]: () => json(200, page([apiKey()])),
    [`GET /v1/projects/${projectId}/provider-accounts${LIST}`]: () => json(200, page([providerAccount()])),
  };
}

export function problem(status: number, detail: string, errors: { pointer: string; detail: string }[] = []) {
  return json(status, { title: "Problem", status, detail, errors });
}
