import { QueryClient } from "@tanstack/react-query";

import { isApiError } from "./api";

const MAX_RETRIES = 2;

/** Client errors (4xx) are final: retrying a 401/403/404/422 only delays the answer. 401 redirects via apiFetch. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < MAX_RETRIES;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: shouldRetry }, mutations: { retry: false } },
  });
}
