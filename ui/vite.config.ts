import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `bun run dev` proxies /api to the capforge web listener; `bun run build`
// emits ui/dist, which the server serves when CAPFORGE_UI_DIR points at it.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8080",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
