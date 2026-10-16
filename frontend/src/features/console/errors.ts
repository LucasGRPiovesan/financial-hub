import { isApiError, problemMessage } from "../../lib/api";

export function errorText(error: unknown): string {
  if (isApiError(error) && error.isNetworkError) return "The API could not be reached.";
  return problemMessage(error, "Something went wrong.");
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
    if (name && item.detail && fields[name] === undefined) fields[name] = item.detail;
  }
  return { fields, form: Object.keys(fields).length > 0 ? null : errorText(error) };
}
