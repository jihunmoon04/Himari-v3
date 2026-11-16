/// <reference types="vitest/config" />
// The new Web page (V3-P102 §7). Built into the Python package's dist/ and served at /next/ beside the old page
// until W6. The assistant-text renderer is the old page's audited rich_text.js, imported as it is (one copy).
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));
const server = process.env.HIMARI_DEV_SERVER ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "/next/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": here("./src"),
      "@static": here("../src/himari/interfaces/web/static"),
    },
  },
  build: {
    outDir: here("../src/himari/interfaces/web/dist"),
    emptyOutDir: true,
    assetsInlineLimit: 0, // a font or image is a file of its own, never a data: URL the CSP would refuse
    modulePreload: { polyfill: false }, // nothing inline in index.html
    sourcemap: false,
    chunkSizeWarningLimit: 700, // one chunk of React, Radix and the renderer; split when the page grows (W2+)
  },
  server: {
    fs: { allow: [here(".."), here(".")] },
    proxy: {
      // the dev server is another origin: the API's Origin check sees the real server's own
      "/api": { target: server, changeOrigin: true, headers: { origin: server } },
      "/assets/katex": { target: server, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
