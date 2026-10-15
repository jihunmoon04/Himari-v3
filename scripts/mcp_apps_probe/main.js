// The host page (main origin): creates the proxy, sends the view, answers ui/initialize.
const SANDBOX = "http://127.0.0.1:18766";
const VIEW_HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><p>view</p><script>
const r = { js: true, origin: self.origin };
document.addEventListener("securitypolicyviolation", (e) => { (r.violations ||= []).push(e.effectiveDirective + " " + e.blockedURI); });
try { void parent.parent.document.title; r.hostDom = "reachable"; } catch (e) { r.hostDom = "blocked:" + e.name; }
try { void parent.document.title; r.proxyDom = "reachable"; } catch (e) { r.proxyDom = "blocked:" + e.name; }
try { localStorage.setItem("k", "v"); r.storage = "available"; } catch (e) { r.storage = "blocked:" + e.name; }
try { r.cookie = JSON.stringify(document.cookie); } catch (e) { r.cookie = "blocked:" + e.name; }
try { r.popup = window.open("http://127.0.0.1:18766/blank?popup") === null ? "null" : "opened"; } catch (e) { r.popup = "blocked:" + e.name; }
try { top.location.href = "http://127.0.0.1:18766/blank?topnav"; r.topNav = "no-exception"; } catch (e) { r.topNav = "blocked:" + e.name; }
const f = document.createElement("iframe"); f.src = "http://127.0.0.1:18766/blank?nested"; document.body.appendChild(f);
fetch("http://127.0.0.1:18765/api/secret?from=view", { credentials: "include" }).then((x) => x.text()).then((t) => (r.fetchMain = "read:" + t), (e) => (r.fetchMain = "failed:" + e.name));
fetch("http://127.0.0.1:18765/api/mutate?from=view", { method: "POST", credentials: "include", mode: "no-cors" }).then(() => (r.postMain = "sent"), (e) => (r.postMain = "failed:" + e.name));
window.addEventListener("message", (ev) => {
  const m = ev.data;
  if (m && m.id === 1 && m.result) {
    r.initialize = m.result.protocolVersion;
    parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} }, "*");
  }
  if (m && m.method === "ui/notifications/tool-input") r.toolInput = m.params.arguments;
  if (m && m.method === "ui/notifications/tool-result") {
    r.toolResult = m.params.structuredContent;
    setTimeout(() => parent.postMessage({ jsonrpc: "2.0", method: "notifications/message", params: { data: r } }, "*"), 800);
  }
});
parent.postMessage({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: { protocolVersion: "2026-01-26", appCapabilities: {}, clientInfo: { name: "probe", version: "1" } } }, "*");
<\/script></body></html>`;

const out = { events: [] };
const finish = () => (document.getElementById("out").textContent = JSON.stringify(out));
const frame = document.createElement("iframe");
frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
frame.src = SANDBOX + "/sandbox";
document.body.appendChild(frame);
window.addEventListener("message", (ev) => {
  if (ev.source !== frame.contentWindow || ev.origin !== SANDBOX) {
    out.events.push("rejected:" + ev.origin);
    return;
  }
  const m = ev.data;
  out.events.push(m.method || "response:" + m.id);
  const send = (msg) => frame.contentWindow.postMessage(msg, SANDBOX);
  if (m.method === "ui/notifications/sandbox-proxy-ready") {
    send({ jsonrpc: "2.0", method: "ui/notifications/sandbox-resource-ready", params: { html: VIEW_HTML } });
  } else if (m.method === "ui/initialize") {
    send({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2026-01-26", hostCapabilities: {}, hostInfo: { name: "himari", version: "3" }, hostContext: {} } });
  } else if (m.method === "ui/notifications/initialized") {
    send({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { city: "Seoul" } } });
    send({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { content: [], structuredContent: { temp: 21 } } });
  } else if (m.method === "notifications/message") {
    out.view = m.params.data;
    out.proxy = m.params.proxy;
    finish();
  }
});
setTimeout(finish, 6000);
