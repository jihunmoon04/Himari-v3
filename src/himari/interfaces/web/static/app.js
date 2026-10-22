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
 * The trash (V3-P65 §9): "Delete conversation" moves it there after a
 * confirmation; the Trash surface lists what is there, to restore or to
 * delete for good. A trashed conversation is a missing one (404) elsewhere.
 *
 * Progress is read by polling the conversation (job + waiting question)
 * and its events after the last sequence shown. Tool views (MCP Apps) are
 * app_view.js's: this file only tells it about rows, results and sources.
 */

import { AppViews } from "/assets/app_view.js";
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
  budget_reached: "Stopped: this turn's token budget is used",
  no_progress: "Stopped: the last steps brought nothing new",
  idle: "Nothing to continue",
  compacted: "Compacted: earlier messages now reach HIMARI as a summary",
  nothing_to_compact: "Nothing to compact yet",
  compaction_failed: "Compaction failed; nothing was changed",
  paused: "Paused — Continue to go on, or send a new message",
  cancelled: "Stopped",
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
    const failure = new ApiError("http", response.status, error.code || "http_error", error.message || "Request failed.");
    failure.fileId = typeof error.file_id === "string" ? error.file_id : null; // attachment_refused (V3-P66 §9)
    throw failure;
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
  prompts: null, // the prompts of the servers on in this conversation (V3-P60 §4), once read
  promptChoices: [], // what the picker shows now
  promptSelected: 0,
  promptDismissed: null, // the composer text the picker was closed for (Escape)
  promptOpen: null, // the prompt whose arguments are being filled in
  promptTicket: 0, // the latest expansion asked for; anything that abandons it moves this on (PR #43 review B1)
  models: [], // the models this server offers (V3-P64), from /api/server
  modelChoice: null, // this conversation's /model answer
  running: false, // the last detail's job state: a model change then applies from the next turn
  changedWhileRunning: false, // the model was changed during the turn under way (PR #53 review C1)
  lastModel: null, // the model of the latest answer shown, for the label on a change
  trashOffset: 0,
  trashDays: null, // from /api/server
  attachments: [], // files for the next message (V3-P66 §9): {key, name, size, state, fileId, progress, xhr, note}
  jobRunning: false, // for the Send button only: set as soon as a send is taken (PR #60 review C5)
  attachKeys: 0,
};

const views = new AppViews(api, el, () => app.conversationId);

function status(text, tone) {
  const bar = $("status-bar");
  bar.textContent = text || "";
  bar.className = tone ? `status-bar is-${tone}` : "status-bar";
}

function showSurface(name) {
  for (const surface of ["login", "home", "chat", "trash"]) $(`surface-${surface}`).hidden = surface !== name;
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
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  clearAttachments();
  showSurface("login");
  $("topbar-title").textContent = "HIMARI";
  $("server-info").hidden = true;
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
  clearAttachments();
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

/* V3-P64 §7: the model that took a step, to be shown when it changed since
 * the step before; null when unchanged. Call it once per step, where the
 * step is shown: the label must not be lost (PR #53 review B1). */
function changedModel(model) {
  if (!model) return null;
  const changed = app.lastModel !== null && model !== app.lastModel ? model : null;
  app.lastModel = model;
  return changed;
}

function assistantHeader(model) {
  const kind = el("div", "item-kind", speaker("assistant_final"));
  if (model) kind.append(el("span", "item-model", model));
  return kind;
}

function renderMessage(event, model = null) {
  const user = event.kind === "user_message";
  const node = el("div", user ? "item item-user" : "item item-assistant");
  node.append(user ? el("div", "item-kind", speaker(event.kind)) : assistantHeader(model));
  if (!user) node.append(renderAssistantText(document, event.text || ""));
  else if (event.text) node.append(el("p", "item-text", event.text));
  if (user && event.attachments && event.attachments.length) node.append(fileChips(event.attachments));
  return node;
}

/* ------------------------------------------------------------------ files */

function humanSize(n) {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KB`;
  return `${n} bytes`;
}

/* A file of this conversation: a download link, or "expired" (V3-P66 §8.5).
 * The server sends it as an attachment of type octet-stream: it never opens here. */
function fileLink(file, className) {
  const label = `${file.name || "file"}${file.version > 1 ? ` (v${file.version})` : ""}`;
  if (file.expired || !file.name) {
    const gone = el("span", `${className} is-expired`, `${label} · expired`);
    gone.title = "This file expired: it was not used for a long time.";
    return gone;
  }
  const link = el("a", className, `${label} · ${humanSize(file.size)}`);
  link.href = `/api/conversations/${encodeURIComponent(app.conversationId)}/files/` +
    `${encodeURIComponent(file.file_id)}/${encodeURIComponent(file.version)}`;
  link.setAttribute("download", file.name);
  return link;
}

function fileChips(files) {
  const list = el("ul", "file-chips");
  for (const file of files) {
    const item = el("li");
    item.append(fileLink(file, "file-chip"));
    list.append(item);
  }
  return list;
}

function presentedCard(file) {
  const card = el("div", "item item-file");
  card.append(el("span", "item-file-note", "HIMARI gave you a file"), fileLink(file, "file-card"));
  return card;
}

function uploading() {
  return app.attachments.some((a) => a.state === "uploading");
}

function clearAttachments() {
  for (const entry of app.attachments) if (entry.xhr) entry.xhr.abort();
  app.attachments = [];
  renderAttachments();
}

function renderAttachments() {
  const list = $("attach-list");
  list.replaceChildren();
  for (const entry of app.attachments) {
    const item = el("li", `attach-chip is-${entry.state}`);
    const text = entry.state === "uploading" ? `${entry.name} · ${entry.progress}%`
      : entry.state === "ready" ? `${entry.name} · ${humanSize(entry.size)}` : `${entry.name} · ${entry.note}`;
    item.append(el("span", "attach-chip-text", text));
    const remove = el("button", "attach-chip-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${entry.name}`);
    remove.addEventListener("click", () => {
      if (entry.xhr) entry.xhr.abort();
      app.attachments = app.attachments.filter((a) => a !== entry);
      renderAttachments();
    });
    item.append(remove);
    list.append(item);
  }
  list.hidden = app.attachments.length === 0;
  $("composer-send").disabled = app.jobRunning || app.sending || uploading();
}

/* One request per file, with progress. Uploaded files are staged on the
 * server; the next message names them. A result that comes back after the
 * user left the conversation attaches nothing (the stale-answer rule). */
function uploadFile(file) {
  const conversationId = app.conversationId;
  if (conversationId === null) return;
  const generation = app.generation;
  const entry = { key: (app.attachKeys += 1), name: file.name, size: file.size, state: "uploading", fileId: null,
                  progress: 0, xhr: new XMLHttpRequest(), note: "" };
  app.attachments.push(entry);
  renderAttachments();
  const xhr = entry.xhr;
  xhr.open("POST", `/api/conversations/${encodeURIComponent(conversationId)}/uploads?name=` +
    encodeURIComponent(file.name));
  xhr.setRequestHeader("content-type", file.type || "application/octet-stream");
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable || generation !== app.generation) return;
    entry.progress = Math.min(99, Math.floor((event.loaded / event.total) * 100));
    renderAttachments();
  });
  xhr.addEventListener("load", () => {
    entry.xhr = null;
    if (generation !== app.generation || !app.attachments.includes(entry)) return;
    let data = null;
    try {
      data = JSON.parse(xhr.responseText);
    } catch (ignored) {
      /* reported below */
    }
    if (xhr.status === 201 && data) {
      Object.assign(entry, { state: "ready", fileId: data.file_id, size: data.size });
      if (data.commands === false) {
        status("Commands cannot run on this server: HIMARI can read text files, but not PDFs or images.", "warn");
      }
    } else {
      const error = data && data.error ? data.error : {};
      if (xhr.status === 401 && error.code === "unauthenticated") showLogin();
      Object.assign(entry, { state: "failed", note: error.message || "Upload failed" });
    }
    renderAttachments();
  });
  xhr.addEventListener("error", () => {
    entry.xhr = null;
    if (generation !== app.generation || !app.attachments.includes(entry)) return;
    Object.assign(entry, { state: "failed", note: "The upload did not finish" });
    renderAttachments();
  });
  xhr.send(file);
}

function addFiles(files) {
  for (const file of files) uploadFile(file);
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
  if (event.kind === "user_message") {
    timeline.append(renderMessage(event));
    return;
  }
  if (event.kind === "assistant_final") {
    timeline.append(renderMessage(event, changedModel(event.model)));
    return;
  }
  if (event.kind === "assistant_tool_calls") {
    let model = changedModel(event.model); // shown on the first thing this step shows
    if (event.text) {
      timeline.append(renderMessage({ ...event, kind: "assistant_final" }, model));
      model = null;
    }
    for (const call of event.calls) {
      const key = `${event.run_id}/${call.call_id}`; // call ids repeat across Runs
      if (call.tool_id === "ask_user") {
        const question = typeof call.arguments.question === "string" ? call.arguments.question : "";
        timeline.append(renderMessage({ kind: "assistant_final", text: question }, model));
        model = null;
        app.toolRows.set(key, null); // the answer appears as the user's message
        continue;
      }
      if (model) { // a step of calls only: its header names the model above them
        const header = el("div", "item item-assistant item-step");
        header.append(assistantHeader(model));
        timeline.append(header);
        model = null;
      }
      const node = el("div", "item item-tool-row is-pending");
      const details = el("details", "tool-row-details");
      node.append(details);
      const entry = { node, details, toolId: call.tool_id, args: call.arguments, result: null };
      app.toolRows.set(key, entry);
      paintToolRow(entry);
      if (call.app_view) views.attach(entry, key, event.run_id, call.call_id, true); // shown while it runs
      timeline.append(node);
    }
    return;
  }
  if (event.kind === "tool_result") {
    const entry = app.toolRows.get(`${event.run_id}/${event.call_id}`);
    if (entry === null) {
      // An ask_user answer is shown as the user's message; a question closed
      // without one (dismissed, or its turn cancelled) gets a note.
      if (event.is_error) timeline.append(el("div", "item item-note", "Question closed without an answer"));
      return;
    }
    if (entry === undefined) {
      const orphan = { node: el("div"), details: el("details", "tool-row-details"), toolId: "tool",
                       args: {}, result: event };
      orphan.node.append(orphan.details);
      paintToolRow(orphan);
      timeline.append(orphan.node);
      for (const file of event.presented_files || []) timeline.append(presentedCard(file)); // PR #60 review C4
      return;
    }
    if (event.app_view && !entry.view) {
      views.attach(entry, `${event.run_id}/${event.call_id}`, event.run_id, event.call_id, false);
    }
    for (const file of event.presented_files || []) timeline.append(presentedCard(file)); // V3-P66 §9
    entry.result = event;
    paintToolRow(entry);
    if (entry.view) views.result(entry, Boolean(event.app_view));
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
  if (n >= 1000000) return `${Number((n / 1000000).toFixed(1))}M`;
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
    label.append(meta);
    for (const problem of source.problems) label.append(el("span", "chat-menu-tool-problem", problem));
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
    if (generation === app.generation) {
      renderToolSources(answer.sources);
      views.sources(answer.sources); // a source off here closes its views
    }
  } catch (ignored) {
    /* display only: the conversation works without it */
  }
}

/* The model (V3-P64): this conversation's choice, kept as made; "Default"
 * follows the server's default model. Saved at once, used from the next turn. */
function option(value, text) {
  const node = el("option", null, text);
  node.value = value;
  return node;
}

function renderModel() {
  const choice = app.modelChoice;
  $("chat-menu-model").hidden = choice === null || app.models.length === 0;
  if (choice === null) return;
  const select = $("chat-menu-model-select");
  const fallback = app.models.find((m) => m.default);
  select.replaceChildren(option("", `Default${fallback ? ` (${fallback.id})` : ""}`));
  for (const model of app.models) select.append(option(model.id, model.id));
  const chosen = choice.chosen ? choice.chosen.model : "";
  if (chosen && !app.models.some((m) => m.id === chosen)) select.append(option(chosen, `${chosen} (not offered)`));
  select.value = chosen;
  const offered = app.models.find((m) => m.id === chosen);
  const effort = $("chat-menu-effort-select");
  const efforts = offered ? offered.efforts : [];
  $("chat-menu-effort-field").hidden = efforts.length === 0;
  effort.replaceChildren(option("", `Default (${offered && offered.default_effort ? offered.default_effort : "none"})`));
  for (const name of efforts) effort.append(option(name, name));
  const chosenEffort = choice.chosen && choice.chosen.effort ? choice.chosen.effort : "";
  if (chosenEffort && !efforts.includes(chosenEffort)) effort.append(option(chosenEffort, `${chosenEffort} (not offered)`));
  effort.value = chosenEffort;
  const note = $("chat-menu-model-note");
  const text = choice.fallback || (app.running && app.changedWhileRunning ? "Applies from the next message." : "");
  note.textContent = text;
  note.classList.toggle("is-warn", Boolean(choice.fallback));
  note.hidden = !text;
}

async function loadModel(generation) {
  try {
    const choice = await api("GET", `/api/conversations/${encodeURIComponent(app.conversationId)}/model`);
    if (generation === app.generation) {
      app.modelChoice = choice;
      renderModel();
    }
  } catch (ignored) {
    /* display only: the conversation works without it */
  }
}

async function setModel(model, effort) {
  const generation = app.generation;
  try {
    const choice = await api("PUT", `/api/conversations/${encodeURIComponent(app.conversationId)}/model`,
      { model, effort });
    if (generation === app.generation) {
      app.modelChoice = choice;
      app.changedWhileRunning = app.running;
      renderModel();
      loadContext(generation); // the budget is the new model's
    }
  } catch (error) {
    if (generation === app.generation) {
      status(error.message, "error");
      renderModel(); // back to what is stored
    }
  }
}

/* ------------------------------------------------------- server prompts */
/* V3-P60 §4: "/" at the start of the composer lists the prompts of the
 * servers on here. Choosing one asks for its arguments, then fills the
 * composer with the server's text: the user reads it, may change it, and
 * sends it as an ordinary message. Nothing is sent by choosing. */

async function loadPrompts(generation) {
  try {
    const answer = await api("GET", `/api/conversations/${encodeURIComponent(app.conversationId)}/prompts`);
    if (generation === app.generation) {
      app.prompts = answer.prompts;
      updatePicker();
    }
  } catch (ignored) {
    if (generation === app.generation) app.prompts = [];
  }
}

function promptQuery() {
  const text = $("composer-text").value;
  if (!text.startsWith("/") || text.startsWith("//") || text.includes("\n")) return null;
  return text.slice(1).trim().toLowerCase();
}

function closePicker() {
  $("prompt-picker").hidden = true;
  $("composer-text").removeAttribute("aria-activedescendant");
  app.promptChoices = [];
}

function updatePicker() {
  const query = promptQuery();
  if (query === null || app.prompts === null || app.promptOpen !== null
      || $("composer-text").value === app.promptDismissed) {
    closePicker();
    return;
  }
  const words = query.split(/\s+/).filter(Boolean);
  const choices = app.prompts.filter((p) => {
    const hay = `${p.source} ${p.name} ${p.title}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
  if (choices.length === 0) {
    closePicker();
    return;
  }
  app.promptChoices = choices;
  app.promptSelected = Math.min(app.promptSelected, choices.length - 1);
  const list = $("prompt-list");
  list.replaceChildren();
  choices.forEach((prompt, index) => {
    const option = el("li", "prompt-option");
    option.id = `prompt-option-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(index === app.promptSelected));
    option.append(el("span", "prompt-option-name", `${prompt.source} · ${prompt.title || prompt.name}`));
    const about = [prompt.title ? prompt.name : "", prompt.description].filter(Boolean).join(" — ");
    if (about) option.append(el("span", "prompt-option-about", about));
    option.addEventListener("mousedown", (event) => event.preventDefault()); // keep the composer focused
    option.addEventListener("click", () => choosePrompt(prompt));
    list.append(option);
  });
  $("prompt-picker").hidden = false;
  $("composer-text").setAttribute("aria-activedescendant", `prompt-option-${app.promptSelected}`);
}

function pickerKey(event) {
  if ($("prompt-picker").hidden || app.promptChoices.length === 0) return false;
  const count = app.promptChoices.length;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    app.promptSelected = (app.promptSelected + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
    updatePicker();
  } else if ((event.key === "Enter" && !event.shiftKey && !event.isComposing) || event.key === "Tab") {
    choosePrompt(app.promptChoices[app.promptSelected]);
  } else if (event.key === "Escape") {
    app.promptDismissed = $("composer-text").value;
    closePicker();
  } else {
    return false;
  }
  event.preventDefault();
  return true;
}

function choosePrompt(prompt) {
  closePicker();
  abandonExpansion(); // another choice: an earlier one still on its way is dropped
  if (prompt.arguments.length === 0) {
    expandPrompt(prompt, {});
    return;
  }
  app.promptOpen = prompt;
  $("prompt-form-title").textContent = `${prompt.source} · ${prompt.title || prompt.name}`;
  $("prompt-form-about").textContent = prompt.description || "";
  const fields = $("prompt-form-fields");
  fields.replaceChildren();
  prompt.arguments.forEach((argument, index) => {
    const field = el("div", "prompt-field");
    const id = `prompt-argument-${index}`;
    const label = el("label", null, argument.required ? `${argument.name} (required)` : argument.name);
    label.htmlFor = id;
    const input = el("input");
    input.id = id;
    input.name = argument.name;
    input.required = Boolean(argument.required);
    input.autocomplete = "off";
    field.append(label);
    if (argument.description) field.append(el("span", "prompt-field-hint", argument.description));
    field.append(input);
    fields.append(field);
  });
  $("prompt-form").hidden = false;
  fields.querySelector("input").focus();
}

/* An expansion still on its way is abandoned: its answer is dropped. */
function abandonExpansion() {
  app.promptTicket += 1;
  $("prompt-form-submit").disabled = false;
}

function closePromptForm() {
  abandonExpansion();
  app.promptOpen = null;
  $("prompt-form").hidden = true;
  $("prompt-form-fields").replaceChildren();
}

async function submitPromptForm(event) {
  event.preventDefault();
  const prompt = app.promptOpen;
  if (prompt === null) return;
  const values = {};
  for (const input of $("prompt-form-fields").querySelectorAll("input")) {
    if (input.value !== "") values[input.name] = input.value;
  }
  await expandPrompt(prompt, values);
}

/* The answer fills the composer only if nothing moved on meanwhile: the
 * same conversation, this still the latest expansion (not cancelled, no
 * other prompt chosen, no message sent), and the composer as it was when
 * it was asked for. Otherwise it is dropped: the user's text wins. */
async function expandPrompt(prompt, values) {
  const generation = app.generation;
  app.promptTicket += 1;
  const ticket = app.promptTicket;
  const box = $("composer-text");
  const before = box.value;
  const current = () => generation === app.generation && ticket === app.promptTicket && box.value === before;
  $("prompt-form-submit").disabled = true;
  try {
    const answer = await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/prompts/expand`,
      { source: prompt.source, name: prompt.name, arguments: values });
    if (!current()) return;
    closePromptForm();
    box.value = answer.text;
    app.promptDismissed = answer.text;
    autosize();
    box.focus();
    box.setSelectionRange(0, 0);
    box.scrollTop = 0;
    status("The prompt is in the message box: read it, then send it.");
  } catch (error) {
    if (current()) status(error.message, "error");
  } finally {
    if (ticket === app.promptTicket) $("prompt-form-submit").disabled = false;
  }
}

async function setToolSource(source, enabled) {
  const generation = app.generation;
  try {
    const answer = await api("PUT",
      `/api/conversations/${encodeURIComponent(app.conversationId)}/tools/${encodeURIComponent(source)}`,
      { enabled });
    if (generation === app.generation) {
      renderToolSources(answer.sources);
      views.sources(answer.sources, true); // close its views here and in this browser's other tabs (V3-P58 §3.7)
      loadPrompts(generation); // the servers on here changed
    }
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
    if (page.items.length < 200) {
      views.autoOpen();
      return true;
    }
  }
}

/* ------------------------------------------------------------ conversation */

function describe(detail) {
  const job = detail.job;
  if (job.running) {
    const text = job.stop_requested === "cancel" ? "Stopping…" : job.stop_requested === "pause" ? "Pausing…" : "Working…";
    return { text, tone: null, active: true };
  }
  if (detail.waiting) {
    // e.g. "answer the question before compacting" when Compact now met an open question
    const why = job.state === "waiting_for_user" && job.detail ? ` (${job.detail})` : "";
    return { text: `${STATE_TEXT.waiting_for_user}${why}`, short: STATE_TEXT.waiting_for_user, tone: "warn",
             active: false };
  }
  // Compacting (or finding nothing to compact) answers nothing: an unanswered
  // message still wins, with the compaction result in front of it.
  const compacted = job.state === "compacted" || job.state === "nothing_to_compact";
  if (detail.continuable && !job.error && (job.state === null || job.state === "completed" || compacted)) {
    const done = compacted ? `${STATE_TEXT[job.state]}. ` : "";
    return { text: `${done}Not answered yet — Continue to answer it.`, short: "Not answered yet",
             tone: "warn", active: false };
  }
  if (job.error) return { text: ERROR_TEXT[job.error] || `The run failed (${job.error}).`, tone: "error", active: false };
  if (job.state === "compacted" || job.state === "nothing_to_compact") {
    const why = job.state === "nothing_to_compact" && job.detail ? ` (${job.detail})` : "";
    return { text: `${STATE_TEXT[job.state]}${why}`, short: STATE_TEXT[job.state], tone: "ok", active: false };
  }
  if (job.state && job.state !== "completed" && job.state !== "idle") {
    const base = STATE_TEXT[job.state] || job.state;
    // The detail says why (e.g. the size that was refused); shown as plain text.
    return { text: job.detail ? `${base} (${job.detail})` : base, short: base, tone: "warn", active: false };
  }
  return { text: job.state ? STATE_TEXT[job.state] : "", tone: null, active: false };
}

/* This turn's tokens against its budget (V3-P62 §7), as " · 132k in · 2.9k out / 1M". */
function usageText(usage) {
  if (!usage || !usage.runs) return "";
  const of = usage.budget ? ` / ${formatTokens(usage.budget)}` : "";
  return ` · ${formatTokens(usage.input_tokens)} in · ${formatTokens(usage.output_tokens)} out${of}`;
}

function applyDetail(detail) {
  const name = conversationName(detail.conversation);
  $("topbar-title").textContent = name;
  $("chat-menu-title").textContent = name;
  const view = describe(detail);
  $("topbar-chat-label").textContent = view.short || view.text;
  $("chat-menu-state").textContent = view.short || view.text || "Ready";
  $("topbar-state-dot").classList.toggle("is-active", view.active);
  // V3-P62 §7: a turn that stopped before its answer (a guard, a pause, a
  // failure) goes on with Continue, offered right where it stopped.
  const stopped = !detail.job.running && detail.continuable && !detail.waiting;
  $("stop-panel").hidden = !stopped;
  $("stop-continue").disabled = false;
  const used = usageText(detail.job.turn_usage);
  $("stop-text").textContent = stopped ? (used ? `This turn${used}` : "Continue where it stopped") : "";
  status(view.tone ? view.text : "", view.tone);
  $("question-panel").hidden = !detail.waiting;
  $("question-text").textContent = detail.waiting ? detail.waiting.question : "";
  $("composer-text").placeholder = detail.waiting ? "Your answer" : "Message HIMARI";
  app.jobRunning = detail.job.running;
  $("composer-send").disabled = detail.job.running || app.sending || uploading();
  $("composer-hint").textContent = detail.job.running ? `HIMARI is working…${usageText(detail.job.turn_usage)}` : "";
  $("chat-menu-resume").hidden = !detail.continuable;
  $("chat-menu-compact").disabled = detail.job.running;
  $("chat-menu-dismiss").hidden = !detail.waiting || detail.job.running;
  const stopping = detail.job.stop_requested;
  $("composer-pause").hidden = !detail.job.running;
  $("composer-pause").disabled = Boolean(stopping);
  $("composer-cancel").hidden = !detail.job.running;
  $("composer-cancel").disabled = stopping === "cancel";
  if (app.running !== detail.job.running) {
    app.running = detail.job.running;
    app.changedWhileRunning = false; // a new turn, or the end of the one it was changed in
    renderModel(); // "applies from the next message" only while that turn runs
  }
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
        loadServerInfo(); // a command may have found the sandbox broken
        loadToolSources(generation); // and close the views of a source turned off elsewhere
        loadModel(generation); // a stored choice may have been changed elsewhere
      }
    } catch (error) {
      if (generation !== app.generation || error.status === 401) return;
      if (error.status === 404) { // moved to the trash elsewhere (V3-P65): it is gone from here
        await showHome().catch(() => {});
        status("That conversation is no longer here; it may have been moved to the trash.", "warn");
        return;
      }
      status("Lost contact with the server; retrying…", "warn");
      poll(generation);
    }
  }, POLL_MS);
}

async function openConversation(id) {
  stopPolling();
  app.generation += 1;
  clearAttachments();
  const generation = app.generation;
  views.reset();
  app.conversationId = id;
  app.lastSequence = 0;
  loadServerInfo();
  app.toolRows = new Map();
  app.context = null;
  $("chat-menu-context-pair").hidden = true;
  $("chat-menu-compact").hidden = true;
  $("chat-menu-tools").hidden = true;
  $("chat-menu-model").hidden = true;
  app.modelChoice = null;
  app.running = false;
  app.changedWhileRunning = false;
  app.lastModel = null;
  app.prompts = null;
  app.promptDismissed = null;
  closePicker();
  closePromptForm();
  $("timeline").replaceChildren();
  markSelected();
  showSurface("chat");
  closeNav();
  loadToolSources(generation);
  loadModel(generation);
  loadPrompts(generation);
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
  const ready = app.attachments.filter((a) => a.state === "ready");
  if ((!text && !ready.length) || app.sending || uploading() || app.conversationId === null) return;
  abandonExpansion(); // a message sent now is the user's: no prompt answer may replace what follows
  const generation = app.generation;
  app.sending = true;
  $("composer-send").disabled = true;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/messages`,
      { text, attachments: ready.map((a) => a.fileId) });
    box.value = "";
    autosize();
    app.jobRunning = true; // a job starts with this message; the next detail says when it ends
    if (generation === app.generation) {
      app.attachments = app.attachments.filter((a) => !ready.includes(a));
      renderAttachments();
    }
    status("");
  } catch (error) {
    if (error.code === "attachment_refused" && error.fileId) { // which chip (V3-P66 §9)
      const refused = app.attachments.find((a) => a.fileId === error.fileId);
      if (refused) Object.assign(refused, { state: "failed", note: `${error.message} — upload it again` });
      renderAttachments();
      status(error.message, "error");
    } else if (error.kind === "network") {
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

async function stopConversation(action) {
  if (app.conversationId === null) return;
  const generation = app.generation;
  try {
    // pause: finish the current step, then stop; cancel: stop now; dismiss: close a waiting question
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/${action}`);
  } catch (error) {
    status(error.message, "error");
  }
  if (generation === app.generation) poll(generation);
}

async function dismissQuestion() {
  closeChatMenu();
  await stopConversation("dismiss");
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

/* ---------------------------------------------------------------- trash */

/* A confirmation over everything else; resolves true only for the action
 * button. Escape, Cancel or the backdrop: false. */
function confirmAction(title, lines, action) {
  const dialog = $("confirm-dialog");
  const before = document.activeElement;
  $("confirm-title").textContent = title;
  $("confirm-body").replaceChildren(...lines.map((line) => el("p", null, line)));
  $("confirm-ok").textContent = action;
  dialog.hidden = false;
  $("confirm-cancel").focus();
  return new Promise((resolve) => {
    const done = (answer) => {
      dialog.hidden = true;
      dialog.removeEventListener("keydown", key);
      dialog.removeEventListener("click", outside);
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      if (before && typeof before.focus === "function") before.focus();
      resolve(answer);
    };
    const key = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        done(false);
      } else if (event.key === "Tab") { // the focus stays in the dialog
        const first = $("confirm-cancel");
        const last = $("confirm-ok");
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    const outside = (event) => {
      if (event.target === dialog) done(false);
    };
    dialog.addEventListener("keydown", key);
    dialog.addEventListener("click", outside);
    $("confirm-ok").onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
  });
}

function localDate(iso) {
  return new Date(iso).toLocaleDateString();
}

/* V3-P65 §6.1: what deleting does not reach. */
function externalLine(notice) {
  if (!notice.servers.length) return "Data stored by external services is not deleted.";
  const names = notice.servers.map((s) => (notice.unconfigured.includes(s) ? `${s} (no longer configured)` : s));
  return `Data stored by the external servers this conversation used (${names.join(", ")}) is not deleted.`;
}

async function deleteConversation() {
  closeChatMenu();
  const id = app.conversationId;
  if (id === null) return;
  const name = $("topbar-title").textContent;
  const generation = app.generation;
  const still = () => generation === app.generation && app.conversationId === id; // not left for another
  let notice;
  try {
    notice = await api("GET", `/api/conversations/${encodeURIComponent(id)}/delete-notice`);
  } catch (error) {
    if (still()) status(error.message, "error");
    return;
  }
  if (!still()) return;
  const lines = [`It moves to the trash and is deleted for good after ${notice.trash_days} days. `
                 + "Until then you can restore it from the trash."];
  if (notice.running) lines.push("The work under way is stopped first.");
  lines.push(externalLine(notice));
  if (!(await confirmAction(`Delete “${name}”?`, lines, "Move to trash")) || !still()) return;
  $("chat-menu-delete").disabled = true;
  status(notice.running ? "Stopping the work under way, then moving it to the trash…" : "Moving it to the trash…");
  try {
    const moved = await api("POST", `/api/conversations/${encodeURIComponent(id)}/trash`);
    // Moved: a list that fails to load now must not read as a failed move.
    await (app.conversationId === id ? showHome() : loadList(true).then(markSelected)).catch(() => {});
    status(`Moved “${name}” to the trash. It is deleted for good on ${localDate(moved.purge_after)}.`, "ok");
  } catch (error) {
    if (error.kind === "network") {
      status("Could not confirm the move; check the trash before trying again.", "warn");
    } else {
      status(error.message, "error");
    }
  } finally {
    $("chat-menu-delete").disabled = false;
  }
}

function renderTrashRow(item) {
  const li = el("li", "trash-row");
  const main = el("div", "trash-row-main");
  main.append(el("span", "row-title", conversationName(item)),
              el("span", "row-meta", `Moved ${new Date(item.trashed_at).toLocaleString()}`
                 + ` · deleted for good on ${localDate(item.purge_after)}`));
  const actions = el("div", "trash-row-actions");
  const restore = el("button", null, "Restore");
  restore.type = "button";
  restore.addEventListener("click", () => restoreConversation(item, restore));
  const purge = el("button", "danger-button", "Delete now");
  purge.type = "button";
  purge.addEventListener("click", () => purgeConversation(item, purge));
  actions.append(restore, purge);
  li.append(main, actions);
  return li;
}

async function loadTrash(reset) {
  if (reset) app.trashOffset = 0;
  const generation = app.generation;
  const page = await api("GET", `/api/conversations?trashed=1&limit=${PAGE}&offset=${app.trashOffset}`);
  if (generation !== app.generation) return;
  const list = $("trash-list");
  if (reset) list.replaceChildren();
  for (const item of page.items) list.append(renderTrashRow(item));
  app.trashOffset = page.next_offset ?? app.trashOffset + page.items.length;
  $("trash-load-more").hidden = page.next_offset === null;
  $("trash-empty").hidden = list.children.length > 0;
}

async function showTrash() {
  stopPolling();
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  clearAttachments();
  markSelected();
  showSurface("trash");
  closeNav();
  $("topbar-title").textContent = "Trash";
  $("trash-hint").textContent = (app.trashDays ? `Conversations here are deleted for good ${app.trashDays} days`
    + " after they were moved. " : "") + "Restore one to read it again.";
  status("");
  await loadTrash(true);
}

async function restoreConversation(item, button) {
  button.disabled = true;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(item.id)}/restore`);
    status(`Restored “${conversationName(item)}”.`, "ok");
  } catch (error) {
    status(error.message, "error");
  }
  await Promise.all([loadTrash(true), loadList(true).then(markSelected)]).catch(() => {});
}

async function purgeConversation(item, button) {
  const name = conversationName(item);
  const lines = ["This cannot be undone. Its messages, its records and its files are deleted.",
                 "Data stored by external services is not deleted."];
  if (!(await confirmAction(`Delete “${name}” for good?`, lines, "Delete for good"))) return;
  button.disabled = true;
  try {
    await api("DELETE", `/api/conversations/${encodeURIComponent(item.id)}`);
    status(`Deleted “${name}” for good.`, "ok");
  } catch (error) {
    status(error.kind === "network" ? "Could not confirm the deletion; check the trash again." : error.message,
           error.kind === "network" ? "warn" : "error");
  }
  await loadTrash(true).catch(() => {});
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

const SANDBOX_TEXT = {
  enforced: "commands ready",
  capable_inactive: "commands off (sandbox disabled)",
  unavailable: "commands unavailable on this host",
  setup_failed: "commands unavailable (sandbox setup failed)",
};

async function loadServerInfo() {
  const info = $("server-info");
  try {
    const server = await api("GET", "/api/server");
    info.textContent = `${server.model} · ${SANDBOX_TEXT[server.sandbox] || server.sandbox}`;
    app.models = Array.isArray(server.models) ? server.models : [];
    app.trashDays = Number.isInteger(server.trash_days) ? server.trash_days : null;
    renderModel();
    info.classList.toggle("is-warn", server.sandbox !== "enforced");
    info.hidden = false;
  } catch (error) {
    info.hidden = true; // display only
  }
}

async function enter() {
  loadServerInfo();
  await showHome();
}

async function boot() {
  $("login-form").addEventListener("submit", signIn);
  $("nav-sign-out").addEventListener("click", signOut);
  $("nav-trash").addEventListener("click", () => showTrash().catch((e) => status(e.message, "error")));
  $("trash-load-more").addEventListener("click", () => loadTrash(false).catch((e) => status(e.message, "error")));
  $("chat-menu-delete").addEventListener("click", deleteConversation);
  $("nav-home").addEventListener("click", () => showHome().catch((e) => status(e.message, "error")));
  $("nav-new-chat").addEventListener("click", newConversation);
  $("home-new-chat").addEventListener("click", newConversation);
  $("conversation-load-more").addEventListener("click", () => loadList(false).then(markSelected));
  $("composer").addEventListener("submit", sendMessage);
  $("composer-attach").addEventListener("click", () => $("composer-files").click());
  $("composer-files").addEventListener("change", (event) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = ""; // the same file may be chosen again
  });
  const dropZone = $("surface-chat");
  dropZone.addEventListener("dragover", (event) => {
    if (app.conversationId === null || !event.dataTransfer || !event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dropZone.classList.add("is-dropping");
  });
  dropZone.addEventListener("dragleave", (event) => {
    if (event.target === dropZone) dropZone.classList.remove("is-dropping");
  });
  dropZone.addEventListener("drop", (event) => {
    dropZone.classList.remove("is-dropping");
    if (app.conversationId === null || !event.dataTransfer || !event.dataTransfer.files.length) return;
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  });
  $("composer-text").addEventListener("input", () => {
    autosize();
    updatePicker();
  });
  $("composer-text").addEventListener("blur", () => setTimeout(closePicker, 150));
  $("prompt-form").addEventListener("submit", submitPromptForm);
  $("prompt-form-cancel").addEventListener("click", () => {
    closePromptForm();
    $("composer-text").focus();
  });
  $("prompt-form").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePromptForm();
      $("composer-text").focus();
    }
  });
  $("composer-text").addEventListener("keydown", (event) => {
    if (pickerKey(event)) return;
    if (event.key === "Escape") abandonExpansion(); // like Cancel: a pending prompt will not fill the box
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("chat-menu-toggle").addEventListener("click", toggleChatMenu);
  $("chat-menu-rename").addEventListener("click", renameConversation);
  $("chat-menu-resume").addEventListener("click", resumeConversation);
  $("chat-menu-compact").addEventListener("click", compactConversation);
  $("chat-menu-dismiss").addEventListener("click", dismissQuestion);
  $("chat-menu-model-select").addEventListener("change", (event) => setModel(event.target.value || null, null));
  $("chat-menu-effort-select").addEventListener("change", (event) =>
    setModel($("chat-menu-model-select").value || null, event.target.value || null));
  $("composer-pause").addEventListener("click", () => stopConversation("pause"));
  $("stop-continue").addEventListener("click", () => {
    $("stop-continue").disabled = true;  // one Continue per click; the next detail enables it again
    resumeConversation();
  });
  $("composer-cancel").addEventListener("click", () => stopConversation("cancel"));
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
