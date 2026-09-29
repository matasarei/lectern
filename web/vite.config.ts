import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The build goes inside the Python package so the wheel ships it; FastAPI serves it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/lectern/web_dist",
    emptyOutDir: true,
  },
  server: {
    // Dev only: the page talks to `lectern serve` on the same origin through this proxy.
    proxy: { "/api": { target: "http://127.0.0.1:8765", ws: true } },
  },
  test: {
    environment: "jsdom",
  },
});
