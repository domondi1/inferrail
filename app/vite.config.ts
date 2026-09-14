import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard is a static SPA served under /dashboard by
// `inferrail serve --app-mode` (docs/adr/0017) -- `base` must match that
// mount path so built asset URLs resolve correctly regardless of which
// route on the FastAPI app served index.html.
export default defineConfig({
  base: "/dashboard/",
  plugins: [react()],
  build: {
    outDir: "dist",
  },
  server: {
    // Local dev only (`npm run dev`): proxy local-API calls to a real
    // `inferrail serve --app-mode` instance so the dashboard can be
    // developed without rebuilding on every change. Not used in
    // production -- there, the same FastAPI process serves both.
    proxy: {
      "/v1/local": "http://127.0.0.1:8000",
    },
  },
});
