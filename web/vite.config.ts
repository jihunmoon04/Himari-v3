/// <reference types="vitest/config" />
// HIMARI's Web page (V3-P102 §7). Built into the Python package's dist/ and served at /. The assistant-text renderer
// is the audited lib/rich_text.js (the Windows companion builds the same file in); KaTeX is public/assets/katex, as
// the renderer loads it (/assets/katex/…).
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));
const server = process.env.HIMARI_DEV_SERVER ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": here("./src"),
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
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
