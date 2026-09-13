import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig(({mode}) => ({
  plugins: [react()],
  server: { proxy: { "/api": { target: loadEnv(mode, ".", "VITE_").VITE_API_PROXY || "http://127.0.0.1:8787", ws: true } } },
}));
