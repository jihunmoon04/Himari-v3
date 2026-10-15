// Drives the real app_view.js with an api() whose answers the test releases
// one by one, so the orderings of PR #28 review B1/B2 happen exactly.
import { AppViews } from "/assets/app_view.js";

const pending = []; // {path, resolve, reject}
function api(method, path) {
  return new Promise((resolve, reject) => pending.push({ path, resolve, reject }));
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
const views = new AppViews(api, el, () => "conv_1");

function view(state, extra = {}) {
  return { state, source: "calliope", tool_id: "calliope/echo", tool: null, arguments: { text: "hi" },
           status: state === "done" ? "succeeded" : "pending", digest: "d1", html: "<p>view</p>",
           prefers_border: null, csp: { connect_domains: [], resource_domains: [] },
           sandbox_url: `${location.origin}/fakeview.html`, result: state === "done" ? { structuredContent: { n: 1 } } : null,
           ...extra };
}

function row(key) {
  const node = el("div", "item item-tool-row");
  document.getElementById("timeline").append(node);
  return { node, details: el("details"), toolId: "calliope/echo", args: { text: "hi" }, result: null, key };
}

window.harness = {
  views, pending, view,
  row(key, running = true) {
    const entry = row(key);
    views.attach(entry, key, "run_1", key, running);
    window.harness.entries[key] = entry;
    return key;
  },
  entries: {},
  answer(state, extra) { pending.shift().resolve(view(state, extra)); },
  refuse(code) {
    const call = pending.shift();
    const error = new Error(code);
    error.code = code;
    call.reject(error);
  },
  resultEvent(key, stored = true) {
    const entry = window.harness.entries[key];
    entry.result = { is_error: false, text: "ok" };
    views.result(entry, stored);
  },
  frames: () => document.querySelectorAll(".app-view-frame").length,
  received() {
    const frame = document.querySelector(".app-view-frame");
    return frame && frame.contentWindow.received ? [...frame.contentWindow.received] : null;
  },
  notices: () => [...document.querySelectorAll(".app-view-notice")].map((n) => n.textContent),
  broadcastOff(source) { new BroadcastChannel("himari-app-views").postMessage({ conversation: "conv_1", source }); },
};
