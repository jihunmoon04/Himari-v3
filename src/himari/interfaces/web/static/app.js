/* HIMARI v3 Web UI. One module script, no framework, no build step.
 *
 * Rules carried over from the HIMARI-v2 UI:
 * 1. Hostile content. Every user/model/tool value is rendered with
 *    textContent on elements built by createElement. Assistant prose goes
 *    through renderAssistantText (rich_text.js), which never parses HTML.
 *    There is no innerHTML/insertAdjacentHTML/eval in this file.
 * 2. Stale responses. Every read for the open conversation captures the
 *    selection generation and re-checks it after each await.
 * 3. Mutations are never replayed. A message whose outcome is unknown
 *    (network error) refreshes the view and says so; it is not resent.
 *
 * Progress is read by polling the conversation (job + waiting question)
 * and its events after the last sequence shown.
 */

import { renderAssistantText } from "/assets/rich_text.js";

const POLL_MS = 1000;
const PAGE = 50;

const STATE_TEXT = {
  completed: "Done",
  waiting_for_user: "Waiting for your answer",
  recovery_required: "Needs attention: an earlier action's outcome is unknown",
  provider_failed: "The model provider failed; nothing was retried",
  outcome_unknown: "The request may not have reached the model; it was not resent",
  invalid_model_output: "The model's reply could not be used",
  request_rejected: "The request was refused before sending",
  run_limit_reached: "Stopped after the maximum number of steps",
  idle: "Nothing to continue",
  compacted: "Compacted: earlier messages now reach HIMARI as a summary",
  nothing_to_compact: "Nothing to compact yet",
  compaction_failed: "Compaction failed; nothing was changed",
};
const ERROR_TEXT = {
  busy: "This conversation is still working.",
  cancelled: "The run was stopped when the server shut down.",
  internal_error: "Something went wrong on the server.",
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

class ApiError extends Error {
  constructor(kind, status, code, message) {
    super(message);
    this.kind = kind; // "http" (definite answer) | "network" (outcome unknown) | "malformed"
    this.status = status;
    this.code = code;
  }
}

async function api(method, path, body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    throw new ApiError("network", 0, "network", "The server could not be reached.");
  }
  if (response.status === 204) return null;
  let data;
  try {
    data = await response.json();
  } catch (error) {
    throw new ApiError("malformed", response.status, "malformed", "The server's answer was not readable.");
  }
  if (!response.ok) {
    const error = data && data.error ? data.error : {};
    if (response.status === 401 && error.code === "unauthenticated") showLogin();
    throw new ApiError("http", response.status, error.code || "http_error", error.message || "Request failed.");
  }
  return data;
}

/* ------------------------------------------------------------------ state */

const app = {
  conversationId: null,
  generation: 0,
  lastSequence: 0,
  toolRows: new Map(),
  pollTimer: null,
  listOffset: 0,
  sending: false,
  context: null, // the last /context answer for this conversation
};

function status(text, tone) {
  const bar = $("status-bar");
  bar.textContent = text || "";
  bar.className = tone ? `status-bar is-${tone}` : "status-bar";
}

function showSurface(name) {
  for (const surface of ["login", "home", "chat"]) $(`surface-${surface}`).hidden = surface !== name;
  const signedIn = name !== "login";
  $("sidebar").hidden = !signedIn;
  $("sidebar-toggle").hidden = !signedIn;
  $("topbar-chat-context").hidden = name !== "chat";
  $("chat-menu-toggle").hidden = name !== "chat";
  if (name !== "chat") closeChatMenu();
}

/* ------------------------------------------------------------- sign-in */

function showLogin() {
  stopPolling();
  app.conversationId = null;
  app.generation += 1;
  showSurface("login");
  $("topbar-title").textContent = "HIMARI";
  $("login-token").value = "";
  $("login-token").focus();
}

async function signIn(event) {
  event.preventDefault();
  const token = $("login-token").value.trim();
  if (!token) return;
  $("login-submit").disabled = true;
  try {
    await api("POST", "/api/session", { token });
    $("login-token").value = "";
    status("");
    await enter();
  } catch (error) {
    status(error.code === "invalid_token" ? "That token is not valid." : error.message, "error");
  } finally {
    $("login-submit").disabled = false;
  }
}

async function signOut() {
  try {
    await api("DELETE", "/api/session");
  } finally {
    showLogin();
  }
}

/* ---------------------------------------------------------- conversations */

function conversationName(item) {
  return item.title || item.preview || "New conversation";
}

function renderListRow(item) {
  const li = el("li");
  const button = el("button", "conv-row", conversationName(item));
  button.type = "button";
  button.dataset.id = item.id;
  button.title = conversationName(item);
  if (item.id === app.conversationId) button.classList.add("is-selected");
  button.addEventListener("click", () => openConversation(item.id));
  li.append(button);
  return li;
}

async function loadList(reset) {
  if (reset) app.listOffset = 0;
  const page = await api("GET", `/api/conversations?limit=${PAGE}&offset=${app.listOffset}`);
  const list = $("conversation-list");
  if (reset) list.replaceChildren();
  for (const item of page.items) list.append(renderListRow(item));
  app.listOffset = page.next_offset ?? app.listOffset + page.items.length;
  $("conversation-load-more").hidden = page.next_offset === null;
  return page.items;
}

function markSelected() {
  for (const button of $("conversation-list").querySelectorAll(".conv-row")) {
    button.classList.toggle("is-selected", button.dataset.id === app.conversationId);
  }
}

async function showHome() {
  stopPolling();
  app.conversationId = null;
  app.generation += 1;
  markSelected();
  showSurface("home");
  $("topbar-title").textContent = "HIMARI";
  const items = await loadList(true);
  const recent = $("home-recent-list");
  recent.replaceChildren();
  for (const item of items.slice(0, 5)) {
    const li = el("li");
    const card = el("button", "home-recent-card");
    card.type = "button";
    card.append(el("span", "home-recent-title", conversationName(item)),
                el("span", "home-recent-activity", new Date(item.last_activity_at).toLocaleString()));
    card.addEventListener("click", () => openConversation(item.id));
    li.append(card);
    recent.append(li);
  }
  $("home-recent-empty").hidden = items.length > 0;
}

async function newConversation() {
  let created;
  try {
    created = await api("POST", "/api/conversations", {});
  } catch (error) {
    if (error.kind === "network") {
      // The server may have created it: show the list instead of retrying.
      status("Could not confirm the new conversation; check the list before creating another.", "warn");
      await loadList(true).then(markSelected).catch(() => {});
    } else {
      status(error.message, "error");
    }
    return;
  }
  await loadList(true);
  await openConversation(created.id);
  $("composer-text").focus();
}

/* --------------------------------------------------------------- timeline */

function speaker(kind) {
  return kind === "user_message" ? "You" : "HIMARI";
}

function renderMessage(event) {
  const user = event.kind === "user_message";
  const node = el("div", user ? "item item-user" : "item item-assistant");
  node.append(el("div", "item-kind", speaker(event.kind)));
  node.append(user ? el("p", "item-text", event.text) : renderAssistantText(document, event.text || ""));
  return node;
}

function paintToolRow(entry) {
  const tone = entry.result === null ? "pending" : entry.result.is_error ? "problem" : "success";
  const mark = tone === "pending" ? "…" : tone === "success" ? "✓" : "!";
  entry.node.className = `item item-tool-row is-${tone}`;
  const body = el("div", "tool-row-body");
  body.append(el("p", "tool-row-heading", "Arguments"), el("pre", "pre-code", JSON.stringify(entry.args, null, 2)));
  if (entry.result !== null) {
    body.append(el("p", "tool-row-heading", "Result"), el("pre", "pre-code", entry.result.text));
  }
  const state = tone === "pending" ? "running" : tone === "success" ? "done" : "failed";
  entry.details.replaceChildren(el("summary", "tool-row-summary", `${mark} ${entry.toolId} · ${state}`), body);
}

function renderEvent(event) {
  const timeline = tagging($("timeline"), event.sequence);
  if (event.kind === "user_message" || event.kind === "assistant_final") {
    timeline.append(renderMessage(event));
    return;
  }
  if (event.kind === "assistant_tool_calls") {
    if (event.text) timeline.append(renderMessage({ ...event, kind: "assistant_final" }));
    for (const call of event.calls) {
      const key = `${event.run_id}/${call.call_id}`; // call ids repeat across Runs
      if (call.tool_id === "ask_user") {
        const question = typeof call.arguments.question === "string" ? call.arguments.question : "";
        timeline.append(renderMessage({ kind: "assistant_final", text: question }));
        app.toolRows.set(key, null); // the answer appears as the user's message
        continue;
      }
      const node = el("div", "item item-tool-row is-pending");
      const details = el("details", "tool-row-details");
      node.append(details);
      const entry = { node, details, toolId: call.tool_id, args: call.arguments, result: null };
      app.toolRows.set(key, entry);
      paintToolRow(entry);
      timeline.append(node);
    }
    return;
  }
  if (event.kind === "tool_result") {
    const entry = app.toolRows.get(`${event.run_id}/${event.call_id}`);
    if (entry === null) return; // ask_user answer
    if (entry === undefined) {
      const orphan = { node: el("div"), details: el("details", "tool-row-details"), toolId: "tool",
                       args: {}, result: event };
      orphan.node.append(orphan.details);
      paintToolRow(orphan);
      timeline.append(orphan.node);
      return;
    }
    entry.result = event;
    paintToolRow(entry);
  }
}

/* Every node an event adds carries its sequence, so the compaction divider
   can be placed before the first event the summary does not cover. */
function tagging(timeline, sequence) {
  return {
    append(...nodes) {
      for (const node of nodes) node.dataset.sequence = String(sequence);
      timeline.append(...nodes);
    },
  };
}

function formatTokens(n) {
  if (n >= 10000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function placeDivider() {
  const timeline = $("timeline");
  const old = timeline.querySelector(".context-divider");
  if (old) old.remove();
  const context = app.context;
  if (!context || !context.enabled || context.covered_through_sequence === null || !context.summary) return;
  const divider = el("details", "context-divider");
  const label = context.compactions > 1
    ? `Earlier messages reach HIMARI as a summary (compacted ${context.compactions} times)`
    : "Earlier messages reach HIMARI as a summary";
  divider.append(el("summary", "context-divider-label", label), el("p", "context-divider-text", context.summary));
  const after = [...timeline.children].find((node) => Number(node.dataset.sequence) > context.covered_through_sequence);
  if (after) timeline.insertBefore(divider, after);
  else timeline.append(divider);
}

function applyContext(context) {
  app.context = context;
  const pair = $("chat-menu-context-pair");
  pair.hidden = !context.enabled;
  $("chat-menu-compact").hidden = !context.enabled;
  if (context.enabled) {
    const size = context.estimated_tokens === null ? "size unknown while working"
      : `about ${formatTokens(context.estimated_tokens)} of ${formatTokens(context.budget_tokens)} tokens`;
    let text = `${size} · summarizes at ${formatTokens(context.compact_at_tokens)}`;
    if (context.hard_limit_tokens !== context.budget_tokens) {
      text += ` · input limit ${formatTokens(context.hard_limit_tokens)}`; // the budget is a target, not the refusal point
    }
    $("chat-menu-context").textContent = text;
  }
  placeDivider();
}

async function loadContext(generation) {
  try {
    const context = await api("GET", `/api/conversations/${encodeURIComponent(app.conversationId)}/context`);
    if (generation === app.generation) applyContext(context);
  } catch (ignored) {
    /* display only: the conversation works without it */
  }
}

/* MCP servers: on/off for this conversation (V3-P53). Changes apply from the
 * next model request; the server checks again before every call. */
function toolSourceMeta(source) {
  const parts = [source.choice === null ? `default (${source.default ? "on" : "off"})` : "set here"];
  if (!source.available) parts.push("not connected");
  else parts.push(`${source.tools.length} tool${source.tools.length === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

function renderToolSources(sources) {
  const list = $("chat-menu-tools-list");
  list.replaceChildren();
  $("chat-menu-tools").hidden = sources.length === 0;
  for (const source of sources) {
    const row = el("li", "chat-menu-tool");
    const id = `tool-source-${source.source}`;
    const box = el("input");
    box.type = "checkbox";
    box.id = id;
    box.checked = source.enabled;
    box.addEventListener("change", () => setToolSource(source.source, box.checked));
    const label = el("label", "chat-menu-tool-name", source.source);
    label.htmlFor = id;
    const meta = el("span", "chat-menu-tool-meta", toolSourceMeta(source));
    if (source.problems.length) meta.title = source.problems.join("\n");
    label.append(meta);
    row.append(box, label);
    if (source.choice !== null) {
      const reset = el("button", "link-button", "Use default");
      reset.type = "button";
      reset.addEventListener("click", () => setToolSource(source.source, null));
      row.append(reset);
    }
    list.append(row);
  }
}

async function loadToolSources(generation) {
  try {
    const answer = await api("GET", `/api/conversations/${encodeURIComponent(app.conversationId)}/tools`);
    if (generation === app.generation) renderToolSources(answer.sources);
  } catch (ignored) {
    /* display only: the conversation works without it */
  }
}

async function setToolSource(source, enabled) {
  const generation = app.generation;
  try {
    const answer = await api("PUT",
      `/api/conversations/${encodeURIComponent(app.conversationId)}/tools/${encodeURIComponent(source)}`,
      { enabled });
    if (generation === app.generation) renderToolSources(answer.sources);
  } catch (error) {
    if (generation === app.generation) {
      status(error.message, "error");
      loadToolSources(generation);
    }
  }
}

async function pullEvents(generation) {
  for (;;) {
    const page = await api("GET",
      `/api/conversations/${encodeURIComponent(app.conversationId)}/events?after=${app.lastSequence}&limit=200`);
    if (generation !== app.generation) return false;
    const timeline = $("timeline");
    const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    for (const event of page.items) renderEvent(event);
    app.lastSequence = page.last_sequence;
    if (page.items.length && atBottom) timeline.scrollTop = timeline.scrollHeight;
    if (page.items.length < 200) return true;
  }
}

/* ------------------------------------------------------------ conversation */

function describe(detail) {
  const job = detail.job;
  if (job.running) return { text: "Working…", tone: null, active: true };
  if (detail.waiting) return { text: STATE_TEXT.waiting_for_user, tone: "warn", active: false };
  // Compacting (or finding nothing to compact) answers nothing: an unanswered
  // message still wins, with the compaction result in front of it.
  const compacted = job.state === "compacted" || job.state === "nothing_to_compact";
  if (detail.continuable && !job.error && (job.state === null || job.state === "completed" || compacted)) {
    const done = compacted ? `${STATE_TEXT[job.state]}. ` : "";
    return { text: `${done}Not answered yet — choose Continue in the conversation menu.`, short: "Not answered yet",
             tone: "warn", active: false };
  }
  if (job.error) return { text: ERROR_TEXT[job.error] || `The run failed (${job.error}).`, tone: "error", active: false };
  if (job.state === "compacted" || job.state === "nothing_to_compact") {
    return { text: STATE_TEXT[job.state], tone: "ok", active: false };
  }
  if (job.state && job.state !== "completed" && job.state !== "idle") {
    return { text: STATE_TEXT[job.state] || job.state, tone: "warn", active: false };
  }
  return { text: job.state ? STATE_TEXT[job.state] : "", tone: null, active: false };
}

function applyDetail(detail) {
  const name = conversationName(detail.conversation);
  $("topbar-title").textContent = name;
  $("chat-menu-title").textContent = name;
  const view = describe(detail);
  $("topbar-chat-label").textContent = view.short || view.text;
  $("chat-menu-state").textContent = view.short || view.text || "Ready";
  $("topbar-state-dot").classList.toggle("is-active", view.active);
  status(view.tone ? view.text : "", view.tone);
  $("question-panel").hidden = !detail.waiting;
  $("question-text").textContent = detail.waiting ? detail.waiting.question : "";
  $("composer-text").placeholder = detail.waiting ? "Your answer" : "Message HIMARI";
  $("composer-send").disabled = detail.job.running || app.sending;
  $("composer-hint").textContent = detail.job.running ? "HIMARI is working…" : "";
  $("chat-menu-resume").hidden = !detail.continuable;
  $("chat-menu-compact").disabled = detail.job.running;
}

async function refresh(generation) {
  const detail = await api("GET", `/api/conversations/${encodeURIComponent(app.conversationId)}`);
  if (generation !== app.generation) return null;
  if (!(await pullEvents(generation))) return null;
  applyDetail(detail);
  return detail;
}

function stopPolling() {
  if (app.pollTimer !== null) clearTimeout(app.pollTimer);
  app.pollTimer = null;
}

function poll(generation) {
  stopPolling();
  app.pollTimer = setTimeout(async () => {
    if (generation !== app.generation) return;
    try {
      const detail = await refresh(generation);
      if (detail && detail.job.running) poll(generation);
      else if (detail) {
        loadList(true).then(markSelected).catch(() => {});
        loadContext(generation);
      }
    } catch (error) {
      if (generation === app.generation && error.status !== 401) {
        status("Lost contact with the server; retrying…", "warn");
        poll(generation);
      }
    }
  }, POLL_MS);
}

async function openConversation(id) {
  stopPolling();
  app.generation += 1;
  const generation = app.generation;
  app.conversationId = id;
  app.lastSequence = 0;
  app.toolRows = new Map();
  app.context = null;
  $("chat-menu-context-pair").hidden = true;
  $("chat-menu-compact").hidden = true;
  $("chat-menu-tools").hidden = true;
  $("timeline").replaceChildren();
  markSelected();
  showSurface("chat");
  closeNav();
  loadToolSources(generation);
  try {
    const detail = await refresh(generation);
    if (detail && detail.job.running) poll(generation);
    else if (detail) loadContext(generation);
  } catch (error) {
    if (generation === app.generation) status(error.message, "error");
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const box = $("composer-text");
  const text = box.value.trim();
  if (!text || app.sending || app.conversationId === null) return;
  const generation = app.generation;
  app.sending = true;
  $("composer-send").disabled = true;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/messages`, { text });
    box.value = "";
    autosize();
    status("");
  } catch (error) {
    if (error.kind === "network") {
      status("The message may or may not have been received; check the conversation before sending again.", "warn");
    } else {
      status(error.message, "error");
    }
  } finally {
    app.sending = false;
  }
  if (generation === app.generation) {
    try {
      await refresh(generation);
    } catch (ignored) {
      /* the poll below reports trouble */
    }
    poll(generation);
  }
}

async function resumeConversation() {
  closeChatMenu();
  if (app.conversationId === null) return;
  const generation = app.generation;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/resume`);
  } catch (error) {
    status(error.message, "error");
  }
  if (generation === app.generation) poll(generation);
}

async function compactConversation() {
  closeChatMenu();
  if (app.conversationId === null) return;
  const generation = app.generation;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/compact`);
    status("Compacting…");
  } catch (error) {
    status(error.message, "error");
  }
  if (generation === app.generation) poll(generation); // the poll's end reloads the context and divider
}

async function renameConversation() {
  closeChatMenu();
  if (app.conversationId === null) return;
  const title = window.prompt("Conversation name", $("topbar-title").textContent);
  if (title === null) return;
  const generation = app.generation;
  try {
    await api("PATCH", `/api/conversations/${encodeURIComponent(app.conversationId)}`, { title });
    if (generation === app.generation) await refresh(generation);
    await loadList(true);
    markSelected();
  } catch (error) {
    status(error.message, "error");
  }
}

/* --------------------------------------------------------------- chrome */

function closeChatMenu() {
  $("chat-menu").hidden = true;
  $("chat-menu-toggle").setAttribute("aria-expanded", "false");
}

function toggleChatMenu() {
  const open = $("chat-menu").hidden;
  $("chat-menu").hidden = !open;
  $("chat-menu-toggle").setAttribute("aria-expanded", String(open));
}

function closeNav() {
  $("layout").classList.remove("is-nav-open");
  $("nav-backdrop").hidden = true;
  $("sidebar-toggle").setAttribute("aria-expanded", "false");
}

function toggleNav() {
  const layout = $("layout");
  if (window.matchMedia("(min-width: 1100px)").matches) {
    layout.classList.toggle("is-nav-collapsed");
    return;
  }
  const open = !layout.classList.contains("is-nav-open");
  layout.classList.toggle("is-nav-open", open);
  $("nav-backdrop").hidden = !open;
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
}

function autosize() {
  const box = $("composer-text");
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 240)}px`;
}

async function enter() {
  await showHome();
}

async function boot() {
  $("login-form").addEventListener("submit", signIn);
  $("nav-sign-out").addEventListener("click", signOut);
  $("nav-home").addEventListener("click", () => showHome().catch((e) => status(e.message, "error")));
  $("nav-new-chat").addEventListener("click", newConversation);
  $("home-new-chat").addEventListener("click", newConversation);
  $("conversation-load-more").addEventListener("click", () => loadList(false).then(markSelected));
  $("composer").addEventListener("submit", sendMessage);
  $("composer-text").addEventListener("input", autosize);
  $("composer-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("chat-menu-toggle").addEventListener("click", toggleChatMenu);
  $("chat-menu-rename").addEventListener("click", renameConversation);
  $("chat-menu-resume").addEventListener("click", resumeConversation);
  $("chat-menu-compact").addEventListener("click", compactConversation);
  $("sidebar-toggle").addEventListener("click", toggleNav);
  $("sidebar-close").addEventListener("click", closeNav);
  $("nav-backdrop").addEventListener("click", closeNav);
  try {
    const session = await api("GET", "/api/session");
    if (session.authenticated) await enter();
    else showLogin();
  } catch (error) {
    showLogin();
    status(error.message, "error");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  boot();
});
