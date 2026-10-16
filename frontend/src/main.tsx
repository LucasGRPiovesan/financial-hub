import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import { ToastProvider } from "./components/ui/toast";
import { installUnauthorizedRedirect } from "./features/auth/session";
import { initLocale } from "./lib/locale";
import { createQueryClient } from "./lib/query-client";
import { initTheme } from "./lib/theme";
import { createAppRouter } from "./router";

initTheme();
initLocale();

const queryClient = createQueryClient();
const router = createAppRouter(queryClient);
installUnauthorizedRedirect(router, queryClient);

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
