import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";

import { HomePage } from "./routes/home";

const rootRoute = createRootRoute({ component: Outlet });
const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: HomePage });

export const router = createRouter({ routeTree: rootRoute.addChildren([homeRoute]) });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
