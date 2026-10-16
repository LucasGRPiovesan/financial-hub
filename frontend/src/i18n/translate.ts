import type { Locale } from "../lib/locale";
import { enUS } from "./en-US";
import { ptBR } from "./pt-BR";
import type { Messages, MessageKey, MessageParams, PluralForms, Translate } from "./types";

const DICTIONARIES: Record<Locale, Messages> = { "en-US": enUS, "pt-BR": ptBR };

type Node = string | PluralForms | { readonly [key: string]: Node };

function isPlural(node: Node): node is PluralForms {
  return typeof node === "object" && typeof (node as { other?: unknown }).other === "string";
}

function lookup(locale: Locale, key: string): string | PluralForms | undefined {
  let node: Node | undefined = DICTIONARIES[locale] as unknown as Node;
  for (const part of key.split(".")) {
    if (node === undefined || typeof node === "string" || isPlural(node)) return undefined;
    node = node[part];
  }
  if (node === undefined) return undefined;
  return typeof node === "string" || isPlural(node) ? node : undefined;
}

const pluralRules = new Map<Locale, Intl.PluralRules>();
const numberFormats = new Map<Locale, Intl.NumberFormat>();

function pluralCategory(locale: Locale, count: number): Intl.LDMLPluralRule {
  let rules = pluralRules.get(locale);
  if (!rules) {
    rules = new Intl.PluralRules(locale);
    pluralRules.set(locale, rules);
  }
  return rules.select(count);
}

function formatNumber(locale: Locale, value: number): string {
  let format = numberFormats.get(locale);
  if (!format) {
    format = new Intl.NumberFormat(locale);
    numberFormats.set(locale, format);
  }
  return format.format(value);
}

/**
 * Looks `key` up in `locale` and fills `{placeholders}` from `params`. A plural message picks its form from
 * `params.count` (Intl.PluralRules) and falls back to `other`; numbers are formatted for the locale.
 * A key absent at runtime (only possible through `translateLoose`) renders as itself.
 */
export function translate(locale: Locale, key: MessageKey, params: MessageParams = {}): string {
  return render(locale, key, params);
}

function render(locale: Locale, key: string, params: MessageParams): string {
  const message = lookup(locale, key);
  if (message === undefined) return key;
  let template: string;
  if (typeof message === "string") {
    template = message;
  } else {
    const count = params.count;
    const category = typeof count === "number" ? pluralCategory(locale, count) : "other";
    template = message[category] ?? message.other;
  }
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => {
    const value = params[name];
    if (value === undefined) return placeholder;
    return typeof value === "number" ? formatNumber(locale, value) : value;
  });
}

/** True when `key` names a message in `locale` (for values that come from the API, e.g. a status). */
export function hasMessage(locale: Locale, key: string): boolean {
  return lookup(locale, key) !== undefined;
}

/** Translates a key built at runtime (`status.${value}`); unknown keys give `fallback` instead of the key. */
export function translateLoose(locale: Locale, key: string, fallback: string, params: MessageParams = {}): string {
  return hasMessage(locale, key) ? render(locale, key, params) : fallback;
}

export function translatorFor(locale: Locale): Translate {
  return (key, params) => translate(locale, key, params);
}

export function messagesFor(locale: Locale): Messages {
  return DICTIONARIES[locale];
}

/** "api_keys:manage" / "customer-anonymized" / "string_too_short" -> "apiKeysManage" / "customerAnonymized" / "stringTooShort". */
export function camelKey(value: string): string {
  return value
    .split(/[-_:.\s]+/)
    .filter(Boolean)
    .map((part, index) => (index === 0 ? part.toLowerCase() : part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()))
    .join("");
}
