import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // 很少變動的套件獨立打包：重新部署只改到自己的程式時，瀏覽器可以沿用快取
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
          charts: ["lightweight-charts"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": backend,
      "/webhook": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
