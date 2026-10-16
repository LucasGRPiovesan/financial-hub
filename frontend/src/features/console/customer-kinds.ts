import { t } from "../../i18n";
import type { CustomerKind } from "../../lib/br-document";

export function isKind(value: string): value is CustomerKind {
  return value === "individual" || value === "company";
}

export function kindLabel(kind: string): string {
  return isKind(kind) ? t(`customerKind.${kind}`) : kind;
}
