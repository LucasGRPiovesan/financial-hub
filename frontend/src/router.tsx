import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  redirect,
  type RouterHistory,
} from "@tanstack/react-router";

import { meQueryOptions } from "./features/auth/auth-api";
import { LoginPage } from "./features/auth/login-page";
import { safeRedirectTarget } from "./features/auth/redirect";
import { AppShell } from "./features/shell/app-shell";
import { NAV_ITEMS } from "./features/shell/nav";
import { isApiError } from "./lib/api";
import { BootScreen, RouteError } from "./routes/route-states";

// Screens load on demand so the first paint ships only the shell, sign-in and the router.
const OverviewPage = lazyRouteComponent(() => import("./routes/overview"), "OverviewPage");
const PaymentDetailPage = lazyRouteComponent(() => import("./routes/payments-detail"), "PaymentDetailPage");
const PlaceholderPage = lazyRouteComponent(() => import("./routes/placeholder"), "PlaceholderPage");
const SCREENS = {
  "/projects": lazyRouteComponent(() => import("./routes/projects"), "ProjectsPage"),
  "/api-keys": lazyRouteComponent(() => import("./routes/api-keys"), "ApiKeysPage"),
  "/members": lazyRouteComponent(() => import("./routes/members"), "MembersPage"),
  "/customers": lazyRouteComponent(() => import("./routes/customers"), "CustomersPage"),
  "/payments": lazyRouteComponent(() => import("./routes/payments"), "PaymentsPage"),
  "/integrations": lazyRouteComponent(() => import("./routes/integrations"), "IntegrationsPage"),
};

export interface RouterContext {
  queryClient: QueryClient;
}

function buildRouteTree() {
  const rootRoute = createRootRouteWithContext<RouterContext>()({
    component: Outlet,
    errorComponent: RouteError,
  });

  const loginRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/login",
    validateSearch: (search: Record<string, unknown>): { redirect?: string } => {
      const target = safeRedirectTarget(search.redirect);
      return target ? { redirect: target } : {};
    },
    pendingComponent: BootScreen,
    pendingMs: 0,
    pendingMinMs: 0,
    beforeLoad: async ({ context }) => {
      // Already signed in: skip the form.
      const signedIn = await context.queryClient.ensureQueryData(meQueryOptions).then(
        () => true,
        (error: unknown) => {
          if (isApiError(error) && error.status === 401) return false;
          throw error;
        },
      );
      if (signedIn) throw redirect({ to: "/" });
    },
    component: function Login() {
      const { redirect: redirectTo } = loginRoute.useSearch();
      return <LoginPage redirectTo={redirectTo} />;
    },
  });

  const appRoute = createRoute({
    getParentRoute: () => rootRoute,
    id: "app",
    beforeLoad: async ({ context, location }) => {
      try {
        await context.queryClient.ensureQueryData(meQueryOptions);
      } catch (error) {
        if (isApiError(error) && error.status === 401) {
          const target = safeRedirectTarget(location.href);
          throw redirect({ to: "/login", search: target ? { redirect: target } : {} });
        }
        throw error;
      }
    },
    component: AppShell,
    pendingComponent: BootScreen,
    pendingMs: 0,
    pendingMinMs: 0,
  });

  const overviewRoute = createRoute({ getParentRoute: () => appRoute, path: "/", component: OverviewPage });
  const screens: Record<string, typeof OverviewPage> = SCREENS;
  const screenRoutes = Object.entries(screens).map(([path, component]) =>
    createRoute({ getParentRoute: () => appRoute, path, component }),
  );
  const paymentDetailRoute = createRoute({ getParentRoute: () => appRoute, path: "/payments/$paymentId", component: PaymentDetailPage });
  const placeholderRoutes = NAV_ITEMS.filter((item) => item.to !== "/" && !(item.to in screens)).map((item) =>
    createRoute({
      getParentRoute: () => appRoute,
      path: item.to,
      component: function Placeholder() {
        return <PlaceholderPage item={item} />;
      },
    }),
  );

  return rootRoute.addChildren([loginRoute, appRoute.addChildren([overviewRoute, ...screenRoutes, paymentDetailRoute, ...placeholderRoutes])]);
}

/**
 * Cross-fade the page content between modules (the CSS in index.css keeps the sidebar and top bar still).
 * Skipped for same-path changes and when the visitor asked for reduced motion.
 */
function viewTransitionTypes({ pathChanged }: { pathChanged: boolean }): string[] | false {
  if (!pathChanged || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  return ["module"];
}

export function createAppRouter(queryClient: QueryClient, history?: RouterHistory) {
  return createRouter({
    routeTree: buildRouteTree(),
    context: { queryClient },
    history,
    defaultPreload: "intent",
    defaultViewTransition: { types: viewTransitionTypes },
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
