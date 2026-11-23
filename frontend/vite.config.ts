import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the FastAPI backend so the SPA calls same-origin
// (no CORS) and local-mode identity just works.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.DVP_API_URL || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
