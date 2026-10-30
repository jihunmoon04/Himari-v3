/* Focused async UI regression checks. No browser, packages, or live service.
 * Usage: node scripts/ui_state_smoke.mjs */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../src/himari/interfaces/web/static/app.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/gm, "");
// The drafts' table as it is (no DOM), over a store that keeps nothing (V3-P78).
const draftsSource = readFileSync(new URL("../src/himari/interfaces/web/static/drafts.js", import.meta.url), "utf8")
  .replace(/^export /gm, "");
const localStore = { WRITER: "w", entries: async () => [], get: async () => null, put: async () => true,
                     remove: async () => true, prune: async () => {}, count: async () => 0, wipe: async () => {},
                     openWrites() {}, closeWrites() {}, onWiped() {} };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/* Enough of an element for the parts app.js builds itself (the folder's conversation control). */
class FakeElement {
  constructor(tag) { Object.assign(this, { tagName: tag, children: [], listeners: {}, textContent: "", className: "", disabled: false, hidden: false, open: false, title: "" }); }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute() {}
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { return Promise.all((this.listeners.click || []).map((fn) => fn())); }
  all() { return [this, ...this.children.flatMap((c) => (c.all ? c.all() : []))]; }
}

function page() {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, { value: "", disabled: false, hidden: true, focus() {}, close() {}, dataset: {},
                                       classList: { toggle() {}, add() {}, remove() {} } });
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: node, addEventListener() {}, createElement: (tag) => new FakeElement(tag) },
    AppViews: class { reset() {} }, FilesPane: class { refresh() {} openFolder() {} }, console, setTimeout, clearTimeout,
    LiveAnswer: class { reset() {} shown() {} keepLast() {} settle() {} open() {} restyle() {} },
    localStore, rememberLab() {}, location: { search: "" },
  });
  const run = (code) => vm.runInContext(code, context);
  run(draftsSource);
  run(source);
  run("status = () => {}; abandonExpansion = () => {}; autosize = () => {}; renderAttachments = () => {}; refresh = async () => {}; poll = () => {};");
  run("scheduleActivity = () => {}; openEvents = () => {};"); // V3-P80's 5 s read and V3-P83's stream would keep this process alive
  return { context, run, node };
}

// Two clicks waiting on one instruction save must share a creation request.
{
  const p = page(), leave = deferred();
  p.context.leave = leave.promise;
  p.run("var creations = 0; mayLeave = () => leave; createConversation = async () => { creations++; return true; };");
  const first = p.run("newConversation()"), second = p.run("newConversation()");
  leave.resolve(true);
  await Promise.all([first, second]);
  assert.equal(p.run("creations"), 1);
  assert.equal(p.run("app.startingConversation"), null);
}

// Cancelling a leave must release the lock without creating anything.
{
  const p = page();
  p.run("var creations = 0; mayLeave = async () => false; createConversation = async () => { creations++; };");
  assert.equal(await p.run("newConversation()"), false);
  assert.equal(p.run("creations"), 0);
  assert.equal(p.run("app.startingConversation"), null);
}

// Returning to the same id after navigating is still a different selection.
{
  const p = page();
  p.run("app.generation = 4; api = async () => ({id:'new'}); loadList = async () => {}; openConversation = async () => { app.conversationId = 'new'; app.generation += 3; };");
  assert.equal(await p.run("createConversation()"), false);
}

for (const scenario of ["same", "edited", "navigated", "failed", "stale-failure"]) {
  const p = page(), response = deferred();
  p.context.response = response.promise;
  p.run("app.conversationId = 'one'; app.place = 'one'; app.generation = 1; var requests = 0; var notices = []; status = (text) => notices.push(text); api = (method, path) => { if (path === '/api/conversations/activity') return Promise.resolve({ working: [], waiting: [] }); requests++; return response; };");
  p.node("composer-text").value = "  submitted draft  ";
  const pending = p.run("sendMessage({ preventDefault() {} })");
  if (scenario === "edited") p.node("composer-text").value = "next draft";
  if (scenario === "navigated" || scenario === "stale-failure") {
    p.run("app.generation++; app.conversationId = 'two'; app.place = 'two';");
    p.node("composer-text").value = "another conversation's draft";
  }
  if (scenario === "failed" || scenario === "stale-failure") {
    response.reject(p.run("new ApiError('network', 0, 'network', 'offline')"));
  } else response.resolve(null);
  await pending;
  const expected = scenario === "same" ? "" : scenario === "edited" ? "next draft"
    : scenario === "failed" ? "  submitted draft  " : "another conversation's draft";
  assert.equal(p.node("composer-text").value, expected, scenario);
  assert.equal(p.run("requests"), 1, "a mutation must never be replayed");
  assert.equal(p.run("app.sending"), false);
  if (scenario === "stale-failure") assert.equal(p.run("notices.length"), 0);
}

// A second submit while the first request is pending sends only once.
{
  const p = page(), response = deferred();
  p.context.response = response.promise;
  p.run("app.conversationId = 'one'; app.place = 'one'; var requests = 0; api = (method, path) => { if (path === '/api/conversations/activity') return Promise.resolve({ working: [], waiting: [] }); requests++; return response; };");
  p.node("composer-text").value = "one message";
  const first = p.run("sendMessage({ preventDefault() {} })");
  await p.run("sendMessage({ preventDefault() {} })");
  response.resolve(null);
  await first;
  assert.equal(p.run("requests"), 1);
}

// Creating the first conversation must send the clicked draft, not later edits.
{
  const p = page(), created = deferred();
  p.context.created = created.promise;
  p.node("surface-home").hidden = false;
  p.node("composer-text").value = "first message";
  p.run("app.place = 'home'; var accepted; newConversation = async () => { await created; app.conversationId = 'new'; app.place = 'new'; return true; }; api = async (method, path, body) => { accepted = body.text; };");
  const pending = p.run("sendMessage({ preventDefault() {} })");
  p.node("composer-text").value = "next draft";
  created.resolve();
  await pending;
  assert.equal(p.run("accepted"), "first message");
  assert.equal(p.node("composer-text").value, "next draft");
}

// Enter right after New chat waits for the new conversation and goes there,
// once even when pressed twice; the one before never gets it.
{
  const p = page(), created = deferred();
  p.context.created = created.promise;
  p.run("app.conversationId = 'before'; app.place = 'before'; var sent = []; mayLeave = async () => true; createConversation = async () => { await created; app.conversationId = 'new'; await openConversation('new', { created: true }); return true; }; api = async (method, path, body) => { sent.push([path, body.text]); };");
  p.run("openConversation = async (id) => { app.place = id; drafts.set(id, composerDraft()); };");
  p.node("composer-text").value = "the draft of the one before";
  p.run("composerChanged()");
  p.run("newConversation()");
  await new Promise((r) => setTimeout(r, 0)); // past the leave check
  // V3-P78 §3.1: New chat leaves the draft with the conversation it was in; what is typed now is the new one's.
  assert.equal(p.node("composer-text").value, "");
  assert.equal(p.run("drafts.get('before').text"), "the draft of the one before");
  p.node("composer-text").value = "right after new chat";
  const first = p.run("sendMessage({ preventDefault() {} })");
  const second = p.run("sendMessage({ preventDefault() {} })");
  await second;
  assert.equal(p.run("sent.length"), 0);
  created.resolve();
  await first;
  assert.equal(p.run("JSON.stringify(sent)"), JSON.stringify([["/api/conversations/new/messages", "right after new chat"]]));
  assert.equal(p.run("app.sendWaiting"), false);
}

// When that creation fails, or the user goes elsewhere meanwhile, nothing is sent.
{
  const p = page(), created = deferred();
  p.context.created = created.promise;
  p.node("surface-chat").hidden = false;
  p.run("app.conversationId = 'before'; app.place = 'before'; var sent = 0; mayLeave = async () => true; createConversation = async () => { await created; return false; }; api = async () => { sent++; };");
  p.node("composer-text").value = "before's";
  p.run("composerChanged()");
  p.run("newConversation()");
  await new Promise((r) => setTimeout(r, 0));
  p.node("composer-text").value = "kept";
  const pending = p.run("sendMessage({ preventDefault() {} })");
  created.resolve();
  await pending;
  assert.equal(p.run("sent"), 0);
  // V3-P78 §3.1: what was typed for it goes back to where it started, after that place's own draft.
  assert.equal(p.node("composer-text").value, "before's\n\nkept");
  assert.equal(p.run("app.place"), "before");
}

// PR #93 review B2: a conversation picked from a folder's list opens with its files, as the button's does.
{
  const p = page();
  p.run(`var opened = []; openConversation = async (id) => { opened.push([id, app.filesOpen]); };
         app.filesOpen = false;
         api = async (method, path) => ({ path: "/p", own: false, refused: null, workspace: null, above: [],
           conversations: [{ id: "c1", title: "One", preview: "", last_activity_at: "2026-10-25T00:00:00Z" }] });`);
  const control = p.run("openHereControl('/p')");
  await new Promise((r) => setTimeout(r, 0));
  const item = control.all().find((n) => n.className.includes("files-open-chat-item"));
  await item.click();
  assert.equal(p.run("JSON.stringify(opened)"), JSON.stringify([["c1", true]]));
}

// PR #93 review B1: the folder's button is off while its request is under way.
{
  const p = page(), answer = deferred();
  p.context.answer = answer.promise;
  p.run(`var asked = 0; openConversation = async () => {}; loadList = async () => [];
         api = async (method, path) => { if (method === "POST") { asked++; return answer; }
           return { path: "/p", own: false, refused: null, workspace: null, above: [], conversations: [] }; };`);
  const control = p.run("openHereControl('/p')");
  const main = control.children[0];
  const clicked = main.click();
  assert.equal(main.disabled, true);
  answer.resolve({ conversation: { id: "c2" }, created: true });
  await clicked;
  assert.equal(main.disabled, false);
  assert.equal(p.run("asked"), 1);
}

// V3-P78 §3.1: each conversation has its own draft; Home's goes into the conversation it starts.
{
  const p = page();
  p.run("app.place = 'a'; app.conversationId = 'a';");
  p.node("composer-text").value = "for a";
  p.run("composerChanged(); leavePlace(); enterPlace('b');");
  assert.equal(p.node("composer-text").value, "", "b starts empty: a's draft never follows");
  p.node("composer-text").value = "for b";
  p.run("composerChanged(); leavePlace(); enterPlace('a');");
  assert.equal(p.node("composer-text").value, "for a");
  p.run("leavePlace(); enterPlace('b');");
  assert.equal(p.node("composer-text").value, "for b");
  // A place entered and left without a change keeps what it had.
  p.run("leavePlace(); enterPlace('a'); leavePlace(); enterPlace('a');");
  assert.equal(p.node("composer-text").value, "for a");
}

// V3-P78 §3.1 (review B2): a message accepted after the user left takes its text out of that draft.
{
  const p = page(), response = deferred();
  p.context.response = response.promise;
  p.run("app.conversationId = 'a'; app.place = 'a'; app.generation = 1; api = () => response;");
  p.node("composer-text").value = "sent from a";
  p.run("composerChanged()");
  const pending = p.run("sendMessage({ preventDefault() {} })");
  assert.equal(p.run("drafts.sending('a')"), true);
  p.run("leavePlace(); app.generation++; app.conversationId = 'b'; enterPlace('b');");
  response.resolve(null);
  await pending;
  assert.equal(p.run("drafts.get('a').text"), "", "never sent twice");
  assert.equal(p.run("drafts.sending('a')"), false);
  p.run("leavePlace(); enterPlace('a');");
  assert.equal(p.node("composer-text").value, "");
}

// PR #106 review B2: chips put back by a leave and a return still leave once the send is accepted.
{
  const p = page(), response = deferred();
  p.context.response = response.promise;
  p.run(`app.conversationId = 'a'; app.place = 'a'; app.generation = 1; api = () => response;
         app.attachments = [{ key: 1, name: 'f', size: 1, state: 'ready', fileId: 'f1', uploadedAt: Date.now() }];
         app.references = [{ key: 2, path: '/x', whole: true, identity: [1, 2, 3, 4], label: 'x' }];`);
  p.node("composer-text").value = "with chips";
  p.run("composerChanged()");
  const pending = p.run("sendMessage({ preventDefault() {} })");
  p.run("leavePlace(); enterPlace('b'); leavePlace(); enterPlace('a');");
  assert.equal(p.run("app.attachments.length + app.references.length"), 2);
  response.resolve(null);
  await pending;
  assert.equal(p.run("app.attachments.length + app.references.length"), 0);
  assert.equal(p.node("composer-text").value, "");
}

// V3-P78 §3.1 (review B1, N5): Enter on Home carries its draft into the new conversation.
{
  const p = page(), created = deferred();
  p.context.created = created.promise;
  p.node("surface-home").hidden = false;
  p.run(`app.place = 'home'; var sent = []; mayLeave = async () => true;
         openConversation = async (id, options) => { app.conversationId = id; app.place = id; drafts.set(id, composerDraft()); if (options && options.created) drafts.remove('home'); };
         createConversation = async () => { await created; await openConversation('new', { created: true }); return true; };
         api = async (method, path, body) => { sent.push([path, body.text]); };`);
  p.node("composer-text").value = "from home";
  p.run("composerChanged()");
  const pending = p.run("sendMessage({ preventDefault() {} })");
  created.resolve();
  await pending;
  assert.equal(p.run("JSON.stringify(sent)"), JSON.stringify([["/api/conversations/new/messages", "from home"]]));
  assert.equal(p.run("drafts.get('home').text"), "");
}

// V3-P78 §3.2 (Q1): leaving while a file uploads asks; Stay keeps it, Leave cancels it once.
{
  const p = page();
  let answer = false;
  p.context.confirmAction = async () => answer;
  p.run("var aborted = 0; app.attachments = [{ state: 'uploading', xhr: { abort() { aborted++; } } }, { state: 'ready', fileId: 'f1', name: 'a', size: 1 }]; filesPanel.dirty = false;");
  p.run("Object.defineProperty(filesPanel, 'dirty', { value: false });");
  assert.equal(await p.run("mayLeave()"), false);
  assert.equal(p.run("app.attachments.length"), 2);
  answer = true;
  assert.equal(await p.run("mayLeave()"), true);
  assert.equal(p.run("aborted"), 1);
  assert.equal(p.run("JSON.stringify(app.attachments.map((a) => a.fileId))"), JSON.stringify(["f1"]));
}

// V3-P78 §6: a second 401 while the sign-in screen shows (the file panel's 5 s check) leaves the token typed.
{
  const p = page();
  p.run("filesPanel.closeForSignIn = () => {}; filesSurface.closeForSignIn = () => {}; showSurface = (name) => { for (const s of ['login', 'home', 'chat']) document.getElementById(`surface-${s}`).hidden = s !== name; };");
  p.run("showLogin()");
  assert.equal(p.node("surface-login").hidden, false);
  p.node("login-token").value = "half a tok";
  p.run("showLogin()");
  assert.equal(p.node("login-token").value, "half a tok");
}

// V3-P79 §4.1: Enter sends from a hardware keyboard, makes a line with an on-screen one up; Shift+Enter is a
// line, Cmd/Ctrl+Enter sends; an Enter that ends a composition (isComposing, or Safari's keyCode 229) is the IME's.
{
  const p = page();
  const key = (extra = {}) => ({ key: "Enter", shiftKey: false, metaKey: false, ctrlKey: false, isComposing: false,
                                 keyCode: 13, ...extra });
  const windowWith = (visible, { touch = false, scale = 1 } = {}) => ({
    innerHeight: 1000, innerWidth: 800, visualViewport: { height: visible, scale },
    matchMedia: () => ({ matches: touch }) });
  p.context.window = windowWith(1000);
  p.run("noteWindowHeight()");
  assert.equal(p.context.enterSends(key()), true, "hardware keyboard");
  assert.equal(p.context.enterSends(key({ shiftKey: true })), false);
  assert.equal(p.context.enterSends(key({ isComposing: true })), false);
  assert.equal(p.context.enterSends(key({ keyCode: 229 })), false, "Safari's composition Enter");
  assert.equal(p.context.enterSends(key({ key: "a" })), false);
  p.context.window = windowWith(1000 - 60, { touch: true }); // an iPad keyboard's shortcut bar
  assert.equal(p.context.enterSends(key()), true);
  p.context.window = windowWith(600, { touch: true }); // the on-screen keyboard
  assert.equal(p.context.enterSends(key()), false, "a new line");
  assert.equal(p.context.enterSends(key({ metaKey: true })), true);
  assert.equal(p.context.enterSends(key({ ctrlKey: true })), true);
  assert.equal(p.context.enterSends(key({ keyCode: 229, metaKey: true })), false);
  p.context.window = windowWith(500, { scale: 2 }); // zoomed in, with a hardware keyboard
  assert.equal(p.context.enterSends(key()), true, "zoom is not a keyboard");
  // Android: the window itself shrinks for the keyboard (interactive-widget=resizes-content).
  p.context.window = { ...windowWith(1000, { touch: true }) };
  p.run("noteWindowHeight()");
  p.context.window = { ...windowWith(600, { touch: true }), innerHeight: 600 };
  assert.equal(p.context.enterSends(key()), false);
  p.context.window = { ...windowWith(600), innerHeight: 600 }; // a desktop window made smaller
  assert.equal(p.context.enterSends(key()), true);
  // PR #108 review B1: an iPad with a keyboard folio (touch-first), turned from portrait to landscape: its
  // window is shorter than before, but it is wider too — a rotation, not a keyboard. Enter still sends.
  p.context.window = { ...windowWith(1100, { touch: true }), innerWidth: 820, innerHeight: 1100 };
  p.run("noteWindowHeight()");
  p.context.window = { ...windowWith(760, { touch: true }), innerWidth: 1180, innerHeight: 760 };
  p.run("noteWindowHeight()");
  assert.equal(p.context.enterSends(key()), true, "rotated, with a hardware keyboard");
}

// A file HIMARI wrote on this server opens beside the conversation (the web UX review): the path the server
// resolved, from the result — never the arguments joined to today's working folder (review B1).
{
  const p = page();
  const entry = (toolId, text) => ({ toolId, args: { path: "relative.txt" }, result: { text } });
  assert.equal(p.context.openableWrite(entry("machine/write_file", "created /home/u/a.py (12 bytes)")), "/home/u/a.py");
  assert.equal(p.context.openableWrite(entry("machine/write_file", "updated /home/u/my notes.md (3 bytes)")),
               "/home/u/my notes.md");
  assert.equal(p.context.openableWrite(entry("machine/edit_file", "edited /etc/x.conf: replaced 2 occurrence(s)")),
               "/etc/x.conf");
  assert.equal(p.context.openableWrite(entry("machine/write_file",
    "created /home/u/a.py, but writing it did not finish; its content may be partial — read it and write it again")), null);
  assert.equal(p.context.openableWrite(entry("machine/read_file", "created /home/u/a.py (1 bytes)")), null);
  assert.equal(p.context.openableWrite(entry("inside/write_file", "created /home/u/a.py (1 bytes)")), null);
  assert.equal(p.context.openableWrite({ toolId: "machine/write_file", args: {}, result: null }), null);
}

// The list's headings: Today, Yesterday, … by the day a conversation was last active (local days).
{
  const p = page();
  const ago = (days) => new Date(Date.now() - days * 86400000).toISOString();
  assert.equal(p.context.activityGroup(new Date().toISOString()), "Today");
  assert.equal(p.context.activityGroup(ago(1)), "Yesterday");
  assert.equal(p.context.activityGroup(ago(3)), "Previous 7 days");
  assert.equal(p.context.activityGroup(ago(12)), "Previous 30 days");
  assert.equal(p.context.activityGroup(ago(90)), "Older");
}

// V3-P82 §6.3: this tab saved a PDF's ink — the chips pointing at it as it was (the composer's, and the drafts
// kept for other places) point at it now; other files' and other versions' chips stay, and so does each
// draft's time (only its chips changed).
{
  const p = page();
  p.run(`
    var PDF = "/home/u/notes.pdf";
    app.references = [{ key: "a", path: PDF, identity: ["1", "2", "3", "5"], stale: true },
                      { key: "b", path: PDF, identity: ["1", "2", "3", "4"] },
                      { key: "c", path: "/home/u/other.pdf", identity: ["1", "2", "3", "5"] }];
    drafts.now = () => 5;
    drafts.set("elsewhere", { text: "about it", attachments: [], references: [{ path: PDF, identity: [1, 2, 3, 5] }] });
    drafts.now = () => 99;
    followSave({ path: PDF, from: ["1", "2", "3", "5"], to: ["9", "9", "9", "9"] });
  `);
  assert.deepEqual(JSON.parse(p.run("JSON.stringify(app.references.map((r) => [r.key, r.identity, Boolean(r.stale)]))")),
                   [["a", ["9", "9", "9", "9"], false], ["b", ["1", "2", "3", "4"], false],
                    ["c", ["1", "2", "3", "5"], false]]);
  assert.deepEqual(JSON.parse(p.run("JSON.stringify(drafts.get('elsewhere').references)")),
                   [{ path: "/home/u/notes.pdf", identity: ["9", "9", "9", "9"] }]);
  assert.equal(p.run("drafts.at('elsewhere')"), 5, "its time kept");
  assert.equal(p.run("drafts.get('elsewhere').text"), "about it");
}

// PR #119 review C3: in the store, a newer draft another tab kept for that place is not overwritten.
{
  const p = page();
  p.run(`
    var PDF = "/home/u/notes.pdf";
    var puts = [];
    drafts.now = () => 5;
    drafts.set("mine", { text: "a", attachments: [], references: [{ path: PDF, identity: ["1"] }] });
    drafts.set("theirs", { text: "b", attachments: [], references: [{ path: PDF, identity: ["1"] }] });
    drafts.store = { put: async (s, place, value) => { puts.push([place, value.references[0].identity[0]]); },
                     get: async (s, place) => (place === "theirs" ? { writer: "another tab", at: 50, text: "newer" } : null),
                     remove: async () => {}, prune: async () => {}, entries: async () => [] };
    var following = drafts.follow(PDF, ["1"], ["9"]);
  `);
  await p.run("following");
  assert.deepEqual(JSON.parse(p.run("JSON.stringify(puts)")), [["mine", "9"]]);
  assert.equal(p.run("drafts.get('theirs').references[0].identity[0]"), "9", "followed in memory");
}

console.log("UI async regression checks: 25 passed");
