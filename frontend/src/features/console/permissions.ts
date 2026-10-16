/*
 * Mirror of the backend's role -> permission table (backend/.../identity/domain/rbac.py), used ONLY to decide
 * which controls to offer. The API enforces every permission itself; a mismatch here can hide or show a
 * button wrongly, never grant access.
 */
export type Permission =
  | "projects:manage"
  | "members:manage"
  | "api_keys:manage"
  | "integrations:read"
  | "integrations:manage"
  | "ledger:read";

const READ: readonly Permission[] = ["integrations:read", "ledger:read"];
const CONFIGURATION: readonly Permission[] = [
  "projects:manage",
  "members:manage",
  "api_keys:manage",
  "integrations:manage",
];

const ROLE_PERMISSIONS: Record<string, readonly Permission[]> = {
  owner: [...READ, ...CONFIGURATION],
  admin: [...READ, ...CONFIGURATION],
  finance: READ,
  viewer: READ,
};

export function can(role: string | null | undefined, permission: Permission): boolean {
  return role != null && (ROLE_PERMISSIONS[role]?.includes(permission) ?? false);
}

/** Why a control is unavailable, for tooltips and helper text. */
export function deniedReason(permission: Permission, role: string | null | undefined): string {
  const action: Record<Permission, string> = {
    "projects:manage": "change project settings",
    "members:manage": "manage members",
    "api_keys:manage": "manage API keys",
    "integrations:read": "view provider accounts",
    "integrations:manage": "manage provider accounts",
    "ledger:read": "read the ledger",
  };
  return `Your role${role ? ` (${role})` : ""} can't ${action[permission]}. Ask a project owner or admin.`;
}

/** Scopes an API key can be granted (docs/SECURITY.md 3.1). Administrative permissions are never grantable. */
export const GRANTABLE_API_KEY_SCOPES: readonly { value: string; description: string }[] = [
  { value: "projects:read", description: "Read the project" },
  { value: "customers:read", description: "Read customers" },
  { value: "customers:write", description: "Create and change customers" },
  { value: "ledger:read", description: "Read accounts, entries and balances" },
  { value: "payments:read", description: "Read payments" },
  { value: "payments:write", description: "Create payments" },
  { value: "billing:read", description: "Read billing data" },
  { value: "billing:write", description: "Change billing data" },
  { value: "events:read", description: "Read events" },
];
