import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const backend = process.env.PX_API_URL ?? "http://localhost:8010";
const proxied = ["/api", "/l", "/p", "/u", "/c", "/r", "/files", "/public", "/legal", "/oauth", "/webhooks", "/privacy"];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: backend, changeOrigin: false }])),
  },
  build: {
    target: "es2022",
    sourcemap: true,
    // No inline scripts: the API serves the app with a strict CSP (script-src 'self').
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        // Long-lived vendor chunks: app deploys don't invalidate the browser's cache of these.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return "react";
          if (id.includes("@tanstack")) return "tanstack";
          if (id.includes("@radix-ui") || id.includes("cmdk")) return "radix";
          if (id.includes("lucide-react")) return "icons";
          return "vendor";
        },
      },
    },
  },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"] },
});
