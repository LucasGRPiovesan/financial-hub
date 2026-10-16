import { getLocale } from "../lib/locale";
import { translate, translateLoose } from "./translate";
import type { MessageKey, MessageParams } from "./types";

export { useI18n, useLocale, useT, type I18n } from "./use-i18n";
export { camelKey, hasMessage, translate, translateLoose, translatorFor } from "./translate";
export type { MessageKey, MessageParams, Messages, Translate } from "./types";

/** Translation in the current language for code outside React; components should prefer `useT()` so they re-render. */
export function t(key: MessageKey, params?: MessageParams): string {
  return translate(getLocale(), key, params);
}

/** Outside-React twin of `tLoose`. */
export function tLoose(prefix: string, value: string, fallback?: string): string {
  return translateLoose(getLocale(), `${prefix}.${value}`, fallback ?? value);
}
