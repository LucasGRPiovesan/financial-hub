import type { AuditRecordOut } from "../../api/generated/model";
import { camelKey, hasMessage, t, tLoose, type MessageKey } from "../../i18n";
import { getLocale } from "../../lib/locale";

/** Operator display names by id (members plus the signed-in operator); anything missing falls back to generic wording. */
export type OperatorNames = ReadonlyMap<string, string>;

export interface ActivityLine {
  /** One readable sentence: "Bruno Admin was added as Admin". */
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

/** "member.role_changed" -> "Member role changed": the English fallback for actions without a dedicated sentence. */
export function humanizeAction(action: string): string {
  const words = action.replace(/[._]+/g, " ").trim();
  return words === "" ? "Activity recorded" : words.charAt(0).toUpperCase() + words.slice(1);
}

function unknownAction(action: string): string {
  // Action codes are data; only English has a readable rendering of them, other languages show the code itself.
  if (getLocale() === "en-US") return humanizeAction(action);
  return action.trim() === "" ? t("activity.recorded") : t("activity.unknown", { action });
}

function sentenceFor(record: AuditRecordOut, names: OperatorNames): string {
  const { action } = record;
  const target = record.target_id ? names.get(record.target_id) : undefined;
  const name = field(record, "after", "name") ?? field(record, "before", "name");
  const named = <K extends "customerCreated" | "customerUpdated" | "apiKeyCreated" | "apiKeyRevoked" | "projectCreated">(
    kind: K,
  ): string => (name ? t(`activity.${kind}Named`, { name }) : t(`activity.${kind}`));

  switch (action) {
    case "member.added": {
      const role = field(record, "after", "role");
      if (!target && !role) return t("activity.memberAdded");
      const who = target ?? t("activity.aMember");
      return role ? t("activity.memberAddedAs", { who, role: tLoose("roles", role) }) : t("activity.memberAddedWho", { who });
    }
    case "member.role_changed": {
      const from = field(record, "before", "role");
      const to = field(record, "after", "role");
      if (!to) return t("activity.roleChanged");
      const who = target ?? t("activity.aMember");
      return from
        ? t("activity.roleChangedFrom", { who, role: tLoose("roles", to), from: tLoose("roles", from) })
        : t("activity.roleChangedTo", { who, role: tLoose("roles", to) });
    }
    case "member.removed":
      return target ? t("activity.memberRemovedNamed", { who: target }) : t("activity.memberRemoved");
    case "customer.created":
      return named("customerCreated");
    case "customer.updated":
      return named("customerUpdated");
    case "customer.anonymized":
      return t("activity.customerAnonymized");
    case "api_key.created":
      return named("apiKeyCreated");
    case "api_key.revoked":
      return named("apiKeyRevoked");
    case "project.created":
      return named("projectCreated");
    default: {
      if (action.startsWith("provider_account.")) {
        const kind = action.slice("provider_account.".length);
        const key = `activity.providerAccount.${camelKey(kind)}`;
        return hasMessage(getLocale(), key) ? t(key as MessageKey) : t("activity.providerAccount.updated");
      }
      return unknownAction(action);
    }
  }
}

function actorFor(record: AuditRecordOut, names: OperatorNames): string {
  if (record.actor_type === "operator") return (record.actor_id ? names.get(record.actor_id) : undefined) ?? t("activity.actors.operator");
  if (record.actor_type === "api_key") return t("activity.actors.apiKey");
  return t("activity.actors.system");
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
