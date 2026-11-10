import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.VITE_PROXY_TARGET ?? "http://localhost:8000", changeOrigin: true },
      "/docs": { target: process.env.VITE_PROXY_TARGET ?? "http://localhost:8000", changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Function form: the object form also moved modules shared with the entry (e.g. small helpers
        // recharts depends on) into "charts", which made every page preload the 420 kB chart library.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          // tiny helpers used by the app shell AND by recharts: keep them out of the chart chunk
          if (/[\/]node_modules[\/](clsx|tailwind-merge|class-variance-authority)[\/]/.test(id)) return "ui";
          if (/[\/]node_modules[\/](recharts|recharts-scale|react-smooth|victory-vendor|d3-[^\/]+)[\/]/.test(id)) return "charts";
          if (/[\/]node_modules[\/](react|react-dom|react-router|react-router-dom|scheduler|@remix-run)[\/]/.test(id)) return "react";
          if (/[\/]node_modules[\/](@tanstack|react-hook-form|zod|@hookform)[\/]/.test(id)) return "data";
          if (/[\/]node_modules[\/](@radix-ui|lucide-react|sonner)[\/]/.test(id)) return "ui";
          return undefined;
        },
      },
    },
  },
  test: { environment: "node" },
} as never);
