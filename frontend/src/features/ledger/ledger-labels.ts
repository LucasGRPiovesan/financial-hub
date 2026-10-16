import { camelKey, useI18n } from "../../i18n";

// Screens registered from the SCREENS table are not in the router's static route types, so these paths are plain strings.
export const LEDGER_PATH: string = "/ledger";

/** "customer_receivable:0190…" -> the account family and, when the account is per owner, the owner's id. */
export function splitAccountCode(code: string): { family: string; owner: string | null } {
  const at = code.indexOf(":");
  return at < 0 ? { family: code, owner: null } : { family: code.slice(0, at), owner: code.slice(at + 1) };
}

function humanize(code: string): string {
  const words = code.replace(/[._:]+/g, " ").trim();
  return words === "" ? code : words.charAt(0).toUpperCase() + words.slice(1);
}

/** The readable name of an account family ("platform_revenue" -> "Receita da plataforma"). */
export function useFamilyName(): (family: string) => string {
  const { tLoose } = useI18n();
  return (family) => tLoose("ledger.account", camelKey(family), humanize(family));
}

