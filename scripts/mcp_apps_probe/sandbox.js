// The proxy (sandbox origin, allow-scripts allow-same-origin), shaped like the V3-P58 design.
const HOSTS = document.querySelector('meta[name="himari-host-origins"]').content.split(" ");
const proxyReport = {};
document.addEventListener("securitypolicyviolation", (e) => {
  (proxyReport.violations ||= []).push(e.effectiveDirective + " " + e.blockedURI);
});
window.addEventListener("DOMContentLoaded", () => {
  const inner = document.createElement("iframe");
  inner.setAttribute("sandbox", "allow-scripts");
  document.body.appendChild(inner);
  window.addEventListener("message", (ev) => {
    if (ev.source === parent) {
      if (!HOSTS.includes(ev.origin)) return;
      const m = ev.data;
      if (m && m.method === "ui/notifications/sandbox-resource-ready") {
        inner.srcdoc = m.params.html;
        return;
      }
      inner.contentWindow.postMessage(m, "*");
    } else if (ev.source === inner.contentWindow) {
      const m = ev.data;
      if (m && m.method === "notifications/message") m.params.proxy = proxyReport;
      parent.postMessage(m, HOSTS[0]);
    }
  });
  // Probe only (not part of the design): a same-site request from the sandbox origin, to see whether the cookie goes with it.
  fetch(HOSTS[0] + "/api/mutate?from=proxy", { method: "POST", credentials: "include", mode: "no-cors" })
    .then(() => (proxyReport.post = "sent"), (e) => (proxyReport.post = "failed:" + e.name));
  parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} }, HOSTS[0]);
});
