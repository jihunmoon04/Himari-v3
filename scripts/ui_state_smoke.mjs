/* Focused async UI regression checks. No browser, packages, or live service.
 * Usage: node scripts/ui_state_smoke.mjs */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../src/himari/interfaces/web/static/app.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/gm, "");

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
    if (!nodes.has(id)) nodes.set(id, { value: "", disabled: false, hidden: true, focus() {} });
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: node, addEventListener() {}, createElement: (tag) => new FakeElement(tag) },
    AppViews: class {}, FilesPane: class { refresh() {} openFolder() {} }, console, setTimeout, clearTimeout,
    LiveAnswer: class { reset() {} shown() {} keepLast() {} settle() {} open() {} restyle() {} },
  });
  const run = (code) => vm.runInContext(code, context);
  run(source);
  run("status = () => {}; abandonExpansion = () => {}; autosize = () => {}; renderAttachments = () => {}; refresh = async () => {}; poll = () => {};");
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
  p.run("app.conversationId = 'one'; app.generation = 1; var requests = 0; var notices = []; status = (text) => notices.push(text); api = () => { requests++; return response; };");
  p.node("composer-text").value = "  submitted draft  ";
  const pending = p.run("sendMessage({ preventDefault() {} })");
  if (scenario === "edited") p.node("composer-text").value = "next draft";
  if (scenario === "navigated" || scenario === "stale-failure") {
    p.run("app.generation++; app.conversationId = 'two';");
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
  p.run("app.conversationId = 'one'; var requests = 0; api = () => { requests++; return response; };");
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
  p.run("var accepted; newConversation = async () => { await created; app.conversationId = 'new'; return true; }; api = async (method, path, body) => { accepted = body.text; };");
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
  p.node("composer-text").value = "right after new chat";
  p.run("app.conversationId = 'before'; var sent = []; mayLeave = async () => true; createConversation = async () => { await created; app.conversationId = 'new'; return true; }; api = async (method, path, body) => { sent.push([path, body.text]); };");
  p.run("newConversation()");
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
  const p = page();
  p.node("composer-text").value = "kept";
  p.run("app.conversationId = 'before'; var sent = 0; mayLeave = async () => true; createConversation = async () => false; api = async () => { sent++; };");
  p.run("newConversation()");
  await p.run("sendMessage({ preventDefault() {} })");
  assert.equal(p.run("sent"), 0);
  assert.equal(p.node("composer-text").value, "kept");
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

console.log("UI async regression checks: 14 passed");
