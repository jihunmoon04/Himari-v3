// HIMARI's sandbox proxy for Tool views (MCP Apps 2026-01-26, V3-P58).
//
// Served from the sandbox origin with the view's CSP as its response header.
// It frames one view in an opaque srcdoc iframe (sandbox="allow-scripts":
// no storage, no access to this page or the host) — the view inherits this
// page's CSP — and relays JSON-RPC messages between the host page and the
// view. Messages named ui/notifications/sandbox-* are for this proxy only.
// The host's "sandbox" and "permissions" values are ignored: never widened.
"use strict";

(() => {
  const HOST = document.querySelector('meta[name="himari-host-origin"]').content;
  const PREFIX = "ui/notifications/sandbox-";
  if (window.parent === window) return;

  const inner = document.createElement("iframe");
  inner.setAttribute("sandbox", "allow-scripts");
  inner.setAttribute("title", "Tool view");
  inner.style.cssText = "border: 0; width: 100%; height: 100%; display: block;";
  document.body.appendChild(inner);
  let loaded = false;

  const isMessage = (m) => m !== null && typeof m === "object" && !Array.isArray(m) && m.jsonrpc === "2.0";
  const forProxy = (m) => typeof m.method === "string" && m.method.startsWith(PREFIX);

  window.addEventListener("message", (event) => {
    const m = event.data;
    if (!isMessage(m)) return;
    if (event.source === window.parent) {
      if (event.origin !== HOST) return;
      if (m.method === "ui/notifications/sandbox-resource-ready") {
        const html = m.params && m.params.html;
        if (loaded || typeof html !== "string") return; // one view per proxy
        loaded = true;
        inner.srcdoc = html;
        return;
      }
      if (!forProxy(m) && loaded && inner.contentWindow) inner.contentWindow.postMessage(m, "*");
    } else if (event.source === inner.contentWindow) {
      if (loaded && !forProxy(m)) window.parent.postMessage(m, HOST);
    }
  });

  window.parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} }, HOST);
})();
