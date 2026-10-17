import { camelKey, t, tLoose, type MessageKey } from "../../i18n";

/*
 * Mirror of the backend's role -> permission table (backend/.../identity/domain/rbac.py), used ONLY to decide
 * which controls to offer. The API enforces every permission itself; a mismatch here can hide or show a
 * button wrongly, never grant access.
 */
export type Permission =
  | "projects:manage"
  | "members:read"
  | "members:manage"
  | "api_keys:manage"
  | "integrations:read"
  | "integrations:manage"
  | "webhooks:read"
  | "webhooks:manage"
  | "ledger:read"
  | "customers:read"
  | "customers:manage"
  | "payments:read"
  | "payments:refund"
  | "events:read"
  | "metering:read"
  | "metering:manage"
  | "audit:read";

/** What every role may read. */
const READ: readonly Permission[] = ["members:read", "integrations:read", "ledger:read", "customers:read", "payments:read", "events:read", "metering:read"];
const CONFIGURATION: readonly Permission[] = [
  "projects:manage",
  "members:manage",
  "api_keys:manage",
  "integrations:manage",
  "webhooks:manage",
];

/** Everyone but viewer reads webhooks (backend: READ_PERMISSIONS minus audit and webhooks for viewer). */
const WEBHOOKS_READ: readonly Permission[] = ["webhooks:read"];

const ROLE_PERMISSIONS: Record<string, readonly Permission[]> = {
  owner: [...READ, ...WEBHOOKS_READ, "audit:read", ...CONFIGURATION, "customers:manage", "payments:refund", "metering:manage"],
  admin: [...READ, ...WEBHOOKS_READ, "audit:read", ...CONFIGURATION, "customers:manage", "metering:manage"],
  finance: [...READ, ...WEBHOOKS_READ, "audit:read", "customers:manage", "payments:refund", "metering:manage"],
  viewer: READ,
};

export function can(role: string | null | undefined, permission: Permission): boolean {
  return role != null && (ROLE_PERMISSIONS[role]?.includes(permission) ?? false);
}

/** Only an owner may grant, change or remove the owner role (the API answers 403 otherwise). */
export function isOwner(role: string | null | undefined): boolean {
  return role === "owner";
}

/** Why a control is unavailable, for tooltips and helper text. */
export function deniedReason(permission: Permission, role: string | null | undefined): string {
  const action = t(`permissions.actions.${camelKey(permission)}` as MessageKey);
  if (!role) return t("permissions.deniedNoRole", { action });
  return t("permissions.denied", { role: tLoose("roles", role), action });
}

/** Scopes an API key can be granted (docs/SECURITY.md 3.1). Administrative permissions are never grantable. */
export const GRANTABLE_API_KEY_SCOPES: readonly { value: string; descriptionKey: MessageKey }[] = [
  { value: "projects:read", descriptionKey: "apiKeyScope.projectsRead" },
  { value: "customers:read", descriptionKey: "apiKeyScope.customersRead" },
  { value: "customers:write", descriptionKey: "apiKeyScope.customersWrite" },
  { value: "ledger:read", descriptionKey: "apiKeyScope.ledgerRead" },
  { value: "payments:read", descriptionKey: "apiKeyScope.paymentsRead" },
  { value: "payments:write", descriptionKey: "apiKeyScope.paymentsWrite" },
  { value: "payments:refund", descriptionKey: "apiKeyScope.paymentsRefund" },
  { value: "billing:read", descriptionKey: "apiKeyScope.billingRead" },
  { value: "billing:write", descriptionKey: "apiKeyScope.billingWrite" },
  { value: "events:read", descriptionKey: "apiKeyScope.eventsRead" },
  { value: "usage:write", descriptionKey: "apiKeyScope.usageWrite" },
];
