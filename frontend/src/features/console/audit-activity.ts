import type { AuditRecordOut } from "../../api/generated/model";

/** Operator display names by id (members plus the signed-in operator); anything missing falls back to generic wording. */
export type OperatorNames = ReadonlyMap<string, string>;

export interface ActivityLine {
  /** One readable sentence: "Bruno Admin was added as admin". */
  sentence: string;
  /** Who did it, when known: a name, or a generic "the system" / "an operator" / "an API key". */
  actor: string;
  /** Identifiers for the monospace secondary line (target first). */
  ids: string[];
}

type Side = "before" | "after";

function field(record: AuditRecordOut, side: Side, key: string): string | null {
  const section = record.diff?.[side];
  if (typeof section !== "object" || section === null) return null;
  const value = (section as Record<string, unknown>)[key];
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** "member.role_changed" -> "Member role changed" for actions without a dedicated sentence. */
export function humanizeAction(action: string): string {
  const words = action.replace(/[._]+/g, " ").trim();
  return words === "" ? "Activity recorded" : words.charAt(0).toUpperCase() + words.slice(1);
}

const PROVIDER_ACCOUNT_SENTENCES: Record<string, string> = {
  created: "Provider account added",
  credentials_replaced: "Provider account credentials replaced",
  activated: "Provider account activated",
  disabled: "Provider account disabled",
  status_changed: "Provider account status changed",
};

function quoted(label: string, name: string | null, fallback: string, verb: string): string {
  return name ? `${label} "${name}" ${verb}` : fallback;
}

function sentenceFor(record: AuditRecordOut, names: OperatorNames): string {
  const { action } = record;
  const target = record.target_id ? names.get(record.target_id) : undefined;
  const name = field(record, "after", "name") ?? field(record, "before", "name");

  switch (action) {
    case "member.added": {
      const role = field(record, "after", "role");
      if (!target && !role) return "Member added";
      return `${target ?? "A member"} was added${role ? ` as ${role}` : ""}`;
    }
    case "member.role_changed": {
      const from = field(record, "before", "role");
      const to = field(record, "after", "role");
      if (!to) return "Member role changed";
      return `${target ?? "A member"} is now ${to}${from ? ` (was ${from})` : ""}`;
    }
    case "member.removed":
      return target ? `${target} was removed from the project` : "Member removed";
    case "customer.created":
      return quoted("Customer", name, "Customer created", "was created");
    case "customer.updated":
      return quoted("Customer", name, "Customer updated", "was updated");
    case "customer.anonymized":
      return "Customer anonymized";
    case "api_key.created":
      return quoted("API key", name, "API key created", "was created");
    case "api_key.revoked":
      return quoted("API key", name, "API key revoked", "was revoked");
    case "project.created":
      return quoted("Project", name, "Project created", "was created");
    default: {
      if (action.startsWith("provider_account.")) {
        return PROVIDER_ACCOUNT_SENTENCES[action.slice("provider_account.".length)] ?? "Provider account updated";
      }
      return humanizeAction(action);
    }
  }
}

function actorFor(record: AuditRecordOut, names: OperatorNames): string {
  if (record.actor_type === "operator") return (record.actor_id ? names.get(record.actor_id) : undefined) ?? "an operator";
  if (record.actor_type === "api_key") return "an API key";
  return "the system";
}

/** Turns an audit record into display text using only what the audit endpoint returns. */
export function describeActivity(record: AuditRecordOut, names: OperatorNames): ActivityLine {
  return {
    sentence: sentenceFor(record, names),
    actor: actorFor(record, names),
    ids: [record.target_id, record.actor_type !== "system" ? record.actor_id : null].filter(
      (id): id is string => id !== null && id !== "",
    ),
  };
}
