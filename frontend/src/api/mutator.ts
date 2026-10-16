/*
 * Orval mutator (fetch signature: `(url, RequestInit)`): every generated hook funnels through apiFetch, so
 * cookies, the CSRF header on unsafe methods, problem+json -> ApiError and the 401 redirect behave exactly
 * like the hand-written client. Orval builds the full URL (path + query string) and a JSON-string body.
 */
import { apiFetch, type ApiError } from "../lib/api";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

function parseBody(body: RequestInit["body"]): unknown {
  if (body === undefined || body === null) return undefined;
  if (typeof body === "string") return JSON.parse(body) as unknown;
  throw new TypeError("apiMutator only supports JSON request bodies");
}

/** Only the idempotency key is forwarded from the generated call; every other header is owned by apiFetch. */
function forwardedHeaders(headers: RequestInit["headers"]): Record<string, string> | undefined {
  if (!headers) return undefined;
  const key = new Headers(headers).get("Idempotency-Key");
  return key === null ? undefined : { "Idempotency-Key": key };
}

export function apiMutator<T>(url: string, options: RequestInit = {}): Promise<T> {
  return apiFetch<T>(url, {
    method: (options.method ?? "GET").toUpperCase() as Method,
    body: parseBody(options.body),
    signal: options.signal ?? undefined,
    headers: forwardedHeaders(options.headers),
  });
}

/** Error type for generated hooks' `TError`. */
export type ErrorType<E> = ApiError & { readonly _body?: E };
