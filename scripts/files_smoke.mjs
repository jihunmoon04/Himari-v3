/* The file explorer's pane (files.js, V3-P75 §3) against a fake DOM: no
 * browser, packages or live service. Usage: node scripts/files_smoke.mjs */
import assert from "node:assert/strict";

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.parent = null;
  }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
  prepend(...nodes) { for (const n of nodes) n.parent = this; this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  after(node) { if (this.parent) this.parent.append(node); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  replaceWith(node) { if (this.parent) this.parent.children = this.parent.children.map((c) => (c === this ? node : c)); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  querySelectorAll() { return []; }
  get classList() { return { toggle() {}, add() {}, remove() {} }; }
  text() { return [this.textContent, ...this.children.map((c) => c.text())].join(" ").trim(); }
  contains(node) { return node === this || this.children.some((c) => c.contains(node)) || this.children.some((c) => c.firstChild === node); }
  get firstChild() { // a line's text, as its text node
    if (this.children.length) return this.children[0];
    if (!this.textContent) return null;
    return (this.textNode ||= { length: this.textContent.length, parentElement: this });
  }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
}

globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag),
};

const { FilesPane } = await import(new URL("../src/himari/interfaces/web/static/files.js", import.meta.url));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const HOME = { path: "/home/u", kind: "directory" };
const listing = (path, names) => ({ path, parent: "/", next: null, workspace: null,
  entries: names.map((name) => ({ name, kind: "file", size: 1, modified: 0 })) });

/* A pane whose answers the test hands out one by one. */
function pane() {
  const waiting = [];
  const asked = [];
  const api = (method, path) => {
    asked.push(path);
    if (path === "/api/machine/stat?path=~") return Promise.resolve(HOME);
    const answer = deferred();
    waiting.push({ path, answer });
    return answer.promise;
  };
  const files = new FilesPane({ api, el });
  const next = async (prefix) => {
    for (let i = 0; i < 20 && !waiting.some((w) => w.path.startsWith(prefix)); i++) await new Promise((r) => setTimeout(r, 0));
    const index = waiting.findIndex((w) => w.path.startsWith(prefix));
    assert.notEqual(index, -1, `a request for ${prefix}`);
    return waiting.splice(index, 1)[0].answer;
  };
  return { files, next, asked };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

const within = (promise) => Promise.race([promise, new Promise((r) => setTimeout(r, 50))]);

// PR #92 review B2: a link's late answer does not replace what was opened meanwhile.
{
  const { files, next, asked } = pane();
  const link = files.follow("/home/u/link", "symlink");
  const stat = await next("/api/machine/stat?path=%2Fhome%2Fu%2Flink");
  const folder = files.openFolder("/home/u/b");
  (await next("/api/machine/list?path=%2Fhome%2Fu%2Fb")).resolve(listing("/home/u/b", ["inside-b"]));
  await folder;
  stat.resolve({ path: "/srv/elsewhere", kind: "directory" });
  await within(link);
  await settle();
  assert.equal(files.folder, "/home/u/b");
  assert.ok(files.body.text().includes("inside-b"));
  assert.ok(!asked.some((p) => p.includes("elsewhere")), "the link's target was not opened");
}

// …nor does its late failure.
{
  const { files, next } = pane();
  const link = files.follow("/home/u/link", "symlink");
  const stat = await next("/api/machine/stat?path=%2Fhome%2Fu%2Flink");
  const folder = files.openFolder("/home/u/b");
  (await next("/api/machine/list?path=%2Fhome%2Fu%2Fb")).resolve(listing("/home/u/b", ["inside-b"]));
  await folder;
  stat.reject(Object.assign(new Error("no such file or directory"), { status: 404 }));
  await within(link);
  await settle();
  assert.ok(files.body.text().includes("inside-b") && !files.body.text().includes("no such file"));
}

// A link followed with nothing else opened meanwhile opens its target.
{
  const { files, next } = pane();
  const link = files.follow("/home/u/link", "symlink");
  (await next("/api/machine/stat?path=%2Fhome%2Fu%2Flink")).resolve({ path: "/home/u/real", kind: "directory" });
  (await next("/api/machine/list?path=%2Fhome%2Fu%2Freal")).resolve(listing("/home/u/real", ["x"]));
  await link;
  assert.equal(files.folder, "/home/u/real");
}

// An older folder answer never replaces a newer one (the ticket for plain opens).
{
  const { files, next } = pane();
  const first = files.openFolder("/home/u/a");
  const slow = await next("/api/machine/list?path=%2Fhome%2Fu%2Fa");
  const second = files.openFolder("/home/u/b");
  (await next("/api/machine/list?path=%2Fhome%2Fu%2Fb")).resolve(listing("/home/u/b", ["inside-b"]));
  await second;
  slow.resolve(listing("/home/u/a", ["inside-a"]));
  await first;
  assert.equal(files.folder, "/home/u/b");
  assert.ok(!files.body.text().includes("inside-a"));
}

// PR #93 review N1: a folder row gets a ⋯ only with something to do, and never a Download.
{
  const node = (row) => row.children.find((c) => c.className.includes("conversation-actions"));
  const labels = (menu) => menu.children[1].children.map((c) => c.textContent);
  for (const [actions, expected] of [[[], null], [[["Attach to this conversation", () => {}]], ["Attach to this conversation", "Copy path"]]]) {
    const files = new FilesPane({ api: async () => HOME, el, folderRowActions: () => actions });
    const folderRow = files.row("/home/u", { name: "sub", kind: "directory", size: null, modified: 0 });
    const fileRow = files.row("/home/u", { name: "a.txt", kind: "file", size: 1, modified: 0 });
    if (expected === null) assert.equal(node(folderRow), undefined);
    else assert.deepEqual(labels(node(folderRow)), expected);
    assert.deepEqual(labels(node(fileRow)), ["Download", "Copy path"]); // nothing here deletes (V3-P99)
  }
}

// --- a file is shown, never edited (V3-P99) -----------------------------------------------------------

const { selectionIn } = await import(new URL("../src/himari/interfaces/web/static/files.js", import.meta.url));

/* A pane over fixed answers: {"METHOD /path-prefix": answer}; what was asked is in ``sent``. */
function viewer(routes, { onAsk = null } = {}) {
  const sent = [];
  const api = async (method, path) => {
    sent.push(`${method} ${path}`);
    if (path === "/api/machine/stat?path=~") return HOME;
    const key = Object.keys(routes).find((k) => `${method} ${path}`.startsWith(k));
    if (!key) throw Object.assign(new Error("not found"), { status: 404, code: "not_found" });
    return routes[key];
  };
  return { files: new FilesPane({ api, el, onAsk }), sent };
}

const shownFile = (text, more = {}) => ({ path: "/home/u/notes.txt", size: (text || "").length, identity: ["1", "2", "3", "4"],
                                         modified: 0, kind: "text", text, truncated: false, ...more });
const controls = (files) => files.tools.children.flatMap((c) => (c.children.length ? c.children[1].children : [c]))
  .map((c) => c.textContent);

// Text is shown line by line, with its line breaks as positions count them; nothing offers a save or a delete.
{
  const asked = [];
  const { files, sent } = viewer({ "GET /api/machine/file": shownFile("one\r\ntwo\rthree") }, { onAsk: (r) => asked.push(r) });
  await files.openFile("/home/u/notes.txt");
  const pre = files.body.children[0].children[0];
  assert.deepEqual(pre.children.map((line) => line.textContent), ["one\n", "two\n", "three\n"]);
  assert.equal(files.shown.text, "one\ntwo\nthree");
  assert.deepEqual(controls(files), ["Ask HIMARI", "Ask HIMARI about this file", "Download", "Copy path"]);
  assert.ok(sent.every((s) => s.startsWith("GET ")), "only reads");
  // With nothing selected (no window here): the whole file.
  await files.ask();
  assert.deepEqual(asked, [{ path: "/home/u/notes.txt", name: "notes.txt", identity: ["1", "2", "3", "4"], whole: true }]);
}

// A selection: its positions in the text, never past the break shown after a last line without one.
{
  const { files } = viewer({ "GET /api/machine/file": shownFile("alpha\nbeta") });
  await files.openFile("/home/u/notes.txt");
  const { pre, text } = files.shown;
  const before = (node, offset) => {
    let n = 0;
    for (const line of pre.children) {
      if (line.firstChild === node) return n + offset;
      n += line.textContent.length;
    }
    throw new Error("not a line of the file");
  };
  document.createRange = () => ({ selectNodeContents() {}, setEnd(node, offset) { this.length = before(node, offset); },
                                 toString() { return "x".repeat(this.length); } });
  const picked = (from, to) => { // a selection from a position of the first line to one of the second
    const [a, b] = [pre.children[0].firstChild, pre.children[1].firstChild];
    const shown = pre.children.map((line) => line.textContent).join("");
    return { rangeCount: 1, isCollapsed: from === to,
             getRangeAt: () => ({ startContainer: a, startOffset: from, endContainer: b, endOffset: to - 6,
                                  toString: () => shown.slice(from, to) }) };
  };
  assert.deepEqual(selectionIn(pre, text, picked(2, 8)), { from: 2, to: 8, text: "pha\nbe" });
  assert.deepEqual(selectionIn(pre, text, picked(6, 11)), { from: 6, to: 10, text: "beta" }); // not the shown break
  assert.equal(selectionIn(pre, text, picked(3, 3)), null);
  assert.equal(selectionIn(pre, text, { rangeCount: 1, isCollapsed: false, getRangeAt: () => ({
    startContainer: {}, startOffset: 0, endContainer: {}, endOffset: 1, toString: () => "x" }) }), null); // elsewhere on the page
  delete document.createRange;
}

// The start of a large file: shown, but no selection is pointed at in it (the whole file still is).
{
  const { files } = viewer({ "GET /api/machine/file": shownFile("start", { truncated: true, size: 2_000_000 }) }, { onAsk: () => {} });
  await files.openFile("/home/u/notes.txt");
  assert.equal(files.shown, null);
  assert.ok(files.body.text().includes("too large to show whole"));
  assert.deepEqual(controls(files), ["Ask HIMARI about this file", "Download", "Copy path"]);
}

// Another kind of file is a download; a folder's head has Refresh only (and the place's own controls).
{
  const { files } = viewer({ "GET /api/machine/file": shownFile(null, { kind: "binary", size: 9 }),
                            "GET /api/machine/list": listing("/home/u", ["a.bin"]) });
  await files.openFile("/home/u/a.bin");
  assert.ok(files.body.text().includes("No preview for this file"));
  assert.deepEqual(controls(files), ["Download", "Copy path"]);
  await files.openFolder("/home/u");
  assert.deepEqual(files.tools.children.map((c) => c.title), ["Refresh"]);
}

// After a turn: the file shown is read again; back goes to the folder it was opened from.
{
  const { files, sent } = viewer({ "GET /api/machine/file": shownFile("v1"), "GET /api/machine/list": listing("/home/u", ["notes.txt"]) });
  await files.openFolder("/home/u");
  await files.openFile("/home/u/notes.txt");
  const reads = () => sent.filter((s) => s.startsWith("GET /api/machine/file")).length;
  assert.equal(reads(), 1);
  await files.refresh();
  assert.equal(reads(), 2);
  assert.equal(files.locations.length, 2, "a refresh is not a move");
  await files.back();
  assert.equal(files.file, null);
  assert.equal(files.folder, "/home/u");
}

// A file that cannot be opened says so, and nothing of the last one is left to ask about.
{
  const { files } = viewer({ "GET /api/machine/file": shownFile("v1") });
  await files.openFile("/home/u/notes.txt");
  files.api = async (method, path) => {
    if (path === "/api/machine/stat?path=~") return HOME;
    throw Object.assign(new Error("No such file."), { status: 404, code: "not_found" });
  };
  await files.openFile("/home/u/gone.txt");
  assert.ok(files.body.text().includes("No such file."));
  assert.equal(files.shown, null);
}

console.log("files smoke: ok");
