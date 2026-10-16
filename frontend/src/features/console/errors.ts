import { camelKey, hasMessage, t, type MessageKey } from "../../i18n";
import { isApiError, type ApiError } from "../../lib/api";
import { getLocale } from "../../lib/locale";

const PROBLEM_TYPE_BASE = "https://financial-hub.dev/problems/";

function problemSlug(error: ApiError): string | null {
  return error.type.startsWith(PROBLEM_TYPE_BASE) ? error.type.slice(PROBLEM_TYPE_BASE.length) : null;
}

function statusKey(status: number): MessageKey | null {
  if (status >= 500) return "errors.byStatus.s5xx";
  switch (status) {
    case 401:
      return "errors.byStatus.s401";
    case 403:
      return "errors.byStatus.s403";
    case 404:
      return "errors.byStatus.s404";
    case 409:
      return "errors.byStatus.s409";
    case 422:
      return "errors.byStatus.s422";
    case 429:
      return "errors.byStatus.s429";
    default:
      return null;
  }
}

/**
 * The message for a failed API call. The API's `detail` is English, so outside en-US it is never the only text:
 * a known problem `type` slug (or the HTTP status) maps to a translated sentence, an unknown failure gets the
 * translated generic message with the server's detail as secondary text.
 */
export function errorText(error: unknown): string {
  if (!isApiError(error)) return t("errors.generic");
  if (error.isNetworkError) return t("errors.network");
  const english = getLocale() === "en-US";
  if (english && error.detail) return error.detail;

  const slug = problemSlug(error);
  const problemKey = slug ? `errors.problems.${camelKey(slug)}` : null;
  if (problemKey && hasMessage(getLocale(), problemKey)) return t(problemKey as MessageKey);
  const byStatus = statusKey(error.status);
  if (byStatus) return t(byStatus);
  const generic = t("errors.generic");
  return error.detail && !english ? `${generic} (${error.detail})` : generic;
}

function fieldText(code: string | undefined, detail: string | undefined): string | null {
  if (getLocale() === "en-US" && detail) return detail;
  if (code) {
    const key = `errors.fieldCodes.${camelKey(code)}`;
    if (hasMessage(getLocale(), key)) return t(key as MessageKey);
  }
  return detail ? t("errors.fieldInvalid") : null;
}

/**
 * problem+json `errors[]` -> `{ "slug": "...", "credentials.api_key": "..." }` (RFC 6901 pointer, dots for slashes).
 * `form` collects what has no field to sit on.
 */
export function fieldErrorsOf(error: unknown): { fields: Record<string, string>; form: string | null } {
  const fields: Record<string, string> = {};
  if (!isApiError(error)) return { fields, form: null };
  for (const item of error.errors ?? []) {
    const name = (item.pointer ?? "").replace(/^\//, "").replaceAll("/", ".");
    const text = fieldText(item.code, item.detail);
    if (name && text && fields[name] === undefined) fields[name] = text;
  }
  return { fields, form: Object.keys(fields).length > 0 ? null : errorText(error) };
}
