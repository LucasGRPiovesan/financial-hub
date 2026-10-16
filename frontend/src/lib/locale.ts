/*
 * The console language (pt-BR by default, switchable at runtime). This is the one store that money, dates and the
 * i18n layer read, so it lives outside React: `getLocale()` works anywhere, `useLocale()` (src/i18n) subscribes.
 * Time zones still follow the operator's browser. The browser language is deliberately NOT consulted:
 * Portuguese is the product default.
 */

export type Locale = "pt-BR" | "en-US";

export const LOCALES: readonly Locale[] = ["pt-BR", "en-US"];
export const DEFAULT_LOCALE: Locale = "pt-BR";
export const LOCALE_STORAGE_KEY = "fh.locale";

const listeners = new Set<() => void>();
let current: Locale = DEFAULT_LOCALE;

export function isLocale(value: unknown): value is Locale {
  return LOCALES.some((locale) => locale === value);
}

export function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return isLocale(stored) ? stored : DEFAULT_LOCALE;
  } catch {
    return DEFAULT_LOCALE;
  }
}

function applyHtmlLang(locale: Locale): void {
  if (typeof document !== "undefined") document.documentElement.setAttribute("lang", locale);
}

/** The current UI locale as a BCP 47 tag; what money and date formatting default to. */
export function getLocale(): Locale {
  return current;
}

/** Changes the language everywhere at once, remembers it when storage allows, and updates <html lang>. */
export function setLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Storage can be blocked (private windows, site-data settings): the choice still applies to this page.
  }
  current = locale;
  applyHtmlLang(locale);
  listeners.forEach((listener) => listener());
}

export function subscribeLocale(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Called once before first render: restores the stored choice (or the default) and sets <html lang>. */
export function initLocale(): void {
  current = readStoredLocale();
  applyHtmlLang(current);
}
