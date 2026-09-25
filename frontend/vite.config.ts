import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The API runs as its own process; proxying keeps the browser on one
      // origin so there is no CORS dance in development.
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
      // The live chart's trades arrive over a WebSocket from the same API.
      "/ws": { target: "ws://localhost:8000", ws: true, changeOrigin: true },
    },
  },
});
