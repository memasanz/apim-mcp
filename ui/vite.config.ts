import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds into ../server/app/static so the FastAPI app serves it at /admin.
export default defineConfig({
  plugins: [react()],
  base: "/admin/",
  build: {
    outDir: "../server/app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/admin/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
      "/mcp": "http://localhost:8000",
    },
  },
});
