import type { QueryClient } from "@tanstack/react-query";
import type { AnyRouter } from "@tanstack/react-router";

import { clearCsrfToken, setUnauthorizedHandler } from "../../lib/api";
import { safeRedirectTarget } from "./redirect";

/** Drops everything tied to the signed-in operator (cached data and the in-memory CSRF token). */
export function clearSession(queryClient: QueryClient): void {
  clearCsrfToken();
  queryClient.clear();
}

/** 401 from any API call -> forget the session and send the operator to /login, remembering where they were. */
export function installUnauthorizedRedirect(router: AnyRouter, queryClient: QueryClient): () => void {
  setUnauthorizedHandler(() => {
    clearSession(queryClient);
    const here = router.state.location;
    if (here.pathname === "/login") return;
    const redirect = safeRedirectTarget(here.href);
    void router.navigate({ to: "/login", search: redirect ? { redirect } : {} });
  });
  return () => setUnauthorizedHandler(null);
}
