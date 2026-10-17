import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 开发时先运行 `uv run voicedetect serve --token dev`，再 `npm run dev`，打开 http://localhost:5173/#token=dev
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../voicedetect/web",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
  },
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8765", ws: true } },
  },
  test: {
    environment: "jsdom",
  },
});
