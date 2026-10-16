import { useCallback, useMemo, useSyncExternalStore } from "react";

import { getLocale, setLocale, subscribeLocale, type Locale } from "../lib/locale";
import { translateLoose, translatorFor } from "./translate";
import type { Translate } from "./types";

/** The current language; the component re-renders when it changes. */
export function useLocale(): { locale: Locale; setLocale: (locale: Locale) => void } {
  const locale = useSyncExternalStore(subscribeLocale, getLocale, getLocale);
  return { locale, setLocale };
}

export interface I18n {
  locale: Locale;
  t: Translate;
  /** For values the API sends (statuses, roles): `tLoose("roles", value, value)` -> "roles.<value>" or the fallback. */
  tLoose: (prefix: string, value: string, fallback?: string) => string;
}

/** `const { t } = useI18n()`: translation bound to the current language; re-renders on a language switch. */
export function useI18n(): I18n {
  const { locale } = useLocale();
  const tLoose = useCallback(
    (prefix: string, value: string, fallback?: string) => translateLoose(locale, `${prefix}.${value}`, fallback ?? value),
    [locale],
  );
  return useMemo(() => ({ locale, t: translatorFor(locale), tLoose }), [locale, tLoose]);
}

/** Shorthand for components that only need `t`. */
export function useT(): Translate {
  return useI18n().t;
}
