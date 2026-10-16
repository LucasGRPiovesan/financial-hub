/*
 * CPF / CNPJ validation, mirroring backend/.../customers/domain/documents.py so the form can say what is wrong
 * before the request. The API stays the authority; this only saves a round trip and never stores anything.
 */
const FORMATTING = /[.\-/\s]/g;

export type CustomerKind = "individual" | "company";

export const CPF_LENGTH = 11;
export const CNPJ_LENGTH = 14;

export function documentDigits(raw: string): string {
  return raw.replace(FORMATTING, "");
}

function digitsOf(value: string): number[] {
  return Array.from(value, (char) => Number(char));
}

function allSame(value: string): boolean {
  return new Set(value).size === 1;
}

function cpfDigit(numbers: number[]): number {
  const weight = numbers.length + 1;
  const total = numbers.reduce((sum, digit, index) => sum + digit * (weight - index), 0);
  return ((total * 10) % 11) % 10;
}

function cnpjDigit(numbers: number[], weights: readonly number[]): number {
  const remainder = numbers.reduce((sum, digit, index) => sum + digit * (weights[index] ?? 0), 0) % 11;
  return remainder < 2 ? 0 : 11 - remainder;
}

const CNPJ_WEIGHTS = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2] as const;

export function isValidCpf(digits: string): boolean {
  if (digits.length !== CPF_LENGTH || allSame(digits)) return false;
  const numbers = digitsOf(digits);
  const first = cpfDigit(numbers.slice(0, 9));
  return numbers[9] === first && numbers[10] === cpfDigit([...numbers.slice(0, 9), first]);
}

export function isValidCnpj(digits: string): boolean {
  if (digits.length !== CNPJ_LENGTH || allSame(digits)) return false;
  const numbers = digitsOf(digits);
  const first = cnpjDigit(numbers.slice(0, 12), CNPJ_WEIGHTS);
  const second = cnpjDigit([...numbers.slice(0, 12), first], [6, ...CNPJ_WEIGHTS]);
  return numbers[12] === first && numbers[13] === second;
}

/** A form error for `raw` as a document of `kind`, or null when it is valid. The message never echoes the input. */
export function documentError(raw: string, kind: CustomerKind): string | null {
  const digits = documentDigits(raw);
  if (!/^\d+$/.test(digits)) return "Use digits only (dots, dashes and slashes are fine).";
  if (kind === "individual") {
    if (digits.length !== CPF_LENGTH) return "A CPF has 11 digits.";
    return isValidCpf(digits) ? null : "This CPF's check digits are wrong.";
  }
  if (digits.length !== CNPJ_LENGTH) return "A CNPJ has 14 digits.";
  return isValidCnpj(digits) ? null : "This CNPJ's check digits are wrong.";
}

/** The same rule the API enforces on `external_ref`. */
export function externalRefError(value: string): string | null {
  if (value.length === 0) return "Enter the reference your system uses for this customer.";
  if (value.length > 128) return "Use at most 128 characters.";
  return /^[A-Za-z0-9._:@+=~-]+$/.test(value) ? null : "Use letters, digits and . _ : @ + = ~ - only.";
}
