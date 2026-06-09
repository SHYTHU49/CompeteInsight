import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_API_TARGET || "http://localhost:8000";
  const wsTarget = apiTarget.replace(/^http/, "ws");

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": "/src",
      },
    },
    server: {
      port: 5173,
      strictPort: false,
      proxy: {
        // 后端 REST
        "/api": {
          target: apiTarget,
          changeOrigin: true,
        },
        // 后端静态文件（快照 / 导出）
        "/files": {
          target: apiTarget,
          changeOrigin: true,
        },
        // WebSocket
        "/ws": {
          target: wsTarget,
          ws: true,
        },
      },
    },
    build: {
      sourcemap: true,
      outDir: "dist",
    },
  };
});
