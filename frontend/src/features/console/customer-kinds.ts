import type { CustomerKind } from "../../lib/br-document";

export const KIND_LABEL: Record<CustomerKind, string> = {
  individual: "Individual (CPF)",
  company: "Company (CNPJ)",
};

export function isKind(value: string): value is CustomerKind {
  return value === "individual" || value === "company";
}

export function kindLabel(kind: string): string {
  return isKind(kind) ? KIND_LABEL[kind] : kind;
}
