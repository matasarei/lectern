import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const BACKEND = "http://127.0.0.1:8765";

// The build goes inside the Python package so the wheel ships it; FastAPI serves it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/lectern/web_dist",
    emptyOutDir: true,
  },
  server: {
    // Dev only: run `lectern serve --no-browser`, then open the Vite page with the same
    // `#token=…` fragment that `lectern serve` printed. The proxy presents itself to the
    // backend as the backend's own origin, because the security layer refuses any other
    // Host or Origin.
    proxy: {
      "/api": {
        target: BACKEND,
        ws: true,
        changeOrigin: true, // Host becomes 127.0.0.1:8765
        headers: { origin: BACKEND }, // HTTP requests
        rewriteWsOrigin: true, // WebSocket upgrades
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
