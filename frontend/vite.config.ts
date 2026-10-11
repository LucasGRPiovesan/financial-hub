import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

// Same origin as the SPA, so the HttpOnly session cookie and the CSRF header work without CORS.
// nginx.conf (production image) mirrors these routes.
const proxy = { target: apiTarget, changeOrigin: true } as const;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    watch: { usePolling: true },
    proxy: {
      "/v1": proxy,
      "/health": proxy,
      // Legacy prefix from the scaffold (docs/DEVELOPMENT.md): /api/x -> /x.
      "/api": { ...proxy, rewrite: (path) => path.replace(/^\/api/, "") },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
