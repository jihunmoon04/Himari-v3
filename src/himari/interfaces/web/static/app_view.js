/* Tool views (MCP Apps 2026-01-26, V3-P58): the host side of the bridge.
 *
 * A view is HTML from a Tool's MCP server. It runs in the sandbox origin's
 * proxy (a second port), in an opaque iframe with the CSP HIMARI signed;
 * this page only exchanges JSON-RPC messages with it.
 *
 * - Shown: the most recent call with a view opens by itself, the others with
 *   "Open view"; at most MAX_LIVE run at once. A view opens while its call
 *   runs (tool-input first) and gets tool-result when the result is stored.
 * - A call that did not run gets tool-cancelled; one that ran or may have
 *   run is never called cancelled: the view is closed with a note instead.
 * - Views of a source turned off in this conversation are closed (here, in
 *   other tabs of this browser, and when the server refuses the view).
 * - Everything the view sends is checked: it must come from that view's
 *   frame and the sandbox origin, be a JSON-RPC message, and be small. Only
 *   ui/initialize, ui/open-link and ping are answered; every other request
 *   is refused (-32601). The view cannot call Tools or send messages here.
 * - Same rules as app.js: textContent only, generation checks after awaits.
 */

const PROTOCOL = "2026-01-26";
const MAX_LIVE = 3;
const MAX_MESSAGE_CHARS = 1024 * 1024;
const MIN_HEIGHT = 40;
const MAX_HEIGHT = 800;
const TEARDOWN_MS = 1000;
const CHANNEL = "himari-app-views";

const NOTICE = {
  view_unavailable: "This result can't be shown in the view.",
  unknown: "The outcome of this call is unknown.",
  source_disabled: "This server is turned off in this conversation.",
  app_view_disabled: "Views from this server are turned off.",
  failed: "The view could not be loaded.",
};

function isMessage(m) {
  return m !== null && typeof m === "object" && !Array.isArray(m) && m.jsonrpc === "2.0";
}

function small(m) {
  try {
    return JSON.stringify(m).length <= MAX_MESSAGE_CHARS;
  } catch (error) {
    return false;
  }
}

export class AppViews {
  /* api: app.js's api(); el: its element helper; conversation(): the open conversation id. */
  constructor(api, el, conversation) {
    this.api = api;
    this.el = el;
    this.conversation = conversation;
    this.generation = 0;
    this.entries = []; // tool rows that may have a view, in timeline order
    this.live = new Map(); // key → live view
    this.closedByUser = new Set();
    window.addEventListener("message", (event) => this.receive(event));
    if (typeof BroadcastChannel === "function") {
      this.channel = new BroadcastChannel(CHANNEL);
      this.channel.addEventListener("message", (event) => {
        const m = event.data;
        if (m && m.conversation === this.conversation() && typeof m.source === "string") {
          this.closeSource(m.source);
        }
      });
    }
  }

  /* A new conversation: tear every view down. */
  reset() {
    this.generation += 1;
    for (const view of [...this.live.values()]) this.teardown(view, "The conversation was closed.");
    this.entries = [];
    this.closedByUser = new Set();
  }

  /* A tool row whose call may show a view (the event's app_view flag). */
  attach(entry, key, runId, callId, runningView) {
    entry.view = { key, runId, callId, runningView, storedView: false,
                   source: entry.toolId.split("/")[0], slot: this.el("div", "app-view-slot") };
    entry.node.append(entry.view.slot);
    this.entries.push(entry);
    this.paintControl(entry);
  }

  /* Its result arrived (storedView: the result was stored with a view). */
  result(entry, storedView) {
    if (!entry.view) return;
    entry.view.storedView = storedView;
    const live = this.live.get(entry.view.key);
    if (live) this.finish(live);
    else this.paintControl(entry);
  }

  viewable(entry) {
    return entry.result === null ? entry.view.runningView : entry.view.storedView;
  }

  /* After a page of events: open the most recent call with a view. */
  autoOpen() {
    const latest = [...this.entries].reverse().find((entry) => this.viewable(entry));
    if (latest && !this.live.has(latest.view.key) && !this.closedByUser.has(latest.view.key)) this.open(latest);
  }

  paintControl(entry) {
    const view = entry.view;
    if (this.live.has(view.key)) return;
    view.slot.replaceChildren();
    if (!this.viewable(entry)) return;
    const button = this.el("button", "link-button app-view-open", "Open view");
    button.type = "button";
    button.addEventListener("click", () => {
      this.closedByUser.delete(view.key);
      this.open(entry);
    });
    view.slot.append(button);
  }

  notice(entry, text, detail) {
    entry.view.slot.replaceChildren(this.el("p", "app-view-notice", text));
    if (detail) entry.view.slot.append(this.el("p", "app-view-notice-detail", detail));
  }

  url(entry) {
    const v = entry.view;
    return `/api/conversations/${encodeURIComponent(this.conversation())}/calls/${encodeURIComponent(v.runId)}/`
      + `${encodeURIComponent(v.callId)}/app?origin=${encodeURIComponent(window.location.origin)}`;
  }

  async open(entry) {
    const generation = this.generation;
    let data;
    try {
      data = await this.api("GET", this.url(entry));
    } catch (error) {
      if (generation === this.generation) this.notice(entry, NOTICE[error.code] || NOTICE.failed);
      return;
    }
    if (generation !== this.generation || this.live.has(entry.view.key)) return;
    if (data.state === "ended") {
      this.notice(entry, NOTICE[data.outcome] || data.reason || NOTICE.failed);
      return;
    }
    while (this.live.size >= MAX_LIVE) {
      const oldest = this.live.values().next().value;
      this.teardown(oldest, "Too many views are open.").then(() => this.paintControl(oldest.entry));
    }
    this.mount(entry, data);
  }

  mount(entry, data) {
    const frame = document.createElement("iframe");
    // The proxy (sandbox origin) needs both, per the spec; the view inside it gets allow-scripts only.
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("title", `View from ${data.source}`);
    frame.className = "app-view-frame";
    frame.style.height = `${MIN_HEIGHT * 4}px`;
    frame.src = data.sandbox_url;
    const close = this.el("button", "link-button app-view-close", "Close view");
    close.type = "button";
    const header = this.el("div", "app-view-header");
    header.append(this.el("span", "app-view-label", `App view from ${data.source} · sandboxed`), close);
    const box = this.el("div", data.prefers_border === false ? "app-view is-borderless" : "app-view");
    box.append(header, frame);
    entry.view.slot.replaceChildren(box);
    const view = { entry, data, frame, box, origin: new URL(data.sandbox_url).origin, initialized: false,
                   pending: null, nextId: 1, waiting: new Map() };
    this.live.set(entry.view.key, view);
    close.addEventListener("click", () => {
      this.closedByUser.add(entry.view.key);
      this.teardown(view, "The user closed the view.").then(() => this.paintControl(entry));
    });
    // The call may have ended while the view was being fetched: settle it now.
    if (data.state === "running" && entry.result !== null) this.finish(view);
  }

  /* The call ended while its view was showing: read what it ended with. */
  async finish(view) {
    const generation = this.generation;
    let data;
    try {
      data = await this.api("GET", this.url(view.entry));
    } catch (error) {
      if (generation === this.generation && this.live.get(view.entry.view.key) === view) {
        const code = NOTICE[error.code] ? error.code : "failed";
        await this.teardown(view, code);
        this.notice(view.entry, NOTICE[code]);
      }
      return;
    }
    if (generation !== this.generation || this.live.get(view.entry.view.key) !== view) return;
    if (data.state === "done" && data.digest !== view.data.digest) {
      await this.teardown(view, "The view changed."); // restarted with new HTML meanwhile: show the stored one
      this.mount(view.entry, data);
      return;
    }
    if (data.state === "done") this.deliver(view, { method: "ui/notifications/tool-result", params: data.result });
    else if (data.state === "ended" && data.outcome === "not_run") {
      this.deliver(view, { method: "ui/notifications/tool-cancelled", params: { reason: data.reason || "Not run." } });
    } else if (data.state === "ended") {
      await this.teardown(view, data.outcome);
      this.notice(view.entry, NOTICE[data.outcome] || NOTICE.failed, data.reason);
    }
  }

  /* Sends after the view's initialized notification (spec), else when it comes. */
  deliver(view, message) {
    if (view.initialized) this.post(view, { jsonrpc: "2.0", ...message });
    else view.pending = message;
  }

  post(view, message) {
    if (view.frame.contentWindow) view.frame.contentWindow.postMessage(message, view.origin);
  }

  async teardown(view, reason) {
    if (this.live.get(view.entry.view.key) !== view) return;
    this.live.delete(view.entry.view.key);
    if (view.initialized) {
      const id = view.nextId++;
      const answered = new Promise((resolve) => view.waiting.set(id, resolve));
      this.post(view, { jsonrpc: "2.0", id, method: "ui/resource-teardown", params: { reason } });
      await Promise.race([answered, new Promise((resolve) => setTimeout(resolve, TEARDOWN_MS))]);
    }
    view.frame.remove();
    if (view.entry.view.slot.contains(view.box)) view.entry.view.slot.replaceChildren();
  }

  /* Turned off here or in another tab of this browser. */
  closeSource(source) {
    for (const view of [...this.live.values()]) {
      if (view.entry.view.source !== source) continue;
      this.teardown(view, NOTICE.source_disabled).then(() => this.notice(view.entry, NOTICE.source_disabled));
    }
  }

  sourceTurnedOff(source) {
    this.closeSource(source);
    if (this.channel) this.channel.postMessage({ conversation: this.conversation(), source });
  }

  /* The tool sources as just read (conversation open, job end). */
  sources(list) {
    for (const source of list) if (!source.enabled) this.closeSource(source.source);
  }

  receive(event) {
    const view = [...this.live.values()].find((v) => v.frame.contentWindow === event.source);
    if (!view || event.origin !== view.origin) return;
    const m = event.data;
    if (!isMessage(m) || !small(m)) return;
    const request = typeof m.method === "string" && m.id !== undefined && m.id !== null;
    if (m.method === "ui/notifications/sandbox-proxy-ready") {
      this.post(view, { jsonrpc: "2.0", method: "ui/notifications/sandbox-resource-ready",
                        params: { html: view.data.html } });
    } else if (m.method === "ui/initialize" && request) {
      this.answer(view, m.id, this.initializeResult(view));
    } else if (m.method === "ui/notifications/initialized" && !view.initialized) {
      view.initialized = true;
      this.post(view, { jsonrpc: "2.0", method: "ui/notifications/tool-input",
                        params: { arguments: view.data.arguments } });
      if (view.data.state === "done") {
        this.post(view, { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: view.data.result });
      } else if (view.pending) {
        this.post(view, { jsonrpc: "2.0", ...view.pending });
      }
      view.pending = null;
    } else if (m.method === "ui/notifications/size-changed") {
      const height = m.params && Number(m.params.height);
      if (Number.isFinite(height)) {
        view.frame.style.height = `${Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(height)))}px`;
      }
    } else if (m.method === "ui/open-link" && request) {
      this.openLink(view, m);
    } else if (m.method === "ping" && request) {
      this.answer(view, m.id, {});
    } else if (request) {
      this.refuse(view, m.id, -32601, "Not supported by this host.");
    } else if (m.method === undefined && view.waiting.has(m.id)) {
      view.waiting.get(m.id)();
      view.waiting.delete(m.id);
    }
  }

  initializeResult(view) {
    const data = view.data;
    const context = {
      theme: "light",
      displayMode: "inline",
      availableDisplayModes: ["inline"],
      containerDimensions: { width: view.frame.clientWidth || view.entry.node.clientWidth, maxHeight: MAX_HEIGHT },
      locale: navigator.language,
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      platform: "web",
    };
    if (data.tool) context.toolInfo = { tool: data.tool }; // the spec's Tool needs inputSchema: whole or nothing
    return {
      protocolVersion: PROTOCOL,
      hostInfo: { name: "himari", version: "3" },
      hostCapabilities: {
        openLinks: {},
        sandbox: { csp: { connectDomains: data.csp.connect_domains, resourceDomains: data.csp.resource_domains } },
      },
      hostContext: context,
    };
  }

  openLink(view, m) {
    let url = null;
    try {
      url = new URL(String(m.params && m.params.url));
    } catch (error) {
      url = null;
    }
    if (!url || (url.protocol !== "https:" && url.protocol !== "http:")) {
      this.refuse(view, m.id, -32000, "Invalid URL");
      return;
    }
    if (!window.confirm(`A view from ${view.data.source} wants to open:\n\n${url.href}`)) {
      this.refuse(view, m.id, -32000, "Link opening denied by user");
      return;
    }
    window.open(url.href, "_blank", "noopener,noreferrer");
    this.answer(view, m.id, {});
  }

  answer(view, id, result) {
    this.post(view, { jsonrpc: "2.0", id, result });
  }

  refuse(view, id, code, message) {
    this.post(view, { jsonrpc: "2.0", id, error: { code, message } });
  }
}
