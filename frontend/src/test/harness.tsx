import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { vi } from "vitest";

import { ToastProvider } from "../components/ui/toast";
import type { MeResponse } from "../features/auth/auth-api";
import { installUnauthorizedRedirect } from "../features/auth/session";
import { clearCsrfToken, setUnauthorizedHandler } from "../lib/api";
import { setLocale } from "../lib/locale";
import { createAppRouter } from "../router";

export const ME: MeResponse = {
  operator: { id: "op-1", email: "ana@example.test", display_name: "Ana Souza" },
  memberships: [
    { project_id: "p-1", project_name: "Acme Payments", role: "owner" },
    { project_id: "p-2", project_name: "Beta Billing", role: "viewer" },
  ],
  csrf_token: "csrf-from-me",
};

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": status >= 400 ? "application/problem+json" : "application/json", ...headers },
  });
}

export type Handler = (request: { path: string; init: RequestInit }) => Response | Promise<Response>;

/** Routes `fetch` by "METHOD /path"; anything unmatched fails the test loudly. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { key: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const key = `${init.method ?? "GET"} ${path}`;
    calls.push({ key, init });
    const handler = routes[key];
    if (!handler) return Promise.reject(new Error(`Unmocked request: ${key}`));
    return Promise.resolve(handler({ path, init }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

export function resetApiState() {
  clearCsrfToken();
  setUnauthorizedHandler(null);
  window.localStorage.clear();
  // Most suites assert the English copy; the i18n suites cover the pt-BR default and the switch.
  setLocale("en-US");
}

export function renderApp(initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const history = createMemoryHistory({ initialEntries: [initialPath] });
  const router = createAppRouter(queryClient, history);
  installUnauthorizedRedirect(router, queryClient);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>,
  );
  return { ...view, router, queryClient, history };
}
