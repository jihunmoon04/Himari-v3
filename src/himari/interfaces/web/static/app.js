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
import { assistantTextLine, renderAssistantText } from "/assets/rich_text.js";
import { LiveAnswer } from "/assets/live.js";
import { FilesPane } from "/assets/files.js";
import * as localStore from "/assets/local_store.js";
import { DraftBook, mergeDrafts, referenceKey, withoutExpired } from "/assets/drafts.js";

const POLL_MS = 1000;
const PAGE = 50;
const MAX_REFERENCES = 10; // a message's file references (V3-P76 §3.1)
const drafts = new DraftBook(localStore, { writer: localStore.WRITER }); // the composer's, by place (V3-P78 §3)

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
  startingConversation: null,
  place: null, // whose the composer is (V3-P78 §3.1): "home", a conversation id, {creating, from, carried}, or null
  placeTouched: false, // the composer was changed in this place since it was entered (§3.3)
  placeEntry: 0, // the latest entry into a place: a later read of the store applies to that one only
  inFlight: new Set(), // the conversations a message is being sent to from this page (V3-P78 §3.1, N6)
  workingHint: "", // "HIMARI is working…" and its usage, under the composer
  lengthHint: null, // {count, over} once the message nears its limit
  questionShown: null, // the question text the panel shows (redrawn only when it changes)
  address: null, // the address of the surface shown (V3-P79 §3); the sign-in screen keeps the one it came from
  activity: { working: new Set(), waiting: new Set() }, // the last /activity answer (V3-P80 §3)
  activityTimer: null,
  activityAsked: new Set(), // ids not in the list that the list was read again for once (§3.3)
  activityTicket: 0, // the latest /activity read: an older answer is dropped (review N3)
  listTicket: 0, // the latest list read
  listResetting: 0, // the ticket of a reload under way (0: none)
  sendWaiting: false, // a send waits for the conversation being created
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
  work: null, // the work block the next tool step goes into (V3-P68 §5.1)
  rowsRunning: null, // the job state the rows without a result were last painted for
  attachKeys: 0,
  references: [], // parts of files for the next message (V3-P76 §3.1): {key, path, name, identity, whole, start, end, sha256, label}
  panelStart: null, // {id, path, start, end}: the file the panel opens on for conversation ``id`` (Ask from Files)
  instructions: null, // the saved instructions (V3-P69 §7): {text, revision, updated_at, max_chars}
  savingInstructions: false,
  instructionsSave: null, // the save under way, for a leave to wait on
  conflictRevision: null, // shown a conflict: the version "save mine" replaces
  instructionsDraft: null, // {text, base}: a save that met a signed-out session, restored after sign-in
  leaveAgreed: false, // "Leave without saving?" answered yes, until the surface changes
  leaveAsked: false, // that question is open
  instructionsTicket: 0, // the latest load: an older answer is not applied
  instructionsLoading: false,
  memory: null, // the last /api/memories answer (V3-P71 §7.2)
  registeredFolders: null, // the last /api/workspaces items, for the attach field (V3-P74 §10.3)
  streaming: false, // the server streams answers (V3-P73 §9)
  filesOpen: false, // the files panel beside the conversation is open (V3-P75 §3.2)
  workingFolder: null, // the open conversation's working folder, when it works on this server's folders
  conversationFolderPaths: [], // all of its folders: a folder already among them is not offered to attach
  folderTicket: 0, // the latest "conversations in this folder" read: older answers are dropped
};

const views = new AppViews(api, el, () => app.conversationId);

/* The server's files (V3-P75 §3): one pane on the Files surface, one beside a conversation.
 * On the surface a folder opens its conversation (§6); beside one, it is attached to it. */
const askFiles = (title, lines, action) => confirmAction(title, lines, action);
// V3-P78 §4.1: a file open with unsaved text in the other pane: what was kept for it is not offered.
const openUnsaved = (pane, path) => pane.doc !== null && pane.doc.path === path && pane.doc.dirty;
const filesSurface = new FilesPane({
  api, el, confirm: askFiles,
  keep: localStore, writer: `${localStore.WRITER}:files`, // unsaved text kept in this browser (V3-P78 §4.1)
  openElsewhere: (path) => openUnsaved(filesPanel, path),
  onAsk: (reference) => askFromFiles(reference),
  folderActions: (listing) => [openHereControl(listing.path)],
  folderRowActions: (path) => [["Open conversation", () => openFolderConversation(path, false)],
                               ["New conversation here", () => openFolderConversation(path, true)]],
});
const filesPanel = new FilesPane({
  api, el, confirm: askFiles,
  keep: localStore, writer: `${localStore.WRITER}:panel`,
  openElsewhere: (path) => openUnsaved(filesSurface, path),
  working: () => app.running, // HIMARI at work in this conversation (§7.3)
  onAsk: (reference) => { // the conversation shown again, where its chip goes
    setPanelExpanded(false);
    if (panelCoversScreen() && app.filesOpen) toggleFilesPanel();
    addReference(reference);
  },
  actions: [$("file-panel-changes"), $("file-panel-expand"), $("file-panel-close")],
  changes: { // V3-P76 §5: each call names its conversation, fixed when it starts (PR #99 review C2)
    of: () => app.conversationId,
    list: async (of) => ({ ...(await api("GET", conversationChanges(of))), id: of }),
    diff: (of, path) => api("GET", conversationChanges(of, `/diff?path=${encodeURIComponent(path)}`)),
    accept: (of, paths) => api("POST", conversationChanges(of, "/accept"), paths ? { paths } : { all: true }),
    revert: (of, path, seen) => api("POST", conversationChanges(of, "/revert"),
                                    seen ? { path, seen: { exists: seen.exists, identity: seen.identity } } : { path }),
    revertAll: (of) => api("POST", conversationChanges(of, "/revert-all"), {}),
    seen: (list) => showChanges(list),
  },
  folderActions: (listing) => (attachable(listing.path) ? [attachHereButton(listing.path)] : []),
  folderRowActions: (path) => (attachable(path) ? [["Attach to this conversation", () => attachHere(path)]] : []),
});

/* The answer being written (V3-P73 §7, live.js): its own item at the end of
 * the timeline while a job runs, replaced by the Run's Event when it comes. */
const live = new LiveAnswer({
  document,
  timeline: () => $("timeline"),
  header: () => assistantHeader(null),
  render: (text) => renderAssistantText(document, text), // redrawn every 100 ms: no Copy until it ends (B2)
  style: () => getComputedStyle(document.documentElement).getPropertyValue("--thinking-style"),
  reducedMotion: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  now: () => Date.now(),
  random: Math.random,
  frame: (fn) => requestAnimationFrame(fn),
  EventSource: globalThis.EventSource,
});

/* ``action``: [label, run] — one button after the text (e.g. Undo), gone with the next status. */
function status(text, tone, action = null) {
  const bar = $("status-bar");
  bar.textContent = text || "";
  bar.className = tone ? `status-bar is-${tone}` : "status-bar";
  if (action && text) {
    const [label, run] = action;
    const button = el("button", "link-button status-action", label);
    button.type = "button";
    button.addEventListener("click", () => { button.disabled = true; run(); });
    bar.append(" ", button);
  }
}

/* ``via`` (V3-P79 §3.2): how the address follows — "push" for a move the user chose, "replace" for a place
 * the page was sent to, "none" for Back/Forward (the browser moved it already). */
function showSurface(name, via = "none") {
  closeModelDialog();
  for (const surface of ["login", "home", "chat", "trash", "instructions", "memory", "folders", "files"]) {
    $(`surface-${surface}`).hidden = surface !== name;
  }
  followAddress(name, via);
  syncJump(); // the composer (and the button in it) moved: shown only beside a scrolled-up conversation
  app.leaveAgreed = false;
  const signedIn = name !== "login";
  $("sidebar").hidden = !signedIn;
  $("sidebar-toggle").hidden = !signedIn;
  syncNavigation();
  $("topbar-chat-context").hidden = name !== "chat";
  syncFilesPanel(name === "chat");
  $("chat-menu-toggle").hidden = name !== "chat";
  if (name !== "chat") closeChatMenu();
  syncModelChrome();
  syncFullscreen();
  // One composer, including its draft and staged files, on either surface.
  if (name === "home" || name === "chat") {
    (name === "home" ? $("home-composer-slot") : $("surface-chat")).append($("composer-wrap"));
    autosize();
  }
}

/* ------------------------------------------------------------- sign-in */

/* ``signingOut``: nothing is kept (V3-P78 §5.3); else the session ended, and what the user wrote is kept
 * for the next sign-in (§6). Shown already: nothing changes, the token being typed stays. */
function showLogin({ signingOut = false } = {}) {
  if (!$("surface-login").hidden) return;
  stopPolling();
  clearTimeout(app.activityTimer); // V3-P80: read again after the sign-in
  app.activityTicket += 1; // a read answered before this never paints the sign-in screen (review N2)
  app.activity = { working: new Set(), waiting: new Set() };
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace({ keep: !signingOut });
  filesPanel.closeForSignIn({ keepText: !signingOut });
  filesPanel.file = null; // its conversation is opened again from Home
  filesSurface.closeForSignIn({ keepText: !signingOut, keepInk: !signingOut });
  showSurface("login");
  $("topbar-title").textContent = "HIMARI";
  $("server-info").hidden = true;
  $("server-details").hidden = true;
  $("server-details").open = false;
  $("appearance-dialog").close();
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

/* V3-P78 §5.3: what can be cancelled first; then nothing more is written, the session ends, and what this
 * browser kept goes, in every tab. */
async function signOut() {
  if (!(await mayLeave())) return;
  const kept = await localStore.count();
  if (kept && !(await confirmAction("Sign out?", [`Unsaved drafts kept in this browser (${kept}) will be deleted.`],
                                    "Sign out"))) return;
  localStore.closeWrites();
  drafts.clear();
  showLogin({ signingOut: true });
  history.replaceState(null, "", "/"); // no conversation's address left behind (V3-P79 §3.3)
  app.address = "/";
  try {
    await api("DELETE", "/api/session");
  } catch (ignored) {
    /* signed out here either way */
  } finally {
    await localStore.wipe();
  }
}

/* ---------------------------------------------------------- conversations */

function conversationName(item) {
  return item.title || item.preview || "New conversation";
}

/* V3-P79 §3.4 (Q4): a conversation as a link: a plain press opens it here; with a modifier, the middle
 * button or "Open in New Tab", the browser opens its address. */
function conversationLink(id, className, text = undefined) {
  const link = el("a", className, text);
  link.href = `/c/${encodeURIComponent(id)}`;
  link.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openConversation(id);
  });
  return link;
}

function renderListRow(item) {
  const li = el("li", "conversation-row");
  const button = conversationLink(item.id, "conv-row", conversationName(item)); // a link (V3-P79 §3.4)
  button.dataset.id = item.id;
  button.title = conversationName(item);
  if (item.id === app.conversationId) button.classList.add("is-selected");
  if (item.id === app.conversationId) button.setAttribute("aria-current", "page");
  const actions = el("details", "conversation-actions");
  const toggle = el("summary", null, "⋯");
  toggle.setAttribute("aria-label", `Actions for ${conversationName(item)}`);
  toggle.title = `Actions for ${conversationName(item)}`;
  const menu = el("div", "conversation-action-list");
  for (const [label, action] of [["Rename", () => renameConversation(item)], ["Move to trash", () => deleteConversation(item)]]) {
    const control = el("button", "link-button", label);
    control.type = "button";
    control.addEventListener("click", () => { actions.open = false; action(); });
    menu.append(control);
  }
  actions.append(toggle, menu);
  actions.addEventListener("toggle", () => {
    if (actions.open) for (const other of $("conversation-list").querySelectorAll("details[open]")) {
      if (other !== actions) other.open = false;
    }
  });
  li.append(button, actions);
  return li;
}

async function loadList(reset) {
  // The latest read draws the list: a reload and "Load more" under way at once never mix (V3-P80 review N2).
  // "Load more" waits out a reload, which would otherwise be dropped for it (re-review N4).
  if (!reset && app.listResetting) return [];
  const ticket = (app.listTicket += 1);
  if (reset) app.listResetting = ticket;
  const offset = reset ? 0 : app.listOffset;
  let page;
  try {
    page = await api("GET", `/api/conversations?limit=${PAGE}&offset=${offset}`);
  } finally {
    if (app.listResetting === ticket) app.listResetting = 0;
  }
  if (ticket !== app.listTicket) return page.items;
  const list = $("conversation-list");
  if (reset) list.replaceChildren();
  for (const item of page.items) list.append(renderListRow(item));
  app.listOffset = page.next_offset ?? offset + page.items.length;
  $("conversation-load-more").hidden = page.next_offset === null;
  paintActivity();
  return page.items;
}

function markSelected() {
  for (const button of $("conversation-list").querySelectorAll(".conv-row")) {
    button.classList.toggle("is-selected", button.dataset.id === app.conversationId);
    if (button.dataset.id === app.conversationId) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  paintActivity(); // rows drawn again get their dots back
}

/* ------------------------------------------------- other conversations (V3-P80 §3) */

const ACTIVITY_MS = 5000;
const ACTIVITY_LABEL = { working: "Working", waiting: "Waiting for your answer" };

/* A dot on each conversation in the list and on Home's cards: working, or waiting for an answer. */
function paintActivity() {
  const rows = [...$("conversation-list").querySelectorAll(".conv-row"),
                ...$("home-recent-list").querySelectorAll(".home-recent-card")];
  for (const row of rows) {
    const id = row.dataset.id;
    const state = app.activity.waiting.has(id) ? "waiting" : app.activity.working.has(id) ? "working" : null;
    let dot = row.querySelector(".activity-dot");
    if (state === null) {
      if (dot) dot.remove();
      continue;
    }
    if (!dot) {
      dot = el("span", "activity-dot");
      dot.setAttribute("role", "img");
      row.append(dot);
    }
    dot.className = `activity-dot is-${state}`;
    dot.title = ACTIVITY_LABEL[state];
    dot.setAttribute("aria-label", ACTIVITY_LABEL[state]);
  }
  followTitle();
}

/* Every 5 s while the page shows and the user is signed in; a hidden page reads again when shown. */
function scheduleActivity(delay = ACTIVITY_MS) {
  clearTimeout(app.activityTimer);
  app.activityTimer = setTimeout(readActivity, delay);
}

async function readActivity() {
  clearTimeout(app.activityTimer);
  if (!$("surface-login").hidden || document.visibilityState === "hidden") return;
  const ticket = (app.activityTicket += 1);
  try {
    const answer = await api("GET", "/api/conversations/activity");
    if (ticket !== app.activityTicket) return; // a newer read is on its way, and schedules the next
    app.activity = { working: new Set(answer.working), waiting: new Set(answer.waiting) };
    const listed = new Set([...$("conversation-list").querySelectorAll(".conv-row")].map((row) => row.dataset.id));
    // One started elsewhere (another tab, the CLI): the list once more, so it is there (§3.3).
    const unknown = [...answer.working, ...answer.waiting].filter((id) => !listed.has(id) && !app.activityAsked.has(id));
    if (unknown.length) {
      for (const id of unknown) app.activityAsked.add(id);
      await loadList(true).then(markSelected).catch(() => {});
    }
    paintActivity();
  } catch (error) {
    // The sign-in screen: reading starts again after it. Any other failure (a 401 a proxy wrote, the
    // network) tries again later (review N3).
    if (!$("surface-login").hidden) return;
  }
  if ($("surface-login").hidden) scheduleActivity();
}

async function showHome({ via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace();
  markSelected();
  showSurface("home", via);
  $("composer-send").disabled = false;
  $("composer-pause").hidden = true;
  $("composer-cancel").hidden = true;
  $("question-panel").hidden = true;
  $("stop-panel").hidden = true;
  app.workingHint = "";
  paintHint();
  $("composer-text").placeholder = "Ask HIMARI anything…";
  closePicker();
  closePromptForm();
  status("");
  app.prompts = null;
  enterPlace("home");
  $("topbar-title").textContent = "HIMARI";
  const items = await loadList(true);
  const recent = $("home-recent-list");
  recent.replaceChildren();
  for (const item of items.slice(0, 3)) {
    const li = el("li");
    const card = conversationLink(item.id, "home-recent-card");
    card.dataset.id = item.id;
    card.append(el("span", "home-recent-title", conversationName(item)),
                el("span", "home-recent-activity", new Date(item.last_activity_at).toLocaleDateString()));
    li.append(card);
    recent.append(li);
  }
  $("home-recent-empty").hidden = items.length > 0;
  paintActivity();
}

/* ``carry``: the composer's draft goes into the new conversation (sent or given files from Home); else the
 * place is left with its draft and the new conversation starts empty (New chat). V3-P78 §3.1. */
async function newConversation({ carry = false } = {}) {
  if (app.startingConversation) return app.startingConversation;
  // Lock before even the leave/save check: repeated clicks may be waiting on
  // the same instruction save, and must not each create a conversation.
  const pending = (async () => {
    if (!(await mayLeave())) return false;
    beginCreating(carry);
    const opened = await createConversation();
    if (!opened) endCreating();
    return opened;
  })();
  app.startingConversation = pending;
  try { return await pending; }
  finally { if (app.startingConversation === pending) app.startingConversation = null; }
}

async function createConversation() {
  const generation = app.generation;
  $("nav-new-chat").disabled = true;
  $("home-new-chat").disabled = true;
  $("composer-send").disabled = true;
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
    return false;
  } finally {
    $("nav-new-chat").disabled = false;
    $("home-new-chat").disabled = false;
    if (generation === app.generation) $("composer-send").disabled = false;
  }
  if (generation !== app.generation) return false; // navigation while creating never sends a draft elsewhere
  await loadList(true).catch(() => {});
  if (generation !== app.generation) return false;
  await openConversation(created.id, { created: true });
  $("composer-text").focus();
  return app.conversationId === created.id && app.generation === generation + 1;
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
  if (!user) node.append(renderAssistantText(document, event.text || "", { copy: copyText }));
  if (!user && event.text) node.append(answerActions(event.text));
  else if (event.text) node.append(el("p", "item-text", event.text));
  if (user && event.attachments && event.attachments.length) node.append(fileChips(event.attachments));
  if (user && event.references && event.references.length) node.append(sentReferenceChips(event.references));
  return node;
}

/* ------------------------------------------------------------- addresses */

const SURFACE_ADDRESS = { home: "/", files: "/files", trash: "/trash", instructions: "/settings/instructions",
                          memory: "/settings/memory", folders: "/settings/folders" };
const CONVERSATION_ADDRESS = /^\/c\/([A-Za-z0-9_-]{1,128})$/;

/* V3-P79 §3.2: the surface now shown, at its address. The sign-in screen leaves the address as it is. */
function followAddress(name, via) {
  if (name === "login") return;
  const address = name === "chat" ? `/c/${encodeURIComponent(app.conversationId)}` : SURFACE_ADDRESS[name];
  if (!address) return;
  app.address = address;
  if (location.pathname === address) return;
  // "none": Back/Forward moved the address already — unless a leave asked meanwhile, and another move
  // came in between (design review B1): then the screen shown wins.
  if (via === "replace" || via === "none") history.replaceState(null, "", address);
  else history.pushState(null, "", address);
}

/* The surface at ``path`` (an unknown one: Home). Whether it is shown now (a refused leave keeps the last). */
async function goTo(path, via) {
  const conversation = CONVERSATION_ADDRESS.exec(path);
  const surface = Object.keys(SURFACE_ADDRESS).find((name) => SURFACE_ADDRESS[name] === path);
  if (conversation) await openConversation(conversation[1], { via });
  else if (surface === "files") await showFiles({ via });
  else if (surface === "trash") await showTrash({ via });
  else if (surface === "instructions") await showInstructions({ via });
  else if (surface === "memory") await showMemory({ via });
  else if (surface === "folders") await showFolders({ via });
  else await showHome({ via: surface === "home" ? via : "replace" });
  return app.address === path;
}

/* Back or Forward (§3.2). The same address — an app view's full-screen entry (V3-P63) — is app_view.js's. */
function addressPopped() {
  if (!$("surface-login").hidden) return; // signed out: the address waits for the sign-in
  const wanted = location.pathname;
  if (wanted === app.address) return;
  goTo(wanted, "none").then(() => {
    // A leave refused (unsaved text, an upload): the address is the surface's still shown, in this entry
    // (a push would add an entry, or drop the Forward ones; design review B1).
    if (app.address !== null && location.pathname !== app.address) history.replaceState(null, "", app.address);
  }).catch((error) => status(error.message, "error"));
}

/* The tab's title follows the top bar's: "<conversation> · HIMARI" (§3.4). */
function followTitle() {
  const shown = $("topbar-title").textContent.trim();
  const title = !shown || shown === "HIMARI" ? "HIMARI" : `${shown} · HIMARI`;
  const waiting = app.activity.waiting.size; // V3-P80 §3.4: conversations waiting for an answer, this one too
  document.title = waiting ? `(${waiting}) ${title}` : title;
}

/* ------------------------------------------------------------ shortcuts */

/* The new-chat shortcut as the keyboard names it: ⌘ on Apple devices. */
function newChatShortcut() {
  const apple = /Mac|iPhone|iPad|iPod/.test(navigator.platform || "") || /Mac OS/.test(navigator.userAgent || "");
  return apple ? "⌘⇧O" : "Ctrl+Shift+O";
}

/* Cmd/Ctrl+Shift+O: a new chat (as ChatGPT); Shift+Esc: the message box. Signed in, with no dialog open. */
function shortcut(event) {
  // Any dialog: a <dialog> open, or the confirmation (a div; review B2).
  if (!$("surface-login").hidden || document.querySelector("dialog[open], [aria-modal='true']:not([hidden])")) return;
  if (event.isComposing || event.repeat) return; // held down: one chat, not one per repeat (N4)
  // The key, not the character: a Korean layout types "ㅒ" there (N4).
  if ((event.metaKey || event.ctrlKey) && event.shiftKey && !event.altKey && event.code === "KeyO") {
    event.preventDefault();
    $("nav-new-chat").click();
  } else if (event.key === "Escape" && event.shiftKey && !$("composer-wrap").closest("[hidden]")) {
    event.preventDefault();
    $("composer-text").focus();
  }
}

/* --------------------------------------------------- the end of the timeline */

/* "↓" above the composer while the conversation is scrolled up from its end; it goes there. */
function syncJump() {
  if (app.jumpFrame) return; // once a frame, however many changes (review N5)
  app.jumpFrame = requestAnimationFrame(() => {
    app.jumpFrame = 0;
    paintJump();
  });
}

function paintJump() {
  const timeline = $("timeline");
  const away = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight > 300;
  $("jump-latest").hidden = !away || $("surface-chat").hidden;
}

/* The composer's count, once the message nears its limit (32,000 characters). */
function syncLength() {
  const box = $("composer-text");
  // Fewer units than the threshold: fewer characters too, no need to count.
  const count = box.value.length < LENGTH_HINT_AT ? 0 : characters(box.value);
  app.lengthHint = count < LENGTH_HINT_AT ? null : { count, over: count > MAX_MESSAGE_CHARS };
  paintHint();
}

/* The hint under the composer has two writers (review B1): the turn at work, from the conversation's state,
 * and the message's count near its limit. One line, the work first; the warning colour is the count's. */
function paintHint() {
  const hint = $("composer-hint");
  const length = app.lengthHint;
  const count = length === null ? ""
    : `${length.count.toLocaleString()} / ${MAX_MESSAGE_CHARS.toLocaleString()} characters`
      + (length.over ? ": too long to send; attach it as a file" : "");
  hint.textContent = [app.workingHint, count].filter(Boolean).join(" · ");
  hint.classList.toggle("is-over", length !== null && length.over);
  hint.classList.toggle("is-counting", length !== null);
}

const LENGTH_HINT_AT = 30000;

/* ---------------------------------------------------------------- paste */

const MAX_MESSAGE_CHARS = 32000; // the server's (characters, as Python counts them)
const PLAIN_NAMES = /^(image|img|pasted|screenshot|untitled)?\.(png|jpe?g|gif|webp|heic|tiff?|bmp)$/i;
const PASTE_TYPES = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
                      "image/heic": "heic", "image/tiff": "tiff", "text/plain": "txt", "application/pdf": "pdf" };

function extensionOf(name) {
  const dot = (name || "").lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : null;
}

/* pasted-YYYYMMDD-HHMMSS[-n].ext: several things pasted at once stay apart (V3-P80 §4.1). */
function pastedName(extension, index = 0) {
  const now = new Date();
  const two = (n) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}`
    + `${two(now.getMinutes())}${two(now.getSeconds())}`;
  return `pasted-${stamp}${index ? `-${index + 1}` : ""}.${extension}`;
}

/* V3-P80 §4: files alone on the clipboard are attached (a screenshot, a copied file); with text, the text
 * is pasted as always, unless it would take the message past its limit: then it is attached as a .txt. */
function pasteIntoComposer(event) {
  const data = event.clipboardData;
  if (!data) return;
  const text = data.getData("text/plain");
  let files = Array.from(data.files || []);
  if (!files.length && data.items) {
    files = Array.from(data.items).filter((item) => item.kind === "file").map((item) => item.getAsFile())
      .filter(Boolean);
  }
  if (files.length && !text) {
    event.preventDefault();
    addFiles(files.map((file, index) => (file.name && !PLAIN_NAMES.test(file.name) ? file
      : new File([file], pastedName(PASTE_TYPES[file.type] || extensionOf(file.name) || "bin", index),
                 { type: file.type }))));
    return;
  }
  if (!text) return;
  // The message as it would be, the selection replaced, counted as the server counts (review N5). Over the
  // limit already: anything pasted goes as a file.
  const box = $("composer-text");
  const kept = box.value.slice(0, box.selectionStart) + box.value.slice(box.selectionEnd);
  if (kept.length + text.length <= MAX_MESSAGE_CHARS) return; // fewer UTF-16 units: fewer characters too
  // A character is at most two units: past twice the limit, no need to count (a huge paste; review N5).
  if (kept.length + text.length <= 2 * MAX_MESSAGE_CHARS
      && characters(kept) + characters(text) <= MAX_MESSAGE_CHARS) return;
  event.preventDefault();
  addFiles([new File([text], pastedName("txt"), { type: "text/plain" })]);
  status("The pasted text was long, so it was attached as a file.", "ok");
}

/* ---------------------------------------------------------------- Enter */

/* V3-P79 §4.1: Enter sends from a hardware keyboard and makes a new line from an on-screen one; Shift+Enter
 * is a new line, Cmd/Ctrl+Enter sends; an Enter that ends a composition (Korean, Japanese…) is the IME's —
 * Safari reports it with isComposing false but keyCode 229. */
function enterSends(event) {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return false;
  if (event.metaKey || event.ctrlKey) return true;
  return !event.shiftKey && !onScreenKeyboard();
}

/* An on-screen keyboard is up: the visible part of the page is over 150px shorter than the window (iPadOS
 * Safari keeps the window's height and shrinks only the visual viewport; a hardware keyboard's shortcut bar
 * is shorter). Where the window itself shrinks for it (Android, interactive-widget=resizes-content), a touch
 * device's window over 150px shorter than it has been at this width: a keyboard takes height, never width,
 * while a rotation or a window resized (Stage Manager) changes the width too. Neither the window's sides
 * nor iPadOS's screen size tell a rotation (PR #108 review B1). */
const tallestWindow = new Map(); // window width → the tallest window seen at it

function noteWindowHeight() {
  const width = window.innerWidth;
  tallestWindow.set(width, Math.max(tallestWindow.get(width) || 0, window.innerHeight));
}

function onScreenKeyboard() {
  const view = window.visualViewport;
  if (view && window.innerHeight - view.height * view.scale > 150) return true;
  if (!window.matchMedia("(hover: none) and (pointer: coarse)").matches) return false;
  if (!view) return true;
  return (tallestWindow.get(window.innerWidth) || 0) - window.innerHeight > 150;
}

/* ----------------------------------------------------------------- copy */

/* V3-P79 §5: under a finished answer, Copy hands over its Markdown as written. */
function answerActions(text) {
  const row = el("div", "item-actions");
  const copy = el("button", "copy-button", "Copy");
  copy.type = "button";
  copy.setAttribute("aria-label", "Copy answer");
  copy.addEventListener("click", () => copyText(text, copy));
  row.append(copy);
  return row;
}

/* ``text`` to the clipboard, told on ``button`` for 2 s; the Clipboard API (HTTPS or loopback), else the
 * old copy command on a hidden field. Nothing is read from the clipboard. */
async function copyText(text, button) {
  let copied = false;
  if (!navigator.clipboard) {
    copied = copyByCommand(text); // at once, while the press still counts as one (iOS; review N5)
  } else {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (ignored) {
      copied = copyByCommand(text);
    }
  }
  if (!copied) {
    status("Couldn't copy; select the text instead.", "warn");
    return;
  }
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.textContent = "Copied";
  clearTimeout(button.copiedTimer);
  button.copiedTimer = setTimeout(() => { button.textContent = button.dataset.label; }, 2000);
}

function copyByCommand(text) {
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.className = "copy-field";
  document.body.append(field);
  field.select();
  field.setSelectionRange(0, text.length); // iOS selects nothing of a read-only field with select() alone
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (ignored) {
    copied = false;
  }
  field.remove();
  return copied;
}

/* ------------------------------------------------------------- references */

/* V3-P76 §3.1: where a reference points — the file, and for a selection its lines and columns. */
function referenceLabel(reference) {
  if (reference.whole) return reference.name;
  const [fromLine, fromColumn, toLine, toColumn] = reference.lines;
  return `${reference.name}:${fromLine}:${fromColumn}–${toLine}:${toColumn}`;
}

/* A reference for the next message: a chip above the composer (at most MAX_REFERENCES). The same
 * place again, selected in a file changed since, renews its chip (the old one would be refused). */
function addReference(reference) {
  if (app.conversationId === null) return;
  const same = app.references.find((r) => r.path === reference.path && r.whole === reference.whole
    && r.start === reference.start && r.end === reference.end);
  const unchanged = same && !same.stale && same.sha256 === reference.sha256
    && same.identity.length === reference.identity.length && same.identity.every((v, i) => v === reference.identity[i]);
  if (unchanged) {
    status(`${referenceLabel(reference)} is already in your message.`);
  } else if (same) {
    Object.assign(same, reference, { label: referenceLabel(reference), stale: false });
    status("");
  } else if (app.references.length >= MAX_REFERENCES) {
    status(`A message points at most ${MAX_REFERENCES} places in files.`, "warn");
  } else {
    app.references.push({ ...reference, key: (app.attachKeys += 1), label: referenceLabel(reference) });
    status("");
  }
  renderAttachments();
  composerChanged();
  $("composer-text").focus({ preventScroll: true }); // the page itself never scrolls
}

/* After a reference_stale refusal: the chips whose file changed since they were made say so, as a
 * refused attachment's chip does (the status line gives way to the conversation's own). */
async function markStaleReferences(references, generation) {
  for (const reference of references) {
    try {
      const now = await api("GET", `/api/machine/stat?path=${encodeURIComponent(reference.path)}`);
      reference.stale = now.identity.length !== reference.identity.length
        || now.identity.some((v, i) => v !== reference.identity[i]);
    } catch (ignored) {
      reference.stale = true; // gone, or not to be read now
    }
  }
  if (generation === app.generation) renderAttachments();
}

/* A reference's place in the panel beside the conversation: the file, its selection selected. */
function showReference(path, start = null, end = null) {
  if (app.workingFolder === null) {
    status("This conversation does not work on this server's files.", "warn");
    return;
  }
  if (!app.filesOpen) {
    app.filesOpen = true;
    syncFilesPanel(true);
  }
  filesPanel.reveal(path, start, end);
}

/* §3.1, from the Files surface: the file's folder's conversation opens (as Open conversation does, §6),
 * with the file in its panel and the reference above its composer. */
async function askFromFiles(reference) {
  const folder = reference.path.slice(0, reference.path.lastIndexOf("/")) || "/";
  const start = { path: reference.path, start: reference.whole ? null : reference.start,
                  end: reference.whole ? null : reference.end };
  const id = await openFolderConversation(folder, false, start);
  if (id === null || app.conversationId !== id) return;
  if (panelCoversScreen() && app.filesOpen) toggleFilesPanel(); // the file waits there for the panel
  addReference(reference);
}

/* A sent message's references: each opens its file (as it is now, not as it was sent). */
function sentReferenceChips(references) {
  const list = el("ul", "reference-chips");
  for (const reference of references) {
    const item = el("li");
    const lines = reference.whole ? "" : reference.start_line === reference.end_line
      ? `:${reference.start_line}` : `:${reference.start_line}–${reference.end_line}`;
    const chip = el("button", "reference-chip", `${reference.name}${lines}`);
    chip.type = "button";
    chip.title = reference.path;
    chip.addEventListener("click", () => showReference(reference.path));
    item.append(chip);
    list.append(item);
  }
  return list;
}

/* ------------------------------------------------------------------ files */

function humanSize(n) {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KB`;
  return `${n} ${n === 1 ? "byte" : "bytes"}`;
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
  const link = el("a", className);
  const extension = (file.name.split(".").pop() || "FILE").slice(0, 8).toUpperCase();
  link.append(el("span", "file-type", extension), el("span", "file-name", label),
              el("span", "file-meta", `${humanSize(file.size)} · Download`));
  link.href = fileUrl(file);
  link.setAttribute("download", file.name);
  return link;
}

function fileUrl(file) {
  return `/api/conversations/${encodeURIComponent(app.conversationId)}/files/` +
    `${encodeURIComponent(file.file_id)}/${encodeURIComponent(file.version)}`;
}

const THUMB_HEIGHT = 240; // as .file-thumb-image's max-height

/* An image with a copy (V3-P67 §8): a thumbnail of that copy — what the
 * model saw, made by HIMARI — pressed, it downloads the file as before.
 * Only an <img>: the server sends it inline with its own type, nosniff. */
function fileThumb(file, className) {
  const label = `${file.name || "image"}${file.version > 1 ? ` (v${file.version})` : ""}`;
  const image = el("img", "file-thumb-image");
  image.alt = label;
  image.loading = "lazy";
  image.decoding = "async";
  const preview = file.image.preview;
  if (preview && preview.width > 0 && preview.height > 0) {
    // Its box is kept before it loads, so the timeline stays at the bottom.
    const height = Math.min(THUMB_HEIGHT, preview.height);
    image.width = Math.max(1, Math.round((preview.width * height) / preview.height)); // never a 0 px box
    image.height = height;
  }
  const holder = el("div", "file-thumb");
  const previewButton = el("button", "file-preview-button");
  previewButton.type = "button";
  previewButton.setAttribute("aria-label", `Preview ${label}`);
  previewButton.addEventListener("click", () => {
    $("image-dialog-title").textContent = label;
    $("image-dialog-image").src = image.src;
    $("image-dialog-image").alt = label;
    const download = $("image-dialog-download");
    download.hidden = Boolean(file.expired);
    download.href = fileUrl(file);
    download.setAttribute("download", file.name);
    if (!$("image-dialog").open) $("image-dialog").showModal();
  });
  holder.title = `${label} · ${file.image.width}×${file.image.height} · ` +
    (file.expired ? "the original expired" : humanSize(file.size));
  // Not loaded (the copy expired since the page was read, or the request failed): the plain file instead.
  image.addEventListener("error", () => holder.replaceWith(fileLink(file, className)), { once: true });
  image.src = `${fileUrl(file)}/view`;
  previewButton.append(image);
  holder.append(previewButton, fileLink(file, "file-thumb-download"));
  return holder;
}

function fileItem(file, className) {
  return file.image && file.image.state === "ready" ? fileThumb(file, className) : fileLink(file, className);
}

function fileChips(files) {
  const list = el("ul", "file-chips");
  for (const file of files) {
    const item = el("li");
    item.append(fileItem(file, "file-chip"));
    list.append(item);
  }
  return list;
}

/* The images a tool returned (V3-P67 §8): the model got them too. */
function toolImages(files) {
  const node = el("div", "item item-tool-images");
  node.append(el("span", "item-file-note", files.length === 1 ? "Image from the tool" : "Images from the tool"),
              fileChips(files));
  return node;
}

function presentedCard(file) {
  const card = el("div", "item item-file");
  card.append(el("span", "item-file-note", "HIMARI gave you a file"), fileLink(file, "file-card"));
  return card;
}

/* V3-P71 §7.1: what a memory call changed, outside the work block so it is
   never folded away. Undo is offered while the note is as the call left it;
   the server decides what an Undo puts back, from the call's own record. */
const MEMORY_ACTIONS = { saved: "Remembered", updated: "Updated memory", forgot: "Forgot" };

function memoryLine(event) {
  const line = el("div", "item item-memory");
  line.append(el("span", "item-memory-action", MEMORY_ACTIONS[event.memory.action]),
              el("span", "item-memory-text", event.memory.text));
  if (event.memory.undo) {
    const undo = el("button", "link-button item-memory-undo", "Undo");
    undo.type = "button";
    undo.setAttribute("aria-label", `Undo: ${MEMORY_ACTIONS[event.memory.action]} m${event.memory.id}`);
    undo.addEventListener("click", () => undoMemory(event, undo));
    line.append(undo);
  }
  return line;
}

async function undoMemory(event, button) {
  const path = `/api/conversations/${encodeURIComponent(app.conversationId)}/calls/`
    + `${encodeURIComponent(event.run_id)}/${encodeURIComponent(event.call_id)}/memory-undo`;
  button.disabled = true;
  try {
    await api("POST", path);
    button.replaceWith(el("span", "item-memory-done", "Undone"));
    status(event.memory.action === "saved" ? "HIMARI no longer remembers it." : "The note is back as it was.", "ok");
  } catch (error) {
    if (error.kind === "http" && (error.status === 404 || error.status === 409)) {
      button.remove(); // it cannot be undone any more
      status(error.status === 409 ? "The note was changed since; see Settings › Memory." : error.message, "warn");
    } else {
      button.disabled = false;
      status(error.message, "error");
    }
  }
}

function uploading() {
  return app.attachments.some((a) => a.state === "uploading");
}

/* --------------------------------------------- the composer's place (V3-P78 §3) */

/* What the composer holds, as a draft: the text, the files that finished uploading, the references. */
function composerDraft() {
  return { text: $("composer-text").value,
           attachments: app.attachments.filter((a) => a.state === "ready")
             .map((a) => ({ fileId: a.fileId, name: a.name, size: a.size, uploadedAt: a.uploadedAt })),
           references: app.references.map(({ key, label, ...reference }) => reference) };
}

function fillComposer(draft) {
  $("composer-text").value = draft.text;
  app.promptDismissed = draft.text || null; // a "/…" draft put back opens no picker (§3.1, N6)
  app.attachments = draft.attachments.map((f) => ({ key: (app.attachKeys += 1), name: f.name, size: f.size,
                                                    state: "ready", fileId: f.fileId, uploadedAt: f.uploadedAt,
                                                    progress: 100, xhr: null, note: "" }));
  app.references = draft.references.map((r) => ({ ...r, key: (app.attachKeys += 1), label: referenceLabel(r) }));
  renderAttachments();
  autosize();
  syncLength();
}

/* The composer changed in its place (§3.3): kept once it settles. The conversation being created is
 * kept in memory only, and goes with the composer. */
function composerChanged() {
  syncLength();
  if (app.place === null) return;
  app.placeTouched = true;
  if (typeof app.place === "string") drafts.schedule(app.place, composerDraft);
}

/* Uploads under way stop; the files that finished stay (§3.2). */
function cancelUploads() {
  const going = app.attachments.filter((a) => a.state === "uploading");
  if (!going.length) return;
  for (const entry of going) if (entry.xhr) entry.xhr.abort();
  app.attachments = app.attachments.filter((a) => !going.includes(a));
  renderAttachments();
}

/* The composer leaves its place: with ``keep``, what was changed there is kept for it (the conversation
 * being created gives its part back to where it started from), then the composer is empty. */
function leavePlace({ keep = true } = {}) {
  cancelUploads();
  const place = app.place;
  if (place !== null && keep) {
    if (typeof place === "object") drafts.merge(place.from, composerDraft());
    else if (app.placeTouched) drafts.set(place, composerDraft());
    else drafts.flush();
  } else {
    drafts.cancel();
  }
  app.place = null;
  app.placeTouched = false;
  app.placeEntry += 1;
  $("composer-text").value = "";
  app.attachments = [];
  app.references = [];
  app.jobRunning = false; // another page: its own detail sets it (PR #60 re-review N1)
  renderAttachments();
  autosize();
}

/* The composer comes to ``place`` with its draft, at once from memory (§3.1, review B3); a newer one another
 * tab kept is put in only while nothing was changed here since. */
function enterPlace(place) {
  app.place = place;
  app.placeTouched = false;
  const entry = (app.placeEntry += 1);
  const warned = drafts.sending(place) && !app.inFlight.has(place); // a tab that died mid-send, not this one
  fillComposer(fresh(place, drafts.get(place)));
  if (warned) status("This message may have been sent already; check the conversation before sending it again.", "warn");
  drafts.fresher(place).then((newer) => {
    if (newer && entry === app.placeEntry && !app.placeTouched && app.place === place) fillComposer(fresh(place, newer));
  });
}

/* ``draft`` without chips of uploads too old to send (§3.1, N5), told. */
function fresh(place, draft) {
  const [kept, dropped] = withoutExpired(draft, Date.now());
  if (!dropped.length) return draft;
  drafts.set(place, kept);
  status(`An attached file expired; attach it again: ${dropped.join(", ")}`, "warn");
  return kept;
}

/* §3.1 (review B1): a conversation is being created. With ``carry`` the composer goes into it as it is
 * (the stored draft of where it started stays until the conversation has it, N5); else the place is left
 * with its draft, and the new one starts empty. */
function beginCreating(carry) {
  const now = app.place;
  const from = now === null ? "home" : typeof now === "object" ? now.from : now;
  if (carry) {
    drafts.flush();
    drafts.forget(from);
  } else {
    leavePlace();
  }
  app.place = { creating: true, from, carried: carry };
  app.placeTouched = carry;
  app.placeEntry += 1;
}

/* The conversation was not created (or not opened): what was written for it goes back where it started. */
function endCreating() {
  const place = app.place;
  if (place === null || typeof place !== "object") return; // left meanwhile: leavePlace gave it back
  const typed = composerDraft();
  const shown = place.from === "home" ? !$("surface-home").hidden
    : !$("surface-chat").hidden && app.conversationId === place.from;
  if (!shown) {
    leavePlace();
    return;
  }
  const back = mergeDrafts(drafts.get(place.from), typed);
  app.place = place.from;
  app.placeEntry += 1;
  fillComposer(back);
  composerChanged();
}

function renderAttachments() {
  const list = $("attach-list");
  list.replaceChildren();
  for (const reference of app.references) {
    const item = el("li", `attach-chip is-reference${reference.stale ? " is-failed" : ""}`);
    const open = el("button", "attach-chip-text attach-chip-open",
                    reference.stale ? `${reference.label} · changed, select again` : reference.label);
    open.type = "button";
    open.title = `${reference.path} — show it`;
    open.addEventListener("click", () => showReference(reference.path, reference.whole ? null : reference.start,
                                                       reference.whole ? null : reference.end));
    const remove = el("button", "attach-chip-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${reference.label}`);
    remove.addEventListener("click", () => {
      app.references = app.references.filter((r) => r !== reference);
      renderAttachments();
      composerChanged();
    });
    item.append(open, remove);
    list.append(item);
  }
  for (const entry of app.attachments) {
    const item = el("li", `attach-chip is-${entry.state}`);
    const text = entry.state === "uploading" ? `${entry.name} · ${entry.progress}%`
      : entry.state === "ready" ? `${entry.name} · ${humanSize(entry.size)}` : `${entry.name} · ${entry.note}`;
    item.append(el("span", "attach-chip-text", text));
    if (entry.state === "failed" && entry.file) { // the same file again, in this place (no new pick)
      const retry = el("button", "attach-chip-retry", "Retry");
      retry.type = "button";
      retry.setAttribute("aria-label", `Upload ${entry.name} again`);
      retry.addEventListener("click", () => {
        app.attachments = app.attachments.filter((a) => a !== entry);
        renderAttachments();
        uploadFile(entry.file);
      });
      item.append(retry);
    }
    const remove = el("button", "attach-chip-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${entry.name}`);
    remove.addEventListener("click", () => {
      if (entry.xhr) entry.xhr.abort();
      app.attachments = app.attachments.filter((a) => a !== entry);
      renderAttachments();
      composerChanged();
    });
    item.append(remove);
    list.append(item);
  }
  list.hidden = app.attachments.length === 0 && app.references.length === 0;
  $("composer-send").disabled = app.jobRunning || app.sending || uploading();
}

/* One request per file, with progress. Uploaded files are staged on the
 * server; the next message names them. A result that comes back after the
 * user left the conversation attaches nothing (the stale-answer rule). */
function uploadFile(file) {
  const conversationId = app.conversationId;
  if (conversationId === null) return;
  const generation = app.generation;
  const entry = { key: (app.attachKeys += 1), name: file.name, size: file.size, state: "uploading", fileId: null, file,
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
      Object.assign(entry, { state: "ready", fileId: data.file_id, size: data.size, uploadedAt: Date.now() });
      composerChanged();
      // An image with a copy reaches a model that takes images as an image (V3-P67): no command needed.
      const seen = data.image && data.image.state === "ready" && modelTakesImages();
      if (data.commands === false && !seen && file.type !== "text/plain") { // plain text is read anyway (V3-P80)
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

async function addFiles(files) {
  if (!files.length) return;
  if (app.startingConversation) { // until it opens, the conversation is the one before (V3-P78 §3.1, C1)
    if (!(await app.startingConversation)) return;
  } else if (app.conversationId === null && !$("surface-home").hidden) {
    if (!(await newConversation({ carry: true }))) return;
  }
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
  // Without a result once the turn stopped: it may not have started, its outcome may be
  // unknown, or its result may be kept out of the conversation — the page cannot tell (V3-P68).
  const pending = app.jobRunning ? "running" : "no result";
  const state = tone === "pending" ? pending : tone === "success" ? "done" : "failed";
  entry.details.replaceChildren(el("summary", "tool-row-summary", `${mark} ${entry.toolId} · ${state}`), body);
}

/* ------------------------------------------------------------ work blocks */

/* V3-P68 §5: the tool steps between visible messages fold into one block. Its
   summary is the latest progress line while the turn runs, then "N tool calls";
   the calls and progress lines are inside, in order. What a step gives the user
   (a row with an app view, a presented file, tool images) stays outside, right
   after it. A block never spans the compaction boundary (§5.6). */

function coveredThrough() {
  const context = app.context;
  if (!context || !context.enabled || context.covered_through_sequence === null || !context.summary) return null;
  return context.covered_through_sequence;
}

function newWorkBlock() {
  const block = el("details", "item item-work");
  block.append(el("summary", "work-summary"), el("div", "work-body"));
  block.outsideRows = []; // its calls' rows shown outside (they have an app view), each with data-call
  return block;
}

function workBody(block) {
  return block.querySelector(":scope > .work-body");
}

function lastSequence(block) {
  const last = workBody(block).lastElementChild;
  return Math.max(Number((last || block).dataset.sequence), ...block.outsideRows.map((row) => Number(row.dataset.call)));
}

/* The block a row outside belongs to, or the block a row is in. */
function workOf(row) {
  return row.closest(".item-work")
    || [...$("timeline").querySelectorAll(":scope > .item-work")].find((block) => block.outsideRows.includes(row))
    || null;
}

/* Its latest progress line, or the one of the block it went on from (§5.6). */
function progressLine(block) {
  const notes = block.querySelectorAll(":scope > .work-body > .work-progress");
  return notes.length ? notes[notes.length - 1].dataset.line : block.dataset.line || "";
}

/* The block the steps of this event go into: the open one, or a new one. */
function workBlock(sequence) {
  const covered = coveredThrough();
  let line = "";
  if (app.work && covered !== null && lastSequence(app.work) <= covered && sequence > covered) {
    line = progressLine(app.work); // the block after the boundary goes on with it
    closeWork();
  }
  if (!app.work) {
    app.work = newWorkBlock();
    if (line) app.work.dataset.line = line;
    tagging($("timeline"), sequence).append(app.work);
  }
  return app.work;
}

/* A visible message ends the block; it folds, even if it was opened (§5.4). */
function closeWork() {
  const block = app.work;
  if (!block) return;
  app.work = null;
  block.open = false;
  paintWork(block);
}

function paintWork(block) {
  const body = workBody(block);
  const rows = [...body.querySelectorAll(".item-tool-row"), ...block.outsideRows];
  const calls = rows.length === 1 ? "1 step" : `${rows.length} steps`;
  // The latest model change in it goes on its summary, so a fold never hides one (§5.5).
  const changes = body.querySelectorAll(":scope > .work-model");
  const model = changes.length ? changes[changes.length - 1].textContent : block.dataset.model || "";
  const live = block === app.work && app.jobRunning;
  // Calls without a result once the turn stopped: never "not run" — a call whose outcome is
  // unknown (recovery) or whose result is kept out of the conversation has none either (§5.4).
  const waiting = live ? 0 : rows.filter((row) => row.classList.contains("is-pending")).length;
  const line = live ? progressLine(block) || "Working" : "";
  const key = JSON.stringify([model, live, line, calls, waiting]);
  if (block.painted === key) return; // repainted each poll, the dot's pulse would restart
  block.painted = key;
  const summary = block.querySelector(":scope > .work-summary");
  summary.replaceChildren();
  if (model) summary.append(el("span", "work-model-name", model));
  if (live) {
    summary.append(el("span", "work-dot"), el("span", "work-line", line), el("span", "work-count", ` · ${calls}`));
  } else {
    summary.append(el("span", "work-count", waiting ? `${calls} · ${waiting} ${waiting === 1 ? "result" : "results"} unavailable` : `Activity · ${calls}`));
  }
}

/* §5.6: the part of a block after the compaction boundary (its steps and
   their rows outside) becomes a block of its own, placed before the first
   node after the boundary; it goes on with the progress line. */
function splitWork(block, covered) {
  if (Number(block.dataset.sequence) > covered) return;
  const first = [...workBody(block).children].find((node) => Number(node.dataset.sequence) > covered);
  const outside = block.outsideRows.filter((row) => Number(row.dataset.call) > covered);
  if (!first && !outside.length) return;
  const rest = newWorkBlock();
  const moved = [];
  for (let node = first; node; node = node.nextElementSibling) moved.push(node);
  rest.dataset.sequence = String(Math.min(...moved.map((node) => Number(node.dataset.sequence)),
                                          ...outside.map((row) => Number(row.dataset.call))));
  if (moved.length && moved[0].matches(".work-model")) { // the change is named on its summary, as when live
    const label = moved.shift();
    rest.dataset.model = label.textContent;
    label.remove();
  }
  workBody(rest).append(...moved);
  rest.outsideRows = outside;
  block.outsideRows = block.outsideRows.filter((row) => !outside.includes(row));
  const line = progressLine(block);
  if (line) rest.dataset.line = line;
  let next = block.nextElementSibling;
  while (next && !(Number(next.dataset.sequence) > covered)) next = next.nextElementSibling;
  block.parentElement.insertBefore(rest, next);
  if (app.work === block) app.work = rest;
  block.open = false;
  paintWork(block);
  paintWork(rest);
}

function renderEvent(event) {
  const timeline = tagging($("timeline"), event.sequence);
  if (event.kind === "user_message") {
    closeWork();
    timeline.append(renderMessage(event));
    return;
  }
  if (event.kind === "assistant_final") {
    closeWork();
    timeline.append(renderMessage(event, changedModel(event.model)));
    live.shown(event.run_id); // it replaces what was shown while it was written
    return;
  }
  if (event.kind === "assistant_tool_calls") {
    live.shown(event.run_id);
    const model = changedModel(event.model); // shown on the first thing this step shows
    const question = event.calls.find((call) => call.tool_id === "ask_user");
    if (question) { // always the only call (V3-P15); its text is the question's lead-in (V3-P68 §5.1)
      closeWork();
      if (event.text) timeline.append(renderMessage({ ...event, kind: "assistant_final" }, model));
      const text = typeof question.arguments.question === "string" ? question.arguments.question : "";
      timeline.append(renderMessage({ kind: "assistant_final", text }, event.text ? null : model));
      app.toolRows.set(`${event.run_id}/${question.call_id}`, null); // the answer appears as the user's message
      return;
    }
    const block = workBlock(event.sequence);
    const inside = tagging(workBody(block), event.sequence);
    if (model) {
      if (!workBody(block).childElementCount && !block.outsideRows.length) block.dataset.model = model; // on its summary
      else inside.append(el("div", "work-model", model)); // before this step, inside (§5.5)
    }
    if (event.text && event.text.trim()) {
      const note = el("div", "work-progress");
      // The summary shows its first line as text: "**Plan:**" reads "Plan:".
      const first = event.text.split(/\r?\n/).find((line) => line.trim()) || "";
      note.dataset.line = assistantTextLine(document, first).trim() || first.trim();
      note.append(renderAssistantText(document, event.text));
      inside.append(note);
    }
    for (const call of event.calls) {
      const key = `${event.run_id}/${call.call_id}`; // call ids repeat across Runs
      const node = el("div", "item item-tool-row is-pending");
      const details = el("details", "tool-row-details");
      node.append(details);
      const entry = { node, details, toolId: call.tool_id, args: call.arguments, result: null };
      app.toolRows.set(key, entry);
      paintToolRow(entry);
      if (call.app_view) { // shown while it runs: the row stays outside the block (§5.3)
        views.attach(entry, key, event.run_id, call.call_id, true);
        node.dataset.call = String(event.sequence);
        block.outsideRows.push(node);
        timeline.append(node);
      } else {
        inside.append(node);
      }
    }
    paintWork(block);
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
      if (event.images && event.images.length) timeline.append(toolImages(event.images));
      return;
    }
    const block = workOf(entry.node);
    if (event.app_view && !entry.view) {
      if (block && entry.node.closest(".item-work")) { // its view is known from its result: the row moves out (§5.3)
        entry.node.dataset.call = entry.node.dataset.sequence;
        block.outsideRows.push(entry.node);
        timeline.append(entry.node); // tagged with the result's sequence: where it now stands
      }
      views.attach(entry, `${event.run_id}/${event.call_id}`, event.run_id, event.call_id, false);
    }
    for (const file of event.presented_files || []) timeline.append(presentedCard(file)); // V3-P66 §9
    if (event.memory && MEMORY_ACTIONS[event.memory.action]) timeline.append(memoryLine(event)); // V3-P71 §7.1
    if (event.images && event.images.length) timeline.append(toolImages(event.images)); // V3-P67 §8
    entry.result = event;
    paintToolRow(entry);
    if (entry.view) views.result(entry, Boolean(event.app_view));
    if (block) paintWork(block);
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
  const covered = context.covered_through_sequence;
  for (const block of timeline.querySelectorAll(":scope > .item-work")) splitWork(block, covered);
  const after = [...timeline.children].find((node) => Number(node.dataset.sequence) > covered);
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

/* Whether this conversation's model gets images (V3-P67 §7.1); not known yet: no. */
function modelTakesImages() {
  if (!app.modelChoice) return false;
  return Boolean((app.models.find((m) => m.id === app.modelChoice.model) || {}).images);
}

function renderModel() {
  const choice = app.modelChoice;
  syncModelChrome();
  if (choice === null) return;
  const select = $("chat-menu-model-select");
  const fallback = app.models.find((m) => m.default);
  select.replaceChildren(option("", `Default${fallback ? ` (${fallback.id})` : ""}`));
  for (const model of app.models) select.append(option(model.id, model.id.split("/").pop()));
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

/* The same controls move into a native modal on compact screens: one set of
 * IDs, listeners and server-backed state across resizing and turn updates. */
function syncModelChrome() {
  const compact = window.matchMedia("(max-width: 1099px)").matches;
  const available = !$("surface-chat").hidden && app.modelChoice !== null && app.models.length > 0;
  const controls = $("chat-menu-model");
  const trigger = $("mobile-model-toggle");
  if (!compact || !available) closeModelDialog();
  const parent = compact ? $("model-dialog-controls") : $("topbar");
  if (controls.parentElement !== parent) {
    if (compact) parent.append(controls);
    else parent.insertBefore(controls, trigger);
  }
  controls.hidden = !available;
  trigger.hidden = !compact || !available;
  $("topbar").classList.toggle("has-mobile-model", compact && available);
  if (!available) return;
  const choice = app.modelChoice;
  const names = {
    "openai/gpt-6-luna": "GPT-6 Luna", "openai/gpt-6.1-sol": "GPT-6.1 Sol",
    "anthropic/claude-sonnet-5.5": "Claude Sonnet 5.5",
    "xiaomi/mimo-v2.6-pro": "MiMo v2.6 Pro", "z-ai/glm-5.3": "GLM 5.3",
    "deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash", "minimax/minimax-m3": "MiniMax M3",
  };
  const model = app.models.find((m) => m.id === choice.model);
  const name = names[choice.model] || choice.model.split("/").pop();
  const summary = name + (model && model.efforts.length ? ` · ${choice.effort || "Auto"}` : "");
  $("mobile-model-summary").textContent = summary;
  trigger.title = `Model & reasoning: ${summary}`;
  trigger.setAttribute("aria-label", trigger.title);
}

function closeModelDialog() {
  if ($("model-dialog").open) $("model-dialog").close();
  $("mobile-model-toggle").setAttribute("aria-expanded", "false");
}

function openModelDialog() {
  closeChatMenu();
  closeNav();
  $("model-dialog").showModal();
  $("mobile-model-toggle").setAttribute("aria-expanded", "true");
  $("chat-menu-model-select").focus();
}

let fullscreenPending = false;

function syncFullscreen() {
  const button = $("app-fullscreen");
  const active = document.fullscreenElement === document.documentElement;
  button.hidden = $("surface-login").hidden === false ||
    (!active && (!document.fullscreenEnabled || typeof document.documentElement.requestFullscreen !== "function"));
  button.disabled = fullscreenPending;
  button.setAttribute("aria-pressed", String(active));
  button.title = active ? "Exit fullscreen" : "Enter fullscreen";
  button.setAttribute("aria-label", button.title);
  $("app-fullscreen-icon").setAttribute("href", active ? "#side-contract" : "#side-expand");
}

async function toggleFullscreen() {
  if (fullscreenPending) return;
  fullscreenPending = true;
  syncFullscreen();
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen({ navigationUI: "hide" });
  } catch (error) {
    status("Fullscreen is unavailable in this browser. You can keep chatting here.", "warn");
  } finally {
    fullscreenPending = false;
    syncFullscreen();
  }
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
  } else if ((event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229)
             || event.key === "Tab") { // 229: Safari's Enter that ends a composition (V3-P79 §4.1)
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
    composerChanged();
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
    live.keepLast();
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
  // Why it stopped, short, beside Continue (the status line above has the whole of it).
  const why = view.short || "Stopped"; // the whole reason is in the status line above (review N3)
  $("stop-text").textContent = stopped ? `${why}${used}` : "";
  status(view.tone ? view.text : "", view.tone);
  $("question-panel").hidden = !detail.waiting;
  // As the timeline shows it: formatted, never parsed as HTML (rich_text.js).
  const question = detail.waiting ? detail.waiting.question : null;
  if (question !== app.questionShown) { // a selection, and formulas, stay as they are between polls (review N6)
    app.questionShown = question;
    $("question-text").replaceChildren(...(question === null ? [] : [renderAssistantText(document, question)]));
  }
  $("composer-text").placeholder = detail.waiting ? "Your answer" : "Message HIMARI";
  app.jobRunning = detail.job.running;
  if (detail.job.running && app.streaming && app.conversationId !== null) {
    live.open(`/api/conversations/${encodeURIComponent(app.conversationId)}/live`);
  }
  live.settle(detail.job.running); // its Events are in: a Run without one was stopped (§7.3)
  if (app.work) paintWork(app.work); // its progress line while the turn runs (V3-P68 §5.2)
  if (app.rowsRunning !== detail.job.running) { // a row without a result: "running" or "no result"
    app.rowsRunning = detail.job.running;
    for (const entry of app.toolRows.values()) if (entry && entry.result === null) paintToolRow(entry);
  }
  $("composer-send").disabled = detail.job.running || app.sending || uploading();
  app.workingHint = detail.job.running ? `HIMARI is working…${usageText(detail.job.turn_usage)}` : "";
  paintHint();
  $("chat-menu-resume").hidden = !detail.continuable;
  $("chat-menu-compact").disabled = detail.job.running;
  $("chat-menu-dismiss").hidden = !detail.waiting || detail.job.running;
  const stopping = detail.job.stop_requested;
  $("composer-pause").hidden = !detail.job.running;
  $("composer-pause").disabled = Boolean(stopping);
  $("composer-cancel").hidden = !detail.job.running;
  $("composer-cancel").disabled = stopping === "cancel";
  if (app.running !== detail.job.running) {
    if (app.running && !detail.job.running) { // the turn's changes: listed, then shown in the panel
      const panelShown = app.filesOpen && !$("file-panel").hidden;
      loadChanges(app.generation).then(() => { if (panelShown) filesPanel.refresh(); });
    }
    app.running = detail.job.running;
    filesPanel.workingChanged();
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
        readActivity(); // this one stopped working, or now waits (V3-P80 §3.3)
        loadContext(generation);
        loadServerInfo(); // a command may have found the sandbox broken
        loadToolSources(generation); // and close the views of a source turned off elsewhere
        loadModel(generation); // a stored choice may have been changed elsewhere
        loadFolders(generation); // and folders attached elsewhere
      }
    } catch (error) {
      if (generation !== app.generation || error.status === 401) return;
      if (error.status === 404) { // moved to the trash elsewhere (V3-P65): it is gone from here
        // V3-P78 §3.1 (N7, N3): its text and references go to Home; its uploads were its own.
        const gone = app.conversationId;
        cancelUploads();
        if (app.place === gone) {
          const left = composerDraft();
          drafts.merge("home", { text: left.text, attachments: [], references: left.references });
          app.placeTouched = false;
        }
        drafts.remove(gone);
        await showHome({ via: "replace" }).catch(() => {});
        status("That conversation is no longer here; it may have been moved to the trash.", "warn");
        return;
      }
      status("Lost contact with the server; retrying…", "warn");
      poll(generation);
    }
  }, POLL_MS);
}

/* ``created``: the conversation just made for the composer's place (V3-P78 §3.1): the composer moves into it
 * as it is — not a leave. */
async function openConversation(id, { created = false, via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  app.generation += 1;
  live.reset();
  const moving = created && app.place !== null && typeof app.place === "object" ? app.place : null;
  if (moving === null) leavePlace();
  app.jobRunning = false;
  const generation = app.generation;
  views.reset();
  app.conversationId = id;
  app.lastSequence = 0;
  loadServerInfo();
  app.toolRows = new Map();
  app.work = null;
  app.rowsRunning = null;
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
  showSurface("chat", via);
  if (moving === null) {
    enterPlace(id);
  } else {
    app.place = id;
    app.placeTouched = true;
    app.placeEntry += 1;
    drafts.set(id, composerDraft());
    if (moving.carried) drafts.remove(moving.from); // kept under the conversation now (N5)
  }
  closeNav();
  $("chat-menu-folders").hidden = true;
  app.workingFolder = null;
  filesPanel.folder = null;
  filesPanel.file = null; // the next opening starts at this conversation's working folder
  filesPanel.reset(); // nothing of the last conversation's still on its way is shown
  showChanges(null);
  loadToolSources(generation);
  loadModel(generation);
  loadFolders(generation);
  loadPrompts(generation);
  try {
    const detail = await refresh(generation);
    if (detail && detail.job.running) poll(generation);
    else if (detail) loadContext(generation);
  } catch (error) {
    if (generation !== app.generation) return;
    if (error.status === 404) { // not here (trashed, deleted, or never): Home, and Back does not come back (§3.3)
      // Its text and references go to Home, as the poll's 404 does (V3-P78 §3.1; PR #108 review N1).
      if (app.place === id) {
        const left = composerDraft();
        drafts.merge("home", { text: left.text, attachments: [], references: left.references });
      }
      cancelUploads();
      app.placeTouched = false;
      drafts.remove(id);
      await showHome({ via: "replace" }).catch(() => {});
      status("That conversation is not here; it may have been moved to the trash.", "warn");
      return;
    }
    status(error.message, "error");
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const box = $("composer-text");
  const draft = box.value;
  const text = draft.trim();
  if (app.startingConversation || (app.conversationId === null && !$("surface-home").hidden && text)) {
    // Sent while a conversation is being created (from Home, or right after
    // New chat): it goes to that one once it opens, not to the one before.
    // One send waits; a second Enter meanwhile is the same message.
    if (app.sendWaiting) return;
    app.sendWaiting = true;
    let opened;
    try { opened = await newConversation({ carry: true }); }
    finally { app.sendWaiting = false; }
    if (!opened) return; // it failed, or the user went elsewhere meanwhile
  }
  const ready = app.attachments.filter((a) => a.state === "ready");
  const pointed = app.references.slice();
  if ((!text && !ready.length && !pointed.length) || app.sending || uploading() || app.conversationId === null) return;
  abandonExpansion(); // a message sent now is the user's: no prompt answer may replace what follows
  const generation = app.generation;
  const sentTo = app.conversationId; // V3-P78 §3.1: the draft the sent parts leave, wherever the composer is then
  const sent = { text: draft, fileIds: ready.map((a) => a.fileId), references: pointed };
  if (app.place === sentTo) drafts.markSending(sentTo, composerDraft());
  app.inFlight.add(sentTo);
  app.sending = true;
  $("composer-send").disabled = true;
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(app.conversationId)}/messages`,
      { text, attachments: ready.map((a) => a.fileId),
        references: pointed.map((r) => (r.whole ? { path: r.path, identity: r.identity }
          : { path: r.path, identity: r.identity, start: r.start, end: r.end, sha256: r.sha256 })) });
    drafts.removeSent(sentTo, sent);
    readActivity(); // it works now, or no longer waits for an answer (V3-P80 review N10)
    if (app.place === sentTo) { // the composer is still this conversation's (not by generation: N2)
      // A user can navigate or type again while this request is in flight.
      // Only clear the exact draft accepted for this conversation.
      if (box.value === draft) {
        box.value = "";
        autosize();
      }
      app.jobRunning = true; // a job starts with this message; the next detail says when it ends
      // By what they are, not by object: chips put back by a leave and return are new objects (PR #106 review B2).
      const sentIds = new Set(sent.fileIds);
      const sentKeys = new Set(pointed.map(referenceKey));
      app.attachments = app.attachments.filter((a) => !(a.state === "ready" && sentIds.has(a.fileId)));
      app.references = app.references.filter((r) => !sentKeys.has(referenceKey(r)));
      renderAttachments();
      composerChanged(); // what was typed meanwhile is kept as usual (N1)
      status("");
    }
  } catch (error) {
    if (error.kind !== "network") drafts.clearSending(sentTo); // refused: it was not sent
    if (generation !== app.generation) {
      // Left and back while it was sent (final check N-a): the draft shown is that message's.
      if (error.kind === "network" && app.place === sentTo) {
        status("The message may or may not have been received; check the conversation before sending again.", "warn");
      }
      return;
    }
    if (error.code === "attachment_refused" && error.fileId) { // which chip (V3-P66 §9)
      const refused = app.attachments.find((a) => a.fileId === error.fileId);
      if (refused) Object.assign(refused, { state: "failed", note: `${error.message} — upload it again` });
      renderAttachments();
      status(error.message, "error");
    } else if (error.code === "reference_stale") { // the chips stay, the changed ones marked
      status(error.message, "error");
      await markStaleReferences(pointed, generation);
    } else if (error.kind === "network") {
      status("The message may or may not have been received; check the conversation before sending again.", "warn");
    } else {
      status(error.message, "error");
    }
  } finally {
    app.sending = false;
    app.inFlight.delete(sentTo);
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

async function renameConversation(item = null) {
  closeChatMenu();
  const id = item ? item.id : app.conversationId;
  if (id === null) return;
  const generation = app.generation;
  const dialog = $("rename-dialog");
  if (dialog.open) return;
  $("rename-name").value = item ? conversationName(item) : $("topbar-title").textContent;
  dialog.returnValue = "";
  const title = await new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "save" ? $("rename-name").value.trim() : null), { once: true });
    dialog.showModal();
    $("rename-name").select();
  });
  if (title === null || !title || generation !== app.generation) return;
  try {
    await api("PATCH", `/api/conversations/${encodeURIComponent(id)}`, { title });
    if (generation === app.generation && app.conversationId === id) await refresh(generation);
    await loadList(true);
    markSelected();
    if (!$("surface-home").hidden) await showHome();
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

/* V3-P71 §4.3: the notes a conversation's calls saved go when it is deleted for good. */
function memoryCountLine(count, now = false) {
  const notes = count === 1 ? "1 note HIMARI saved in this conversation is" : `${count} notes HIMARI saved in this conversation are`;
  return now ? `${notes} deleted with it.` : `${notes} deleted when it is deleted for good.`;
}

/* Undo right after a move to the trash: restored; opened again if it was the one open (its draft went with
 * the move), else the list shows it again where the user is (review N1). */
async function undoTrash(id, name, reopen) {
  try {
    await api("POST", `/api/conversations/${encodeURIComponent(id)}/restore`);
  } catch (error) {
    status(error.message, "error");
    return;
  }
  await loadList(true).then(markSelected).catch(() => {});
  if (reopen) await openConversation(id);
  status(`Restored “${name}”.`, "ok");
}

async function deleteConversation(item = null) {
  closeChatMenu();
  const id = item ? item.id : app.conversationId;
  if (id === null) return;
  const name = item ? conversationName(item) : $("topbar-title").textContent;
  const generation = app.generation;
  const still = () => generation === app.generation; // not left for another surface
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
  if (notice.memory_count) lines.push(memoryCountLine(notice.memory_count));
  lines.push(externalLine(notice));
  if (!(await confirmAction(`Delete “${name}”?`, lines, "Move to trash")) || !still()) return;
  $("chat-menu-delete").disabled = true;
  status(notice.running ? "Stopping the work under way, then moving it to the trash…" : "Moving it to the trash…");
  const wasOpen = app.conversationId === id;
  try {
    const moved = await api("POST", `/api/conversations/${encodeURIComponent(id)}/trash`);
    if (app.conversationId === id) cancelUploads(); // moved: its uploads go with it, nothing to ask (PR #106 N4)
    // Moved: a list that fails to load now must not read as a failed move.
    await (app.conversationId === id ? showHome({ via: "replace" }) : !$("surface-home").hidden ? showHome({ via: "none" })
      : loadList(true).then(markSelected)).catch(() => {});
    drafts.remove(id); // after the leave kept it (V3-P78 §3.1)
    status(`Moved “${name}” to the trash. It is deleted for good on ${localDate(moved.purge_after)}.`, "ok",
           ["Undo", () => undoTrash(id, name, wasOpen)]);
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

async function showTrash({ via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace();
  markSelected();
  showSurface("trash", via);
  closeNav();
  $("topbar-title").textContent = "Trash";
  $("trash-hint").textContent = (app.trashDays ? `Conversations here are deleted for good ${app.trashDays} days`
    + " after they were moved. " : "") + "Restore one to read it again.";
  status("");
  await loadTrash(true);
}

/* ------------------------------------------------------------ instructions */

/* V3-P69 §7: the user's instructions for every conversation. A save sends the
   revision the text was edited from; one made elsewhere since is never
   overwritten without the user choosing to, against the version they were
   told about (PR #68 review C1). */

function characters(text) {
  return [...text].length; // code points, as the server counts
}

function instructionsDirty() {
  return app.instructions !== null && $("instructions-text").value !== app.instructions.text;
}

function paintInstructions() {
  const saved = app.instructions;
  const text = $("instructions-text").value;
  const busy = app.savingInstructions;
  const counter = $("instructions-count");
  if (saved === null) { // not loaded (yet): nothing to edit against
    counter.textContent = "Loading…";
    counter.classList.remove("is-over");
  } else {
    const count = characters(text);
    counter.textContent = `${count.toLocaleString()} / ${saved.max_chars.toLocaleString()}`;
    counter.classList.toggle("is-over", count > saved.max_chars);
  }
  const over = saved !== null && characters(text) > saved.max_chars;
  const dirty = instructionsDirty();
  $("instructions-text").readOnly = busy || saved === null;
  $("instructions-save").disabled = saved === null || !dirty || over || busy;
  $("instructions-discard").disabled = busy || (!dirty && app.conflictRevision === null);
  $("instructions-load").disabled = busy;
  $("instructions-overwrite").disabled = busy;
  $("instructions-saved").textContent = !saved || saved.updated_at === null ? ""
    : `Saved ${new Date(saved.updated_at).toLocaleString()}`;
}

/* Called with the panel's buttons enabled (after a save's repaint), so the focus can land (review C2). */
function showConflict(revision, again) {
  app.conflictRevision = revision; // "save mine" replaces this version, the one the user is told about
  const panel = $("instructions-conflict");
  panel.hidden = false;
  status(again ? "Changed again elsewhere; choose again." : "Changed elsewhere since you opened it. Choose below.",
         "warn");
  paintInstructions();
  panel.scrollIntoView({ block: "nearest" });
  $("instructions-load").focus();
}

/* The saved version; a draft kept from a save that met a signed-out session comes back on top (review C2).
   Only the latest load, and only while the surface is shown, is applied: a late answer never replaces
   what was typed since, and the draft is used only when applied (PR #68 re-review C1). */
async function loadInstructions() {
  const ticket = ++app.instructionsTicket;
  const saved = await api("GET", "/api/instructions");
  if (ticket !== app.instructionsTicket || $("surface-instructions").hidden) return;
  app.instructions = saved;
  app.conflictRevision = null;
  $("instructions-conflict").hidden = true;
  const draft = app.instructionsDraft;
  app.instructionsDraft = null;
  $("instructions-text").value = draft ? draft.text : saved.text;
  if (draft && draft.base !== saved.revision) showConflict(saved.revision, false);
  else if (draft) status("Your unsaved instructions are back; save them when ready.", "warn");
  paintInstructions();
}

async function showInstructions({ via = "push" } = {}) {
  if (!$("surface-instructions").hidden && (app.instructions !== null || app.instructionsLoading)) {
    closeNav(); // shown, and loaded or loading: nothing to start again
    return;
  }
  if ($("surface-instructions").hidden) {
    if (!(await mayLeave())) return;
    stopPolling();
    views.reset();
    app.conversationId = null;
    app.generation += 1;
    live.reset();
    leavePlace();
    markSelected();
    app.instructions = null;
    app.conflictRevision = null;
    $("instructions-conflict").hidden = true;
    $("instructions-text").value = "";
    showSurface("instructions", via);
    $("topbar-title").textContent = "Instructions";
    status("");
  }
  closeNav();
  paintInstructions();
  app.instructionsLoading = true;
  try {
    await loadInstructions(); // a failure says so; clicking Instructions again retries (review C3)
  } finally {
    app.instructionsLoading = false;
  }
  if (!$("surface-instructions").hidden && window.matchMedia("(min-width: 768px)").matches) {
    $("instructions-text").focus(); // no keyboard pop on phones
  }
}

/* ``over``: the user chose to replace the version they were shown with theirs. */
async function saveInstructions(over) {
  if (app.savingInstructions || app.instructions === null) return;
  app.savingInstructions = true;
  paintInstructions();
  let finished;
  app.instructionsSave = new Promise((resolve) => { finished = resolve; });
  const text = $("instructions-text").value;
  const base = app.instructions.revision;
  let conflict = null; // {revision, again}: shown once the buttons are enabled again
  try {
    const revision = over ? app.conflictRevision : base;
    const saved = await api("PUT", "/api/instructions", { text, revision });
    app.instructions = saved;
    app.conflictRevision = null;
    $("instructions-text").value = saved.text; // as stored: newlines and blank text normalized
    $("instructions-conflict").hidden = true;
    status("Saved. Applies from the next message.", "ok");
    $("instructions-text").focus(); // the Save button is disabled now: keep the keyboard somewhere useful
  } catch (error) {
    if (error.code === "revision_conflict") {
      try {
        conflict = { revision: (await api("GET", "/api/instructions")).revision, again: over }; // what "mine" replaces
      } catch (reading) {
        if (reading.status === 401) keepDraft(text, base);
        else status("Changed elsewhere since you opened it, and the saved version could not be read.", "error");
      }
    } else if (error.status === 401) {
      keepDraft(text, base);
    } else {
      status(error.message, "error");
    }
  } finally {
    app.savingInstructions = false;
    app.instructionsSave = null;
    finished();
    paintInstructions();
  }
  if (conflict && !$("surface-instructions").hidden) showConflict(conflict.revision, conflict.again);
}

/* Signed out mid-save (e.g. a server restart): the text is kept for after sign-in (review C2, re-review N2). */
function keepDraft(text, base) {
  app.instructionsDraft = { text, base };
  status("Signed out; your instructions are kept. Sign in to continue.", "warn");
}

async function discardInstructions() {
  if (app.conflictRevision !== null) { // what to go back to is the saved version, not the one edited from
    await loadInstructions();
    return;
  }
  $("instructions-text").value = app.instructions ? app.instructions.text : "";
  paintInstructions();
}

/* Leaving the instructions with unsaved changes asks first. An answered "Leave"
   holds until the surface changes, so a navigation that goes on to open a
   conversation does not ask twice, and one that fails keeps the text (N2). */
async function mayLeave() {
  // V3-P78 §3.2 (Q1): an upload under way is cancelled only if the user says so; asked once.
  if (uploading()) {
    if (!(await confirmAction("A file is still uploading", ["Leave and cancel it? Files that finished stay in "
                                                          + "your message."], "Leave"))) return false;
    cancelUploads();
  }
  // The panel's file goes with its conversation; unsaved text there is dropped only if the user says so
  // (V3-P75 §7). The Files surface keeps its own file across surfaces: nothing to ask for it here.
  if (filesPanel.dirty) {
    if (!(await filesPanel.mayLeave())) return false;
    filesPanel.closeDoc();
    filesPanel.file = null;
  }
  const instructions = !$("surface-instructions").hidden;
  if (instructions && app.instructionsSave) await app.instructionsSave; // a save under way ends first (N1)
  // An open memory form with changes asks the same way (PR #77 review C2).
  const dirty = instructions ? instructionsDirty() : memoryDirty();
  if (!dirty || app.leaveAgreed) return true;
  if (app.leaveAsked) return false; // one question at a time; a second navigation waits for none (re-review N4)
  app.leaveAsked = true;
  try {
    const what = instructions ? "Your changes to the instructions are not saved." : "Your changes to a memory are not saved.";
    app.leaveAgreed = await confirmAction("Leave without saving?", [what], "Leave");
  } finally {
    app.leaveAsked = false;
  }
  return app.leaveAgreed;
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
                 "Data stored by external services is not deleted.",
                 // V3-P72 §8: what search already copied into other conversations stays there.
                 "Text that other conversations found with search stays in them."];
  let saved = 0;
  try { // the notes it saved, as the memory screen lists them (V3-P71 §4.3)
    const listing = await api("GET", "/api/memories");
    saved = listing.memories.filter((note) => note.source && note.source.conversation === item.id).length;
  } catch (error) {
    lines.push("Notes HIMARI saved in it, if any, are deleted with it.");
  }
  if (saved) lines.splice(1, 0, memoryCountLine(saved, true));
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

/* ---------------------------------------------------------------- memory */

/* V3-P71 §7.2: the notes HIMARI keeps about the user, by category. An edit
   sends the revision it started from; a note changed elsewhere since is
   shown with the user's text kept in the box, and saving again replaces it. */

const MEMORY_CATEGORIES = [["user", "About you"], ["preference", "Preferences"], ["project", "Projects"],
                           ["reference", "References"]];

function memorySource(note) {
  const meta = el("span", "row-meta memory-source");
  const when = localDate(note.updated_at);
  if (!note.source) {
    meta.textContent = `Added by you · ${when}`;
    return meta;
  }
  if (note.source.in_trash) {
    meta.append("From a conversation in the trash");
  } else {
    const link = el("button", "link-button memory-source-link", `“${conversationName(note.source)}”`);
    link.type = "button";
    link.addEventListener("click", () => openConversation(note.source.conversation));
    meta.append("From ", link);
  }
  meta.append(` · ${note.origin === "model" ? "HIMARI" : "edited by you"} · ${when}`);
  return meta;
}

/* The text as the server keeps it (V3-P71 §4.2), to recognize a save that went through. */
function storedText(text) {
  return text.replace(/\r\n?/g, "\n").trim();
}

/* After a save whose answer was lost (PR #77 review C1): whether the list now
   holds it — true, false, or null while the server cannot be asked. A new note
   counts as saved when a note newer than ``newest`` (the highest id the page
   knew before saving; ids only grow) has its category and text, so a note that
   already said the same is never taken for it (integration review C2); a
   rewrite when the note moved past the revision it was edited from and holds
   the text. */
async function memorySaved(note, revision, newest, body) {
  const listing = await api("GET", "/api/memories").catch(() => null);
  if (!listing) return null;
  const text = storedText(body.text);
  return listing.memories.some((n) => n.category === body.category && n.text === text
                               && (note ? n.id === note.id && n.revision > revision : n.id > newest));
}

function memoryDirty() {
  return !$("surface-memory").hidden
    && [...$("memory-groups").querySelectorAll(".memory-form")].some((form) => form.dirty());
}

/* A form in place of a note (``note``) or for a new one in ``category``. */
function memoryForm(note, category, row) {
  const form = el("form", "memory-form");
  const select = el("select", "memory-form-category");
  select.setAttribute("aria-label", "Category");
  for (const [value, label] of MEMORY_CATEGORIES) select.append(option(value, label));
  select.value = note ? note.category : category;
  const text = el("textarea", "memory-form-text");
  text.rows = 3;
  text.value = note ? note.text : "";
  text.setAttribute("aria-label", note ? `Note m${note.id}` : "New note");
  text.placeholder = "One or two sentences, as: I write Python at work.";
  const conflict = el("p", "memory-form-conflict");
  conflict.hidden = true;
  const save = el("button", "primary-button", "Save");
  save.type = "submit";
  const cancel = el("button", "link-button", "Cancel");
  cancel.type = "button";
  const bar = el("div", "memory-form-bar");
  bar.append(select, cancel, save);
  form.append(text, conflict, bar);
  let revision = note ? note.revision : null;
  let unsure = false; // the last save's answer was lost: look before saving again
  let newest = 0; // the highest note id known when a new note was first sent
  form.dirty = () => text.value !== (note ? note.text : "") || select.value !== (note ? note.category : category);
  cancel.addEventListener("click", () => loadMemory().catch((e) => status(e.message, "error")));
  const saved = async () => {
    // Off, the note is kept but no model reads it (§9; integration review C3).
    status(app.memory && !app.memory.enabled ? "Saved. Memory is off on this server, so models do not use it."
      : "Saved. HIMARI uses it from the next message.", "ok");
    await loadMemory();
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    const body = { category: select.value, text: text.value };
    if (unsure) {
      const done = await memorySaved(note, revision, newest, body);
      if (done) {
        await saved();
        return;
      }
      if (done === null) {
        save.disabled = false;
        status("Still could not reach the server; nothing was saved again.", "warn");
        return;
      }
      unsure = false; // it did not go through: save it now
    }
    if (!note) newest = Math.max(0, ...app.memory.memories.map((n) => n.id));
    try {
      if (note) await api("PUT", `/api/memories/${note.id}`, { ...body, revision });
      else await api("POST", "/api/memories", body);
      await saved();
    } catch (error) {
      save.disabled = false;
      if (error.kind !== "http") { // it may have been saved all the same: never save twice blindly
        const done = await memorySaved(note, revision, newest, body);
        if (done) {
          await saved();
          return;
        }
        unsure = done === null;
        status(unsure ? "Could not confirm the save. Saving again checks first, so it is not saved twice."
          : "The note was not saved. Try again.", "warn");
      } else if (error.status === 409 && note) {
        const latest = await api("GET", "/api/memories").catch(() => null);
        const now = latest && latest.memories.find((n) => n.id === note.id);
        if (!now) {
          status("The note was deleted elsewhere.", "warn");
          await loadMemory();
          return;
        }
        revision = now.revision; // saving again replaces what is shown here
        conflict.textContent = `Changed elsewhere. Saved now: “${now.text}” Save again to replace it with yours.`;
        conflict.hidden = false;
      } else if (error.status === 404) {
        status("The note was deleted elsewhere.", "warn");
        await loadMemory();
      } else {
        status(error.message, "error");
      }
    }
  });
  row.replaceChildren(form);
  text.focus();
}

function memoryRow(note) {
  const li = el("li", "memory-row");
  const main = el("div", "memory-row-main");
  main.append(el("p", "memory-text", note.text), memorySource(note));
  const actions = el("div", "memory-row-actions");
  const edit = el("button", "link-button", "Edit");
  edit.type = "button";
  edit.setAttribute("aria-label", `Edit note m${note.id}`);
  edit.addEventListener("click", () => memoryForm(note, null, li));
  const remove = el("button", "link-button danger-link", "Delete");
  remove.type = "button";
  remove.setAttribute("aria-label", `Delete note m${note.id}`);
  remove.addEventListener("click", () => deleteMemory(note));
  actions.append(edit, remove);
  li.append(main, actions);
  return li;
}

async function deleteMemory(note) {
  if (!(await confirmAction("Delete this memory?", [note.text, "Conversations that already used it still show it."],
                            "Delete"))) return;
  try {
    await api("DELETE", `/api/memories/${note.id}?revision=${note.revision}`);
    status("Deleted. HIMARI no longer uses it.", "ok");
  } catch (error) {
    status(error.status === 409 ? "The note was changed elsewhere; look at it again before deleting." : error.message,
           error.kind === "http" ? "warn" : "error");
  }
  await loadMemory();
}

function paintMemory() {
  const listing = app.memory;
  $("memory-off").hidden = listing.enabled;
  $("memory-count").textContent = `${listing.memories.length.toLocaleString()} of ${listing.max_count.toLocaleString()}`;
  const groups = $("memory-groups");
  groups.replaceChildren();
  for (const [category, label] of MEMORY_CATEGORIES) {
    const notes = listing.memories.filter((note) => note.category === category);
    const group = el("section", "memory-group");
    const head = el("div", "memory-group-head");
    const add = el("button", "link-button", "Add");
    add.type = "button";
    add.setAttribute("aria-label", `Add to ${label}`);
    const list = el("ul", "rows memory-list");
    add.addEventListener("click", () => {
      const row = el("li", "memory-row");
      list.prepend(row);
      memoryForm(null, category, row);
    });
    head.append(el("h2", "memory-group-title", label), add);
    for (const note of notes) list.append(memoryRow(note));
    group.append(head, list);
    if (!notes.length) group.append(el("p", "hint memory-group-empty", "Nothing yet."));
    groups.append(group);
  }
}

async function loadMemory() {
  const generation = app.generation;
  const listing = await api("GET", "/api/memories");
  if (generation !== app.generation || $("surface-memory").hidden) return;
  app.memory = listing;
  paintMemory(); // closes any open form
}

async function showMemory({ via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace();
  markSelected();
  app.memory = null;
  $("memory-groups").replaceChildren();
  $("memory-count").textContent = "Loading…";
  showSurface("memory", via);
  closeNav();
  $("topbar-title").textContent = "Memory";
  status("");
  await loadMemory();
}

/* --------------------------------------------------------------- folders */

/* V3-P74 §10.3: folders on this server. A conversation that works on them
   lists its own folder first, then the attached ones; the checked one is the
   working directory. Changes apply from the model's next step. */

function folderPath(item) {
  return item.path || "(the runner does not answer)";
}

function renderFolders(items) {
  const working = items.find((item) => item.working && item.path);
  const before = app.workingFolder;
  app.workingFolder = working ? working.path : null;
  app.conversationFolderPaths = items.map((item) => item.path).filter(Boolean);
  syncFilesPanel(!$("surface-chat").hidden);
  if (app.filesOpen && app.workingFolder && app.workingFolder !== before) openPanelStart();
  if (app.workingFolder !== null && before === null) loadChanges(app.generation);
  const list = $("chat-menu-folders-list");
  list.replaceChildren();
  $("chat-menu-folders").hidden = items.length === 0;
  for (const item of items) {
    const row = el("li", "chat-menu-tool chat-menu-folder");
    const id = `folder-${item.binding_id}`;
    const radio = el("input");
    radio.type = "radio";
    radio.name = "chat-menu-working-folder";
    radio.id = id;
    radio.checked = item.working;
    radio.title = "Working directory";
    radio.addEventListener("change", () => setWorkingFolder(item.binding_id));
    const label = el("label", "chat-menu-tool-name", item.own ? "This conversation" : item.label);
    label.htmlFor = id;
    label.append(el("span", "chat-menu-tool-meta", folderPath(item) + (item.working ? " · working directory" : "")));
    row.append(radio, label);
    if (!item.own) {
      const detach = el("button", "link-button", "Detach");
      detach.type = "button";
      detach.setAttribute("aria-label", `Detach ${item.label}`);
      detach.addEventListener("click", () => detachFolder(item));
      row.append(detach);
    }
    list.append(row);
  }
}

function conversationFolders() {
  return `/api/conversations/${encodeURIComponent(app.conversationId)}/workspaces`;
}

async function loadFolders(generation) {
  try {
    const answer = await api("GET", conversationFolders());
    if (generation !== app.generation) return;
    renderFolders(answer.items);
    if (!answer.items.length) return;
    const known = await api("GET", "/api/workspaces");
    if (generation !== app.generation) return;
    app.registeredFolders = known.items;
    $("chat-menu-attach-known").replaceChildren(...known.items.map((w) => option(w.path, w.label)));
  } catch (ignored) {
    /* display only: the conversation works without it */
  }
}

async function changeFolders(method, path, body, done) {
  const generation = app.generation;
  try {
    const answer = await api(method, path, body);
    if (generation !== app.generation) return;
    renderFolders(answer.items);
    if (done) status(done, "ok");
  } catch (error) {
    if (generation === app.generation) {
      status(error.message, "error");
      loadFolders(generation);
    }
  }
}

async function attachFolder(event) {
  event.preventDefault();
  const given = $("chat-menu-attach-path").value.trim();
  if (!given) return;
  const known = (app.registeredFolders || []).find((w) => w.path === given || w.id === given);
  await changeFolders("POST", conversationFolders(), known ? { workspace_id: known.id } : { path: given },
                      "Attached. HIMARI can write there from its next step.");
  $("chat-menu-attach-path").value = "";
  loadFolders(app.generation); // the registered list may have grown
}

function setWorkingFolder(bindingId) {
  return changeFolders("PUT", `${conversationFolders()}/working`, { binding_id: bindingId }, null);
}

async function detachFolder(item) {
  await changeFolders("DELETE", `${conversationFolders()}/${encodeURIComponent(item.binding_id)}`, undefined,
                      `Detached ${item.label}. The folder itself is unchanged.`);
}

/* The settings list: register, rename, note, unregister. */

function folderRow(workspace) {
  const li = el("li", "memory-row");
  const main = el("div", "memory-row-main");
  main.append(el("p", "memory-text", workspace.label), el("span", "row-meta", workspace.path));
  if (workspace.note) main.append(el("span", "row-meta folders-note", workspace.note));
  if (workspace.cloud_synced) {
    main.append(el("span", "row-meta", "Cloud-synced: the model is told files have no execute bit and sync can lag"));
  }
  if (workspace.attached) {
    main.append(el("span", "row-meta",
                   `Attached to ${workspace.attached} conversation${workspace.attached === 1 ? "" : "s"}`));
  }
  const actions = el("div", "memory-row-actions");
  const edit = el("button", "link-button", "Edit");
  edit.type = "button";
  edit.setAttribute("aria-label", `Edit ${workspace.label}`);
  edit.addEventListener("click", () => folderForm(workspace, li));
  const remove = el("button", "link-button danger-link", "Unregister");
  remove.type = "button";
  remove.setAttribute("aria-label", `Unregister ${workspace.label}`);
  remove.addEventListener("click", () => unregisterFolder(workspace));
  actions.append(edit, remove);
  li.append(main, actions);
  return li;
}

function folderForm(workspace, li) {
  const form = el("form", "memory-form");
  const label = el("input", "memory-form-text");
  label.value = workspace.label;
  label.maxLength = 100;
  label.setAttribute("aria-label", "Name");
  const note = el("textarea", "memory-form-text");
  note.rows = 3;
  note.maxLength = 1000;
  note.value = workspace.note || "";
  note.setAttribute("aria-label", "Note HIMARI sees");
  const bar = el("div", "memory-form-bar");
  const cancel = el("button", "link-button", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => loadFoldersList());
  const save = el("button", "primary-button", "Save");
  save.type = "submit";
  bar.append(cancel, save);
  form.append(label, note, bar);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("PATCH", `/api/workspaces/${encodeURIComponent(workspace.id)}`,
                { label: label.value, note: note.value.trim() ? note.value : null });
      status("Saved. HIMARI sees it from the next message.", "ok");
      await loadFoldersList();
    } catch (error) {
      status(error.message, "error");
    }
  });
  li.replaceChildren(form);
  label.focus();
}

async function unregisterFolder(workspace) {
  if (!(await confirmAction(`Unregister “${workspace.label}”?`,
                            [workspace.path, "The folder and its files are not changed."], "Unregister"))) return;
  try {
    await api("DELETE", `/api/workspaces/${encodeURIComponent(workspace.id)}`);
    status("Unregistered. The folder itself is unchanged.", "ok");
  } catch (error) {
    status(error.message, error.kind === "http" ? "warn" : "error");
  }
  await loadFoldersList();
}

async function loadFoldersList() {
  const generation = app.generation;
  const listing = await api("GET", "/api/workspaces");
  if (generation !== app.generation || $("surface-folders").hidden) return;
  $("folders-off").hidden = listing.available;
  $("folders-add").hidden = !listing.available;
  $("folders-empty").hidden = !listing.available || listing.items.length > 0;
  $("folders-list").replaceChildren(...listing.items.map(folderRow));
}

async function registerFolder(event) {
  event.preventDefault();
  const path = $("folders-add-path").value.trim();
  if (!path) return;
  const label = $("folders-add-label").value.trim();
  const note = $("folders-add-note").value.trim();
  try {
    await api("POST", "/api/workspaces", { path, label: label || null, note: note || null });
    $("folders-add").reset();
    status("Registered. Attach it from a conversation's menu.", "ok");
  } catch (error) {
    status(error.message, "error");
  }
  await loadFoldersList();
}

/* ------------------------------------------------------------------ files */

/* V3-P75 §3.2: the Files surface — this server's files without a conversation. */
async function showFiles({ via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace();
  markSelected();
  showSurface("files", via);
  closeNav();
  $("topbar-title").textContent = "Files";
  status("");
  if (filesSurface.place) await filesSurface.refresh();
  else await filesSurface.openFolder("~");
}

/* A conversation opened from a folder: its files beside it (V3-P75 §6; PR #93 review B2), on ``start``
 * ({path, start, end}: a file, its selection selected) instead of the working folder when given. */
function openWithFiles(id, start = null) {
  app.filesOpen = true;
  app.panelStart = start ? { ...start, id } : null;
  return openConversation(id);
}

/* §6: a folder's conversation — its latest, or a new one working in it. Its id once opened, else null. */
async function openFolderConversation(path, isNew, start = null) {
  try {
    const answer = await api("POST", "/api/machine/conversations", { path, new: isNew });
    await openWithFiles(answer.conversation.id, start);
    if (answer.created) loadList(true).then(markSelected).catch(() => {});
    return answer.conversation.id;
  } catch (error) {
    status(error.message, "error");
    return null;
  }
}

/* The surface's folder control: "Open conversation", with the folder's list behind ⌄ (§6.1). */
function openHereControl(path) {
  const box = el("div", "files-open-chat");
  const main = el("button", "primary-button files-open-chat-main", "Open conversation");
  main.type = "button";
  main.addEventListener("click", async () => {
    main.disabled = true; // one request at a time from here (PR #93 review B1)
    try {
      await openFolderConversation(path, false);
    } finally {
      main.disabled = false;
    }
  });
  const more = el("details", "files-open-chat-more");
  const toggle = el("summary", "files-open-chat-toggle", "⌄");
  toggle.setAttribute("aria-label", "Conversations in this folder");
  toggle.title = "Conversations in this folder";
  const list = el("div", "conversation-action-list files-open-chat-list");
  more.append(toggle, list);
  box.append(main, more);
  const ticket = ++app.folderTicket;
  api("GET", `/api/machine/conversations?path=${encodeURIComponent(path)}`).then((folder) => {
    if (ticket !== app.folderTicket) return;
    if (folder.refused) {
      main.disabled = true;
      main.title = folder.refused;
      more.hidden = true;
      return;
    }
    if (!folder.conversations.length) main.textContent = "New conversation here";
    for (const item of folder.conversations) {
      const row = el("button", "link-button files-open-chat-item");
      row.type = "button";
      row.append(el("span", "files-name", conversationName(item)),
                 el("span", "files-meta", new Date(item.last_activity_at).toLocaleDateString()));
      row.addEventListener("click", () => { more.open = false; openWithFiles(item.id); });
      list.append(row);
    }
    if (!folder.own) {
      const fresh = el("button", "link-button", "New conversation here");
      fresh.type = "button";
      fresh.addEventListener("click", () => { more.open = false; openFolderConversation(path, true); });
      list.append(fresh);
    }
    for (const above of folder.above) { // Q2: the folders above with conversations, as links
      const link = el("button", "link-button files-open-chat-above",
                      `${above.count} in ${above.workspace.label} (above)`);
      link.type = "button";
      link.title = above.workspace.path;
      link.addEventListener("click", () => { more.open = false; filesSurface.openFolder(above.workspace.path); });
      list.append(link);
    }
  }).catch(() => { /* the button still opens or makes one; the server says why if it cannot */ });
  return box;
}

/* Beside a conversation: a folder not yet among its folders can be attached (§6.3). */
function attachable(path) {
  return app.conversationId !== null && !app.conversationFolderPaths.includes(path);
}

function attachHere(path) {
  return changeFolders("POST", conversationFolders(), { path },
                       "Attached. HIMARI can write there from its next step, and works in it.")
    .then(() => filesPanel.refresh());
}

function attachHereButton(path) {
  const button = el("button", "link-button", "Attach to this conversation");
  button.type = "button";
  button.title = "HIMARI may then write here, and works in this folder";
  button.addEventListener("click", () => attachHere(path));
  return button;
}

/* -------------------------------------------- HIMARI's changes (V3-P76 §5) */

function conversationChanges(of, tail = "") {
  return `/api/conversations/${encodeURIComponent(of)}/changes${tail}`;
}

/* The count in the panel's head (and a dot on its toggle while it is closed), and the rows' dots. */
function showChanges(list) {
  if (list !== null && list.id !== app.conversationId) return;
  const count = list === null ? 0 : list.items.length + list.hidden;
  const button = $("file-panel-changes");
  button.hidden = count === 0;
  button.textContent = `Changes ${count}`;
  const stuck = list !== null && list.items.some((item) => !item.revertible);
  button.classList.toggle("is-warn", stuck);
  button.title = stuck ? "Files HIMARI changed, to review: some cannot be reverted" : "Files HIMARI changed, to review";
  $("chat-files-toggle").classList.toggle("has-changes", count > 0);
  filesPanel.markChanged(list);
}

/* Read again at a conversation's opening and at the end of each turn (display only). */
async function loadChanges(generation) {
  if (app.workingFolder === null) {
    showChanges(null);
    return;
  }
  try {
    const list = await filesPanel.changes.list(app.conversationId);
    if (generation === app.generation) showChanges(list);
  } catch (ignored) {
    /* the conversation works without it */
  }
}

/* The panel beside a conversation that works on this server's folders. */
const PANEL_WIDTH_KEY = "himari.filePanelWidth";

function syncFilesPanel(onChat) {
  const offered = onChat && app.workingFolder !== null;
  $("chat-files-toggle").hidden = !offered;
  $("chat-files-toggle").setAttribute("aria-expanded", String(offered && app.filesOpen));
  $("file-panel").hidden = !(offered && app.filesOpen);
  $("layout").classList.toggle("has-file-panel", offered && app.filesOpen);
  if (!(offered && app.filesOpen)) setPanelExpanded(false);
}

function toggleFilesPanel() {
  app.filesOpen = !app.filesOpen;
  syncFilesPanel(true);
  if (app.filesOpen && app.workingFolder) {
    // Its place again, or its first place the first time.
    if (filesPanel.place) filesPanel.refresh();
    else openPanelStart();
  }
}

/* The panel's first place in this conversation: the file Ask HIMARI came from (V3-P76 §3.1), its
 * selection selected, else the conversation's working folder. */
function openPanelStart() {
  const start = app.panelStart !== null && app.panelStart.id === app.conversationId ? app.panelStart : null;
  app.panelStart = null;
  if (start) filesPanel.reveal(start.path, start.start, start.end);
  else filesPanel.openFolder(app.workingFolder);
}

/* Too narrow for the panel beside the conversation: it covers the screen (app.css). */
function panelCoversScreen() {
  return window.matchMedia("(max-width: 899px)").matches;
}

function setPanelExpanded(expanded) {
  $("layout").classList.toggle("is-files-expanded", expanded);
  $("file-panel-expand").setAttribute("aria-pressed", String(expanded));
  $("file-panel-expand").setAttribute("aria-label", expanded ? "Show the conversation" : "Expand files");
  $("file-panel-expand").title = expanded ? "Show the conversation" : "Expand files";
  $("file-panel-expand-icon").setAttribute("href", expanded ? "#side-contract" : "#side-expand");
}

function panelWidth(fraction) {
  const clamped = Math.min(0.7, Math.max(0.3, fraction));
  $("layout").style.setProperty("--file-panel-width", `${(clamped * 100).toFixed(1)}%`);
  return clamped;
}

function restorePanelWidth() {
  try {
    const saved = Number(localStorage.getItem(PANEL_WIDTH_KEY));
    if (saved) panelWidth(saved);
  } catch (ignored) { /* the default width */ }
}

function dragPanel(event) {
  if (event.button !== 0) return;
  event.preventDefault();
  const grip = $("file-panel-grip");
  grip.setPointerCapture(event.pointerId);
  const box = $("layout").getBoundingClientRect();
  let fraction = null;
  const move = (e) => { fraction = panelWidth((box.right - e.clientX) / box.width); };
  const end = () => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", end);
    grip.removeEventListener("pointercancel", end);
    try { if (fraction !== null) localStorage.setItem(PANEL_WIDTH_KEY, String(fraction)); } catch (ignored) { /* kept for this page */ }
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

function nudgePanel(event) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  const box = $("layout").getBoundingClientRect();
  const now = $("file-panel").getBoundingClientRect().width / box.width;
  const fraction = panelWidth(now + (event.key === "ArrowLeft" ? 0.05 : -0.05));
  try { localStorage.setItem(PANEL_WIDTH_KEY, String(fraction)); } catch (ignored) { /* kept for this page */ }
}

async function showFolders({ via = "push" } = {}) {
  if (!(await mayLeave())) return;
  stopPolling();
  views.reset();
  app.conversationId = null;
  app.generation += 1;
  live.reset();
  leavePlace();
  markSelected();
  $("folders-list").replaceChildren();
  showSurface("folders", via);
  closeNav();
  $("topbar-title").textContent = "Folders";
  status("");
  await loadFoldersList();
}

/* The Settings link only where folders are offered. */
async function syncFoldersLink() {
  try {
    const offered = (await api("GET", "/api/workspaces")).available;
    $("nav-folders").hidden = !offered;
    $("nav-files").hidden = !offered; // the Files surface too (V3-P75 §3.2)
  } catch (ignored) {
    $("nav-folders").hidden = true;
    $("nav-files").hidden = true;
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
  const wasOpen = $("layout").classList.contains("is-nav-open");
  $("layout").classList.remove("is-nav-open");
  $("nav-backdrop").hidden = true;
  syncNavigation();
  if (wasOpen) $("sidebar-toggle").focus();
}

function syncNavigation() {
  const desktop = window.matchMedia("(min-width: 1100px)").matches;
  const layout = $("layout");
  const open = !$("sidebar").hidden && (desktop ? !layout.classList.contains("is-nav-collapsed") : layout.classList.contains("is-nav-open"));
  $("sidebar-toggle").setAttribute("aria-expanded", String(open));
  $("sidebar-toggle").setAttribute("aria-label", open ? "Collapse navigation" : "Open navigation");
  $("sidebar-toggle").title = open ? "Collapse navigation" : "Open navigation";
}

function toggleNav() {
  const layout = $("layout");
  if (window.matchMedia("(min-width: 1100px)").matches) {
    layout.classList.toggle("is-nav-collapsed");
    syncNavigation();
    if (layout.classList.contains("is-nav-collapsed")) $("sidebar-toggle").focus();
    else $("sidebar-close").focus();
    return;
  }
  const open = !layout.classList.contains("is-nav-open");
  layout.classList.toggle("is-nav-open", open);
  $("nav-backdrop").hidden = !open;
  syncNavigation();
  if (!open) $("sidebar-toggle").focus();
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
    app.streaming = server.streaming === true; // V3-P73 §9
    renderModel();
    info.classList.toggle("is-warn", server.sandbox !== "enforced");
    $("server-details").classList.toggle("is-warn", server.sandbox !== "enforced");
    $("server-status-label").textContent = server.sandbox === "enforced" ? "Connected" : "Limited tools";
    info.hidden = false;
    $("server-details").hidden = $("sidebar").hidden;
  } catch (error) {
    info.hidden = true; // display only
    $("server-details").hidden = true;
  }
}

async function enter() {
  localStore.openWrites(); // V3-P78 §5.3: closed by a sign-out until now
  await drafts.load();
  app.activityAsked.clear();
  scheduleActivity(0); // V3-P80 §3.3
  loadServerInfo();
  syncFoldersLink();
  // A save that met a signed-out session goes back to its draft (PR #68 review C2).
  if (!app.instructionsDraft) {
    // The page's address, or the one signed in at (V3-P79 §3.3); the list as Home would show it (review N4).
    if (location.pathname !== "/") loadList(true).then(markSelected).catch(() => {});
    await goTo(location.pathname, "replace");
    return;
  }
  loadList(true).then(markSelected).catch(() => {}); // as Home would (re-review N3)
  await showInstructions({ via: "replace" }); // as the other way in (PR #108 review N4)
}

async function boot() {
  // The thinking line follows the theme at once (V3-P73 §7.4).
  new MutationObserver(() => live.restyle()).observe(document.documentElement,
    { attributes: true, attributeFilter: ["data-theme"] });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => live.restyle());
  $("login-form").addEventListener("submit", signIn);
  $("nav-sign-out").addEventListener("click", () => { $("appearance-dialog").close(); signOut(); });
  $("nav-trash").addEventListener("click", () => showTrash().catch((e) => status(e.message, "error")));
  $("nav-memory").addEventListener("click", () => {
    $("appearance-dialog").close();
    showMemory().catch((e) => status(e.message, "error"));
  });
  $("nav-folders").addEventListener("click", () => {
    $("appearance-dialog").close();
    showFolders().catch((e) => status(e.message, "error"));
  });
  $("folders-add").addEventListener("submit", registerFolder);
  $("chat-menu-attach").addEventListener("submit", attachFolder);
  $("nav-instructions").addEventListener("click", () => {
    $("appearance-dialog").close();
    showInstructions().catch((e) => status(e.message, "error"));
  });
  $("instructions-text").addEventListener("input", () => {
    app.leaveAgreed = false; // edited again: leaving asks again
    paintInstructions();
  });
  $("instructions-save").addEventListener("click", () => saveInstructions(false));
  $("instructions-overwrite").addEventListener("click", () => saveInstructions(true));
  $("instructions-discard").addEventListener("click", () => discardInstructions().catch((e) => status(e.message, "error")));
  $("instructions-load").addEventListener("click", () => loadInstructions().catch((e) => status(e.message, "error")));
  // V3-P78 §3.3: what waits to be kept goes now (Safari may not finish it; the short delay is the guarantee).
  const keepNow = () => {
    if (app.placeTouched && typeof app.place === "string") drafts.set(app.place, composerDraft());
    for (const pane of [filesPanel, filesSurface]) {
      pane.keepNow();
      pane.keepPlace(); // a PDF's page and zoom, as the page goes (web polish 2, B3)
    }
  };
  window.addEventListener("pagehide", keepNow);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") keepNow();
    else if ($("surface-login").hidden) readActivity(); // shown again: what changed meanwhile (V3-P80 §3.3)
  });
  // §5.3: another tab signed out — nothing of this one is kept or written either.
  localStore.onWiped(() => { // signed out like that tab: to sign in again, where writes open again (PR #106 review B3)
    drafts.clear();
    filesSurface.closeForSignIn({ keepText: false }); // also when the sign-in screen shows already (re-review N3)
    showLogin({ signingOut: true });
  });
  window.addEventListener("beforeunload", (event) => {
    const unsaved = app.instructionsDraft !== null || (!$("surface-instructions").hidden && instructionsDirty())
      || memoryDirty() || filesSurface.dirty || filesPanel.dirty;
    if (!unsaved) return;
    event.preventDefault(); // unsaved: the browser asks
    event.returnValue = ""; // older Chrome and some webviews ask only with this
  });
  $("trash-load-more").addEventListener("click", () => loadTrash(false).catch((e) => status(e.message, "error")));
  $("chat-menu-delete").addEventListener("click", () => deleteConversation());
  $("nav-home").addEventListener("click", () => showHome().catch((e) => status(e.message, "error")));
  $("nav-new-chat").addEventListener("click", newConversation);
  $("nav-new-chat").title = `New chat (${newChatShortcut()})`;
  document.addEventListener("keydown", shortcut);
  $("files-surface-slot").append(filesSurface.root);
  $("file-panel-slot").append(filesPanel.root);
  restorePanelWidth();
  $("nav-files").addEventListener("click", () => showFiles().catch((e) => status(e.message, "error")));
  $("chat-files-toggle").addEventListener("click", toggleFilesPanel);
  $("file-panel-close").addEventListener("click", toggleFilesPanel);
  $("file-panel-changes").addEventListener("click", () => filesPanel.openChanges());
  $("file-panel-expand").addEventListener("click",
    () => setPanelExpanded(!$("layout").classList.contains("is-files-expanded")));
  $("file-panel-grip").addEventListener("pointerdown", dragPanel);
  $("file-panel-grip").addEventListener("keydown", nudgePanel);
  $("home-new-chat").addEventListener("click", newConversation);
  $("conversation-load-more").addEventListener("click", () => loadList(false).then(markSelected));
  $("composer").addEventListener("submit", sendMessage);
  $("composer-attach").addEventListener("click", () => $("composer-files").click());
  $("composer-files").addEventListener("change", (event) => {
    addFiles(Array.from(event.target.files || []));
    event.target.value = ""; // the same file may be chosen again
  });
  for (const dropZone of [$("surface-chat"), $("surface-home")]) {
  dropZone.addEventListener("dragover", (event) => {
    if (!event.dataTransfer || !event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dropZone.classList.add("is-dropping");
  });
  dropZone.addEventListener("dragleave", (event) => {
    if (event.target === dropZone) dropZone.classList.remove("is-dropping");
  });
  dropZone.addEventListener("drop", (event) => {
    dropZone.classList.remove("is-dropping");
    if (!event.dataTransfer || !event.dataTransfer.files.length) return;
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  });
  }
  $("composer-text").addEventListener("input", () => {
    autosize();
    updatePicker();
    composerChanged();
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
  noteWindowHeight();
  window.addEventListener("resize", noteWindowHeight);
  window.addEventListener("popstate", addressPopped);
  $("composer-text").addEventListener("paste", pasteIntoComposer);
  $("timeline").addEventListener("scroll", syncJump, { passive: true });
  new MutationObserver(syncJump).observe($("timeline"), { childList: true, subtree: true });
  $("jump-latest").addEventListener("click", () => {
    const timeline = $("timeline");
    timeline.scrollTo({ top: timeline.scrollHeight, behavior: "smooth" });
  });
  new MutationObserver(followTitle).observe($("topbar-title"), { childList: true, characterData: true, subtree: true });
  $("composer-text").addEventListener("keydown", (event) => {
    if (pickerKey(event)) return;
    if (event.key === "Escape") abandonExpansion(); // like Cancel: a pending prompt will not fill the box
    if (enterSends(event)) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("chat-menu-toggle").addEventListener("click", toggleChatMenu);
  $("mobile-model-toggle").addEventListener("click", openModelDialog);
  $("model-dialog-close").addEventListener("click", closeModelDialog);
  $("model-dialog-done").addEventListener("click", closeModelDialog);
  $("model-dialog").addEventListener("close", () => {
    $("mobile-model-toggle").setAttribute("aria-expanded", "false");
    if (!$("mobile-model-toggle").hidden) $("mobile-model-toggle").focus();
  });
  $("model-dialog").addEventListener("click", (event) => {
    if (event.target !== $("model-dialog")) return;
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModelDialog();
  });
  $("app-fullscreen").addEventListener("click", toggleFullscreen);
  document.addEventListener("fullscreenchange", syncFullscreen);
  $("chat-menu-rename").addEventListener("click", () => renameConversation());
  $("image-dialog-close").addEventListener("click", () => $("image-dialog").close());
  $("image-dialog").addEventListener("close", () => $("image-dialog-image").removeAttribute("src"));
  document.addEventListener("click", (event) => {
    for (const menu of $("conversation-list").querySelectorAll("details[open]")) {
      if (!menu.contains(event.target)) menu.open = false;
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeChatMenu();
    for (const menu of $("conversation-list").querySelectorAll("details[open]")) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  });
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
  $("sidebar-close").addEventListener("click", toggleNav);
  $("nav-backdrop").addEventListener("click", closeNav);
  window.matchMedia("(min-width: 1100px)").addEventListener("change", () => {
    closeNav();
    $("server-details").open = false;
    syncModelChrome();
  });
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
