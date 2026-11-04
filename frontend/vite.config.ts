/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type ProxyOptions } from "vite";

/**
 * Proxies /api to the backend. A stopped backend is a refused connection, which Vite would answer with a bare 500;
 * this answers 502 with a header the page recognises (src/lib/api.ts), so it can say the backend is not running.
 * The listener is added in `configure`, which Vite calls before installing its own handler.
 */
function apiProxy(backend: string): ProxyOptions {
  return {
    target: backend,
    changeOrigin: false,
    configure: (proxy) => {
      proxy.on("error", (_error, _request, response) => {
        if (!("writeHead" in response) || response.headersSent || response.writableEnded) return;
        response
          .writeHead(502, { "Content-Type": "application/json", "X-DevPilot-Proxy": "backend-unreachable" })
          .end(JSON.stringify({ detail: `The DevPilot backend at ${backend} could not be reached.` }));
      });
    },
  };
}

// The browser talks only to this dev server. Requests under /api are proxied to the FastAPI backend, so the backend's
// CORS list does not need to change and no backend address is baked into the bundle. Nothing secret is configured here:
// the API token is typed in the page by the user and kept in sessionStorage.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.DEVPILOT_BACKEND_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "localhost",
      port: 5173,
      strictPort: true,
      proxy: { "/api": apiProxy(backend) },
    },
    preview: { host: "localhost", port: 4173, proxy: { "/api": apiProxy(backend) } },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
    },
  };
});
