/*
 * Same-origin API client. The session is an HttpOnly cookie (`fh_session`); unsafe requests also carry
 * `X-CSRF-Token`, whose value lives only in memory (handed out by /v1/auth/login and /v1/auth/me, never
 * written to any storage). Errors are RFC 9457 problem+json and surface as ApiError.
 */

export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly title: string;
  readonly detail: string | null;
  /** Seconds to wait, from the Retry-After header (429/503); null when absent. */
  readonly retryAfterSeconds: number | null;

  constructor(init: {
    status: number;
    type?: string;
    title: string;
    detail?: string | null;
    retryAfterSeconds?: number | null;
  }) {
    super(init.detail ?? init.title);
    this.name = "ApiError";
    this.status = init.status;
    this.type = init.type ?? "about:blank";
    this.title = init.title;
    this.detail = init.detail ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }

  /** The request never reached the API (offline, DNS, CORS, connection refused). */
  get isNetworkError(): boolean {
    return this.status === 0;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export function clearCsrfToken(): void {
  csrfToken = null;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

/** Registers what happens on a 401 from any request that has not opted out (the app redirects to /login). */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface ApiRequestInit {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** JSON-serialised when present. */
  body?: unknown;
  signal?: AbortSignal;
  /** Set false for requests whose 401 is an expected outcome (login, the route guard's session probe). */
  redirectOn401?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseRetryAfter(header: string | null): number | null {
  if (header === null) return null;
  const trimmed = header.trim();
  if (/^\d+$/.test(trimmed)) return Math.max(0, Math.trunc(Number(trimmed)));
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - Date.now()) / 1000));
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfterSeconds = parseRetryAfter(response.headers.get("Retry-After"));
  let problem: ProblemDetails = {};
  try {
    const body: unknown = await response.json();
    if (isRecord(body)) {
      problem = {
        type: typeof body.type === "string" ? body.type : undefined,
        title: typeof body.title === "string" ? body.title : undefined,
        detail: typeof body.detail === "string" ? body.detail : undefined,
      };
    }
  } catch {
    // Not JSON (proxy error pages, empty bodies): fall back to the HTTP status text.
  }
  return new ApiError({
    status: response.status,
    type: problem.type,
    title: problem.title ?? (response.statusText || `HTTP ${response.status}`),
    detail: problem.detail,
    retryAfterSeconds,
  });
}

export async function apiFetch<T = void>(path: string, init: ApiRequestInit = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json, application/problem+json" };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE_METHODS.has(method) && csrfToken !== null) headers["X-CSRF-Token"] = csrfToken;

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "include",
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError({ status: 0, title: "Network error", detail: "The API could not be reached." });
  }

  if (!response.ok) {
    const error = await toApiError(response);
    if (response.status === 401 && init.redirectOn401 !== false) unauthorizedHandler?.();
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
