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
}

globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag),
};

const { FilesPane, readDownload, postAppend } = await import(new URL("../src/himari/interfaces/web/static/files.js", import.meta.url));
const { strokeFromSaved } = await import(new URL("../src/himari/interfaces/web/static/pdfink.js", import.meta.url));

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

/* Timers of a second or more run only when the test moves the clock (V3-P82's waits); a 0 ms one runs as ever. */
function fakeClock() {
  const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, now: Date.now };
  let now = real.now();
  let next = 1;
  const timers = new Map();
  globalThis.setTimeout = (fn, ms = 0, ...args) => {
    if (!ms) return real.setTimeout(fn, 0, ...args);
    const id = `fake-${next++}`;
    timers.set(id, { fn, at: now + ms, ms });
    return id;
  };
  globalThis.clearTimeout = (id) => {
    if (typeof id === "string" && id.startsWith("fake-")) timers.delete(id);
    else real.clearTimeout(id);
  };
  Date.now = () => now;
  return {
    timers,
    waits: () => [...timers.values()].map((t) => t.ms).sort((a, b) => a - b),
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at;
        timers.delete(due[0]);
        due[1].fn();
        for (let i = 0; i < 10; i++) await settle();
      }
      now = end;
    },
    restore() {
      globalThis.setTimeout = real.setTimeout;
      globalThis.clearTimeout = real.clearTimeout;
      Date.now = real.now;
    },
  };
}
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
    assert.deepEqual(labels(node(fileRow)), ["Delete…", "Download", "Copy path"]);
  }
}

// A file deleted from its row's ⋯: asked as from the file itself (kept or not, by the kind the server reads),
// with the identity seen; the folder is read again (the web UX review's row Delete).
{
  const sent = [];
  const asked = [];
  const routes = {
    "GET /api/machine/stat": { path: "/home/u/big.pdf", kind: "file", size: 5 * 1024 * 1024, identity: [3, 3, 3, 3] },
    "GET /api/machine/file": { path: "/home/u/big.pdf", kind: "pdf", size: 5 * 1024 * 1024, identity: [3, 3, 3, 3] },
    "POST /api/machine/file/delete": { path: "/home/u/big.pdf", previous_kept: true },
    "GET /api/machine/list": listing("/home/u", ["other.txt"]),
  };
  const api = async (method, path, body) => {
    sent.push([method, path, body]);
    if (path === "/api/machine/stat?path=~") return HOME;
    const key = Object.keys(routes).find((k) => `${method} ${path}`.startsWith(k));
    return routes[key];
  };
  const files = new FilesPane({ api, el, confirm: async (title, lines, action) => { asked.push([title, lines, action]); return true; } });
  files.folder = "/home/u";
  await files.deleteFile("/home/u/big.pdf");
  assert.deepEqual(asked.map(([t, , a]) => [t, a]), [["Delete big.pdf?", "Delete"]]); // a PDF's content is kept: one question
  assert.deepEqual(asked[0][1], ["Its content is kept as a previous version on the server for 30 days."]);
  assert.deepEqual(sent.find(([m, p]) => m === "POST")[2], { path: "/home/u/big.pdf", expected: [3, 3, 3, 3] });
  assert.ok(files.body.text().includes("other.txt"), "the folder again");

  // Review B1: a PDF opened while the row's delete was on its way stays open.
  let release;
  routes["POST /api/machine/file/delete"] = new Promise((r) => { release = r; });
  const deleting = files.deleteFile("/home/u/big.pdf");
  await new Promise((r) => setTimeout(r, 0));
  let destroyed = false;
  files.ticket += 1; // something else opened meanwhile
  files.pdf = { path: "/home/u/other.pdf", pollTimer: null, view: { current: 0, zoom: 1, destroy() { destroyed = true; } } };
  release({ path: "/home/u/big.pdf", previous_kept: true });
  await deleting;
  assert.equal(destroyed, false);
  assert.equal(files.pdf.path, "/home/u/other.pdf");
}

// --- the editor (V3-P75 §7), with a fake CodeMirror and a fake server ---------------------------------

function fakeModule() {
  const made = [];
  const read = (text) => text.replace(/\r\n?/g, "\n"); // as CodeMirror does: every line break one "\n"
  return {
    made,
    createEditor(parent, opts) {
      const editor = { value: read(opts.text), opts, text: () => editor.value, destroyed: false, picked: [0, 0],
        setText(text) { editor.value = read(text); },
        type(text) { editor.value = read(text); if (opts.onChange) opts.onChange(); },
        destroy() { editor.destroyed = true; }, readOnly: false, setReadOnly(on) { editor.readOnly = on; }, focus() {},
        selection() { // one line: columns are positions + 1
          const [from, to] = editor.picked;
          return { from, to, text: editor.value.slice(from, to), fromLine: 1, fromColumn: from + 1, toLine: 1,
                   toColumn: to + 1 };
        },
        select(from, to) { editor.picked = [from, to]; } };
      made.push(editor);
      return editor;
    },
    createDiff(parent, sides) { made.push({ diff: sides }); return { destroy() {} }; },
  };
}

/* A pane over a fake server: ``routes`` answers "METHOD path-start"; ``sent`` keeps every request. */
function editorPane(routes, { confirms = [], onAsk = null, changes = null, working = () => false, keep = null,
                              openElsewhere = () => false } = {}) {
  const sent = [];
  const module = fakeModule();
  const asked = [];
  const api = async (method, path, body) => {
    sent.push([method, path, body]);
    if (path === "/api/machine/stat?path=~") return HOME;
    const key = Object.keys(routes).find((k) => `${method} ${path}`.startsWith(k));
    assert.ok(key, `no route for ${method} ${path}`);
    const answer = routes[key];
    return typeof answer === "function" ? answer(body, path) : answer;
  };
  const files = new FilesPane({ api, el, loadEditor: async () => module, onAsk, changes, working, keep, writer: "me",
    openElsewhere,
    confirm: async (title, lines, action) => { asked.push([title, action]); return confirms.length ? confirms.shift() : true; } });
  return { files, sent, module, asked };
}

const TEXT = { path: "/home/u/a.txt", size: 3, identity: [1, 2, 3, 4], modified: 0, kind: "text", text: "one",
               truncated: false, editable: true };
const refusal = (code) => Object.assign(new Error(code), { code });

// Typing, then saving: the identity read goes with it; the answer's is the next save's.
{
  const { files, sent, module } = editorPane({
    "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": { path: TEXT.path, identity: [1, 2, 5, 6], previous_kept: true },
  });
  await files.openFile(TEXT.path);
  const editor = module.made[0];
  assert.equal(editor.text(), "one");
  assert.equal(files.saveButton.disabled, true);
  editor.type("two");
  assert.equal(files.dirty, true);
  assert.equal(files.saveButton.disabled, false);
  await files.save();
  const put = sent.find(([method]) => method === "PUT");
  assert.deepEqual(put[2], { path: TEXT.path, content: "two", expected: [1, 2, 3, 4] });
  assert.equal(files.dirty, false);
  assert.deepEqual(files.doc.identity, [1, 2, 5, 6]);
  files.closeDoc();
}

// A save refused because the file changed on the server: the text stays; "save mine over it" saves
// against the server's identity now (its version kept there).
{
  let puts = 0;
  const { files, sent, module } = editorPane({
    "GET /api/machine/file": TEXT,
    "GET /api/machine/stat": { path: TEXT.path, kind: "file", identity: [9, 9, 9, 9], size: 9 },
    "PUT /api/machine/file": () => { puts += 1; if (puts === 1) throw refusal("changed");
      return { path: TEXT.path, identity: [7, 7, 7, 7], previous_kept: true }; },
  });
  await files.openFile(TEXT.path);
  module.made[0].type("mine");
  await files.save();
  assert.equal(files.doc.bar.hidden, false);
  assert.equal(module.made[0].text(), "mine");
  assert.equal(files.dirty, true);
  const over = files.doc.bar.children.find((c) => c.textContent === "Save mine over it");
  await over.listeners.click[0]();
  assert.deepEqual(sent.filter(([m]) => m === "PUT").map(([, , b]) => b.expected), [[1, 2, 3, 4], [9, 9, 9, 9]]);
  assert.equal(files.dirty, false);
  assert.equal(files.doc.bar.hidden, true);
  files.closeDoc();
}

// A file's own line breaks: not "unsaved" when opened or loaded again, and written back by a save.
for (const [text, typed, written] of [
  ["one\r\ntwo\r\n", "one\nthree\n", "one\r\nthree\r\n"],
  ["one\rtwo\r", "one\nthree\n", "one\rthree\r"],
  ["one\ntwo\n", "one\nthree\n", "one\nthree\n"],
  ["a\r\nb\r\nc\nd\r\n", "a\nb\n", "a\r\nb\r\n"], // mixed: the break most lines end with
]) {
  const { files, sent, module } = editorPane({
    "GET /api/machine/file": { ...TEXT, text },
    "PUT /api/machine/file": { path: TEXT.path, identity: [1, 2, 5, 6], previous_kept: true },
  });
  await files.openFile(TEXT.path);
  const editor = module.made[0];
  editor.type(editor.text()); // a change, then back: the same text
  assert.equal(files.dirty, false);
  editor.type(typed);
  await files.save();
  assert.equal(sent.find(([method]) => method === "PUT")[2].content, written);
  assert.equal(files.dirty, false);
  await files.loadDisk(); // the server's version (as it was read) again: nothing to save
  assert.equal(files.dirty, false);
  files.restore(text); // a previous version with the same text
  assert.equal(files.dirty, false);
  files.closeDoc();
}

// Changed on disk while open: unchanged text follows it; text being edited is kept and the user told.
{
  let disk = { ...TEXT };
  const { files, module } = editorPane({
    "GET /api/machine/file": () => disk,
    "GET /api/machine/stat": () => ({ path: TEXT.path, kind: "file", identity: disk.identity, size: disk.size }),
  });
  await files.openFile(TEXT.path);
  disk = { ...TEXT, text: "from HIMARI", identity: [1, 2, 11, 12] };
  await files.checkDisk();
  assert.equal(module.made[0].text(), "from HIMARI");
  assert.equal(files.dirty, false);
  module.made[0].type("mine");
  disk = { ...TEXT, text: "again", identity: [1, 2, 13, 14] };
  await files.refresh(); // the turn ended
  assert.equal(module.made[0].text(), "mine");
  assert.equal(files.doc.bar.hidden, false);
  files.closeDoc();
}

// Unsaved text is not dropped unasked: a "no" keeps the editor; a "yes" goes.
{
  const { files, asked, module } = editorPane({
    "GET /api/machine/file": TEXT,
    "GET /api/machine/list": listing("/home/u", ["a.txt"]),
  }, { confirms: [false, true] });
  await files.openFile(TEXT.path);
  module.made[0].type("unsaved");
  await files.openFolder("/home/u");
  assert.equal(files.file, TEXT.path);
  assert.equal(module.made[0].destroyed, false);
  await files.openFolder("/home/u");
  assert.equal(files.folder, "/home/u");
  assert.equal(files.doc, null);
  assert.equal(module.made[0].destroyed, true);
  assert.deepEqual(asked.map(([, action]) => action), ["Discard", "Discard"]);
}

// Over 1 MiB, or not whole: shown read-only, no editor.
{
  const { files, module } = editorPane({ "GET /api/machine/file": { ...TEXT, size: 2_000_000, editable: false, truncated: true } });
  await files.openFile(TEXT.path);
  assert.equal(module.made.length, 0);
  assert.equal(files.doc, null);
}

// Delete: one question, or two for a file no previous version is kept of; sent with the identity seen.
for (const [size, questions] of [[3, 1], [2_000_000, 2]]) {
  const { files, sent, asked } = editorPane({
    "GET /api/machine/file": TEXT,
    "GET /api/machine/stat": { path: TEXT.path, kind: "file", identity: [5, 5, size, 5], size },
    "POST /api/machine/file/delete": { path: TEXT.path, previous_kept: size < 1024 * 1024 },
    "GET /api/machine/list": listing("/home/u", []),
  });
  await files.openFile(TEXT.path);
  await files.deleteFile();
  assert.equal(asked.length, questions);
  assert.deepEqual(sent.find(([, p]) => p === "/api/machine/file/delete")[2], { path: TEXT.path, expected: [5, 5, size, 5] });
  assert.equal(files.folder, "/home/u");
}

// A new file is made empty on the server and opened in the editor.
{
  const { files, sent, module } = editorPane({
    "GET /api/machine/list": listing("/home/u", []),
    "POST /api/machine/file": { path: "/home/u/new.md", identity: [3, 3, 0, 3] },
    "GET /api/machine/file": { ...TEXT, path: "/home/u/new.md", text: "", size: 0, identity: [3, 3, 0, 3] },
  });
  await files.openFolder("/home/u");
  files.askName("/home/u", "file");
  const form = files.body.children[0];
  form.children[0].value = "new.md";
  await form.listeners.submit[0]({ preventDefault() {} });
  assert.deepEqual(sent.find(([m, p]) => m === "POST" && p === "/api/machine/file")[2], { path: "/home/u/new.md" });
  assert.equal(files.file, "/home/u/new.md");
  assert.equal(module.made.length, 1);
  files.closeDoc();
}

// PR #95 review B1: typing while the server's version is being read keeps the typing; the user is told.
{
  let disk = { ...TEXT };
  const reading = deferred();
  let reads = 0;
  const { files, module } = editorPane({
    "GET /api/machine/file": () => { reads += 1; return reads === 1 ? disk : reading.promise; },
    "GET /api/machine/stat": () => ({ path: TEXT.path, kind: "file", identity: disk.identity, size: disk.size }),
  });
  await files.openFile(TEXT.path);
  disk = { ...TEXT, text: "from HIMARI", identity: [1, 2, 11, 12] };
  const checking = files.checkDisk();
  for (let i = 0; i < 20 && reads < 2; i++) await settle(); // its read of the file is under way
  module.made[0].type("typed meanwhile");
  reading.resolve(disk);
  await checking;
  assert.equal(module.made[0].text(), "typed meanwhile");
  assert.equal(files.dirty, true);
  assert.equal(files.doc.bar.hidden, false);
  files.closeDoc();
}

// PR #95 review B2: a delete whose content was not kept says so where the user lands.
{
  const { files } = editorPane({
    "GET /api/machine/file": TEXT,
    "GET /api/machine/stat": { path: TEXT.path, kind: "file", identity: [5, 5, 3, 5], size: 3 },
    "POST /api/machine/file/delete": { path: TEXT.path, previous_kept: false },
    "GET /api/machine/list": listing("/home/u", ["b.txt"]),
  });
  await files.openFile(TEXT.path);
  await files.deleteFile();
  assert.equal(files.folder, "/home/u");
  assert.ok(files.body.text().includes("could not be kept"));
}

// PR #95 review B3: a file deleted from a folder is found there, and put back as a new file with its content
// (V3-P76: where the file system cannot put it back in one step).
{
  const { files, sent, module } = editorPane({
    "POST /api/machine/versions/v1/restore": () => { throw refusal("unsupported"); },
    "GET /api/machine/list": listing("/home/u", []),
    "GET /api/machine/versions/deleted": { items: [{ path: "/home/u/gone.txt", name: "gone.txt", id: "v1",
      saved_at: "2026-10-25T00:00:00+00:00", size: 4 }] },
    "GET /api/machine/versions/v1": { text: "kept" },
    "POST /api/machine/file": { path: "/home/u/gone.txt", identity: ["8", "8", "0", "8"] },
    "PUT /api/machine/file": { path: "/home/u/gone.txt", identity: ["8", "8", "4", "9"], previous_kept: true },
    "GET /api/machine/file": { ...TEXT, path: "/home/u/gone.txt", text: "kept", size: 4, identity: ["8", "8", "4", "9"] },
  });
  await files.openFolder("/home/u");
  await files.showDeleted("/home/u");
  const dialog = files.root.children.find((c) => c.tagName === "dialog");
  const restore = dialog.children[1].children[0].children.find((c) => c.textContent === "Restore");
  await restore.listeners.click[0]();
  const calls = sent.map(([m, p]) => `${m} ${p.split("?")[0]}`);
  assert.deepEqual(calls.slice(-4), ["GET /api/machine/versions/v1", "POST /api/machine/file", "PUT /api/machine/file",
                                     "GET /api/machine/file"]);
  assert.deepEqual(sent.find(([m]) => m === "PUT")[2], { path: "/home/u/gone.txt", content: "kept", expected: ["8", "8", "0", "8"] });
  assert.equal(files.file, "/home/u/gone.txt");
  assert.equal(module.made[0].text(), "kept");
  files.closeDoc();
}

// PR #95 re-review C1: a restore that ends after another file was opened and edited leaves that file be.
{
  const slow = deferred();
  const { files, sent, module } = editorPane({
    "POST /api/machine/versions/v1/restore": () => { throw refusal("unsupported"); },
    "GET /api/machine/versions/v1": () => slow.promise,
    "POST /api/machine/file": { path: "/home/u/gone.txt", identity: ["8", "8", "0", "8"] },
    "PUT /api/machine/file": { path: "/home/u/gone.txt", identity: ["8", "8", "4", "9"], previous_kept: true },
    "GET /api/machine/file": TEXT,
  });
  const item = { path: "/home/u/gone.txt", name: "gone.txt", id: "v1" };
  const restoring = files.restoreDeleted(item);
  await files.openFile(TEXT.path); // the user goes on meanwhile
  module.made[0].type("mine, unsaved");
  slow.resolve({ text: "kept" });
  assert.equal(await restoring, null);
  assert.equal(files.file, TEXT.path);
  assert.equal(module.made[0].text(), "mine, unsaved");
  assert.equal(module.made[0].destroyed, false);
  assert.ok(files.body.text().includes("gone.txt is restored"));
  assert.equal(sent.filter(([m, p]) => m === "GET" && p.startsWith("/api/machine/file?path=%2Fhome%2Fu%2Fgone")).length, 0);
  files.closeDoc();
}

// PR #95 re-review C2: a restore stopped after its empty file was made fills that same file when tried again,
// while it is still that file; a file made by anyone else meanwhile is never written.
{
  let puts = 0;
  let onDisk = ["8", "8", "0", "8"];
  const { files, sent } = editorPane({
    "POST /api/machine/versions/v1/restore": () => { throw refusal("unsupported"); },
    "GET /api/machine/versions/v1": { text: "kept" },
    "POST /api/machine/file": { path: "/home/u/gone.txt", identity: ["8", "8", "0", "8"] },
    "GET /api/machine/stat": () => ({ path: "/home/u/gone.txt", kind: "file", identity: onDisk, size: 0 }),
    "PUT /api/machine/file": () => { puts += 1; if (puts === 1) throw Object.assign(new Error("disk full"), { code: "other" });
      return { path: "/home/u/gone.txt", identity: ["8", "8", "4", "9"], previous_kept: true }; },
    "GET /api/machine/file": { ...TEXT, path: "/home/u/gone.txt", text: "kept" },
  });
  const item = { path: "/home/u/gone.txt", name: "gone.txt", id: "v1" };
  const first = await files.restoreDeleted(item);
  assert.ok(first.includes("Restore again"));
  assert.deepEqual(item.made.identity, ["8", "8", "0", "8"]);
  assert.equal(await files.restoreDeleted(item), null);
  assert.equal(sent.filter(([m, p]) => m === "POST" && p === "/api/machine/file").length, 1); // not made twice
  assert.equal(sent.filter(([m, p]) => p.endsWith("/restore")).length, 1); // the second try fills its own file
  assert.deepEqual(sent.filter(([m]) => m === "PUT").map(([, , b]) => b.expected), [["8", "8", "0", "8"], ["8", "8", "0", "8"]]);
  assert.equal(item.made, null);
  files.closeDoc();
  // Changed by someone else since: not that file any more; a new one is asked for (and refused as taken).
  const other = { path: "/home/u/gone.txt", name: "gone.txt", id: "v1", made: { path: "/home/u/gone.txt", identity: ["1", "1", "0", "1"] } };
  onDisk = ["2", "2", "5", "2"];
  const routes = editorPane({
    "GET /api/machine/versions/v1": { text: "kept" },
    "GET /api/machine/stat": () => ({ path: "/home/u/gone.txt", kind: "file", identity: onDisk, size: 5 }),
    "POST /api/machine/file": () => { throw Object.assign(new Error("exists"), { code: "exists" }); },
  });
  const answer = await routes.files.restoreDeleted(other);
  assert.ok(answer.includes("exists again"));
  assert.equal(routes.sent.filter(([m]) => m === "PUT").length, 0);
}

// V3-P76 §4.1: a deleted file put back in one step, never over a name taken since.
{
  const { files, sent, module } = editorPane({
    "POST /api/machine/versions/v1/restore": { path: "/home/u/gone.txt" },
    "GET /api/machine/file": { ...TEXT, path: "/home/u/gone.txt", text: "kept" },
  });
  assert.equal(await files.restoreDeleted({ path: "/home/u/gone.txt", name: "gone.txt", id: "v1" }), null);
  assert.deepEqual(sent.map(([m, p]) => `${m} ${p.split("?")[0]}`),
                   ["POST /api/machine/versions/v1/restore", "GET /api/machine/stat", "GET /api/machine/file"]);
  assert.deepEqual(sent[0][2], { path: "/home/u/gone.txt" });
  assert.equal(files.file, "/home/u/gone.txt");
  assert.equal(module.made[0].text(), "kept");
  files.closeDoc();
  const taken = editorPane({ "POST /api/machine/versions/v1/restore": () => { throw refusal("changed"); } });
  const answer = await taken.files.restoreDeleted({ path: "/home/u/gone.txt", name: "gone.txt", id: "v1" });
  assert.ok(answer.includes("exists again"));
  assert.equal(taken.sent.length, 1);
}

// PR #95 re-review C1: a new file made after the user moved on is not opened over their editing.
{
  const slow = deferred();
  const { files, module } = editorPane({
    "GET /api/machine/list": listing("/home/u", []),
    "POST /api/machine/file": () => slow.promise,
    "GET /api/machine/file": TEXT,
  });
  await files.openFolder("/home/u");
  files.askName("/home/u", "file");
  const form = files.body.children[0];
  form.children[0].value = "new.md";
  const submitting = form.listeners.submit[0]({ preventDefault() {} });
  await files.openFile(TEXT.path);
  module.made[0].type("editing");
  slow.resolve({ path: "/home/u/new.md", identity: ["3", "3", "0", "3"] });
  await submitting;
  assert.equal(files.file, TEXT.path);
  assert.equal(module.made[0].text(), "editing");
  assert.ok(files.body.text().includes("new.md is made"));
  files.closeDoc();
}

// V3-P76 §3.1: Ask HIMARI — a selection as positions and its text's hash, never the text; with none,
// the whole file; unsaved text is not pointed at.
{
  const asked = [];
  const { files, module } = editorPane({ "GET /api/machine/file": { ...TEXT, text: "one two" } },
                                       { onAsk: (reference) => asked.push(reference) });
  await files.openFile(TEXT.path);
  const editor = module.made[0];
  assert.equal(typeof editor.opts.onAsk, "function");
  editor.picked = [4, 7];
  await editor.opts.onAsk();
  assert.deepEqual(asked.pop(), { path: TEXT.path, name: "a.txt", identity: [1, 2, 3, 4], whole: false, start: 4, end: 7,
    sha256: "3fc4ccfe745870e2c0d99f71f30ff0656c8dedd41cc1d7d3d376b0dbe685e2f3", lines: [1, 5, 1, 8] });
  editor.picked = [2, 2];
  await files.ask();
  assert.deepEqual(asked.pop(), { path: TEXT.path, name: "a.txt", identity: [1, 2, 3, 4], whole: true });
  editor.type("one two three");
  editor.picked = [0, 3];
  await files.ask();
  assert.equal(asked.length, 0);
  assert.ok(files.stateLabel.textContent.startsWith("Save first"));
  files.closeDoc();
}

// Ask from the ⋯ menu: the whole file, any kind; offered only where the place takes references.
{
  const asked = [];
  const image = { path: "/home/u/p.png", size: 9, identity: ["5", "6", "9", "7"], modified: 0, kind: "image",
                  text: null, truncated: false, editable: false };
  const { files } = editorPane({ "GET /api/machine/file": image }, { onAsk: (reference) => asked.push(reference) });
  await files.openFile(image.path);
  const menu = files.tools.children.find((c) => c.className.includes("files-file-menu"));
  const item = menu.children[1].children.find((c) => c.textContent === "Ask HIMARI about this file");
  item.listeners.click[0]();
  assert.deepEqual(asked, [{ path: image.path, name: "p.png", identity: ["5", "6", "9", "7"], whole: true }]);
  const plain = editorPane({ "GET /api/machine/file": TEXT });
  await plain.files.openFile(TEXT.path);
  assert.equal(plain.module.made[0].opts.onAsk, null);
  const plainMenu = plain.files.tools.children.find((c) => c.className.includes("files-file-menu"));
  assert.ok(!plainMenu.children[1].children.some((c) => c.textContent === "Ask HIMARI about this file"));
  plain.files.closeDoc();
}

// A reference's place: the file opened (once), its selection selected.
{
  const { files, sent, module } = editorPane({ "GET /api/machine/file": { ...TEXT, text: "one two" } });
  await files.reveal(TEXT.path, 4, 7);
  assert.deepEqual(module.made[0].picked, [4, 7]);
  await files.reveal(TEXT.path, 0, 3);
  assert.equal(sent.filter(([m, path]) => m === "GET" && path.startsWith("/api/machine/file")).length, 1);
  assert.deepEqual(module.made[0].picked, [0, 3]);
  files.closeDoc();
}

// --- HIMARI's changes (V3-P76 §5, §6) -----------------------------------------------------------------

/* A fake conversation's review: ``list`` and ``diffs`` as the server would answer; every call kept, with the
 * conversation it named last. ``source.shown``: the conversation shown now; ``gate``: a promise each list and
 * diff waits for first. */
function changesSource(list, diffs = {}, { revert = () => ({ reverted: true }),
                                           revertAll = () => ({ left: [], hidden: 0 }) } = {}) {
  const calls = [];
  const told = []; // each list read, as the pane tells its place
  const source = {
    calls, told, shown: "c1", gate: null,
    of: () => source.shown,
    list: async (of) => { calls.push(["list", of]); await source.gate; return list; },
    diff: async (of, path) => { calls.push(["diff", path, of]); await source.gate; return diffs[path]; },
    accept: async (of, paths) => { calls.push(["accept", paths, of]); return { accepted: true }; },
    revert: async (of, path, seenState) => { calls.push(["revert", path, seenState, of]); return revert(path, seenState); },
    revertAll: async (of) => { calls.push(["revertAll", of]); return revertAll(); },
    seen: (given) => told.push(given),
  };
  return source;
}

const CHANGED = { path: "/home/u/a.txt", name: "a.txt", kind: "changed", state: "ok", interleaved: false, count: 2,
                  revertible: true, changed_at: "2026-10-25T00:00:00+00:00" };
const STUCK = { ...CHANGED, path: "/home/u/big.bin", name: "big.bin", state: "no_version", count: 1, revertible: false };
const LIST = { items: [STUCK, CHANGED], hidden: 1, commands_ran: "2026-10-25T00:00:00+00:00", running: false };
const DIFF = { path: "/home/u/a.txt", name: "a.txt", kind: "changed", state: "ok", revertible: true, changed: false,
               interleaved: false, before: { state: "present", text: "A" },
               now: { exists: true, identity: ["1", "2", "3", "4"], text: "B" } };

// The list: what each change is, the ones that cannot be reverted marked, commands and hidden ones told.
{
  const source = changesSource(LIST);
  const { files } = editorPane({}, { changes: source });
  await files.openChanges();
  assert.equal(files.page, "changes");
  assert.deepEqual(source.told, [LIST]);
  const rows = files.body.children.find((c) => c.className.includes("files-changes")).children;
  assert.deepEqual(rows.map((r) => r.className), ["files-row files-change is-warn", "files-row files-change"]);
  assert.ok(rows[0].text().includes("its previous version could not be kept"));
  assert.ok(rows[1].text().includes("changed by HIMARI · 2 times"));
  const button = (row, label) => row.children.find((c) => c.textContent === label);
  assert.equal(button(rows[0], "Revert").disabled, true);
  assert.equal(button(rows[1], "Revert").disabled, false);
  const text = files.body.text();
  assert.ok(text.includes("also ran commands") && text.includes("1 change is in files hidden now"));
  assert.deepEqual(files.tools.children.map((c) => [c.textContent, c.disabled]), [["Accept all", false], ["Revert all", false]]);
  // Accepting one: then the list again, told so.
  await button(rows[1], "Accept").listeners.click[0]();
  assert.deepEqual(source.calls.find(([k]) => k === "accept"), ["accept", ["/home/u/a.txt"], "c1"]);
  assert.ok(files.body.text().includes("a.txt: accepted."));
}

// While HIMARI works: nothing can be accepted or reverted.
{
  const { files } = editorPane({}, { changes: changesSource({ ...LIST, running: true }) });
  await files.openChanges();
  assert.ok(files.tools.children.every((c) => c.disabled));
  const rows = files.body.children.find((c) => c.className.includes("files-changes")).children;
  assert.ok(rows.every((row) => row.children.filter((c) => c.tagName === "button" && c.className === "link-button").every((c) => c.disabled)));
  assert.ok(files.body.text().includes("HIMARI is working"));
}

// A change: before (left) and now (right); a revert refused as changed since shows it, to revert anyway as seen.
{
  const changedSince = { ...DIFF, changed: true, now: { exists: true, identity: ["9", "9", "9", "9"], text: "owner's" } };
  let reverts = 0;
  const source = changesSource(LIST, { "/home/u/a.txt": changedSince }, {
    revert: (path, seenState) => { reverts += 1; if (!seenState) throw refusal("changed"); return { reverted: true }; } });
  const { files, module, asked } = editorPane({}, { changes: source });
  await files.openChanges();
  const row = files.body.children.find((c) => c.className.includes("files-changes")).children[1];
  await row.children.find((c) => c.textContent === "Revert").listeners.click[0]();
  assert.deepEqual(files.page, { path: "/home/u/a.txt" });
  assert.deepEqual(module.made.at(-1).diff, { left: "A", right: "owner's", name: "a.txt" });
  const revert = files.tools.children.find((c) => c.textContent === "Revert");
  assert.equal(revert.disabled, true); // only "anyway", in the bar
  const bar = files.body.children.find((c) => c.className === "files-bar");
  assert.ok(bar.text().includes("changed by something other than this conversation's HIMARI"));
  await bar.children.find((c) => c.textContent === "Revert anyway").listeners.click[0]();
  assert.deepEqual(asked.at(-1), ["Revert a.txt anyway?", "Revert anyway"]);
  assert.deepEqual(source.calls.filter(([k]) => k === "revert").map(([, , s, of]) => [s, of]),
                   [[null, "c1"], [{ exists: true, identity: ["9", "9", "9", "9"], text: "owner's" }, "c1"]]);
  assert.equal(files.page, "changes");
  assert.ok(files.body.text().includes("a.txt is back as it was"));
  // Made by HIMARI and deleted since: told in words, the diff against nothing.
  const made = changesSource(LIST, { "/home/u/n.txt": { ...DIFF, path: "/home/u/n.txt", name: "n.txt", kind: "created",
    before: { state: "absent", text: null }, now: { exists: false, identity: null, text: null }, changed: true } });
  const shown = editorPane({}, { changes: made });
  await shown.files.openChange("/home/u/n.txt");
  const words = shown.files.body.text();
  assert.ok(words.includes("there was no such file (HIMARI made it)") && words.includes("Now: there is no such file"));
  assert.ok(!shown.files.tools.children.some((c) => c.textContent === "Open"));
}

// Revert all: asked first; the ones left are named, the hidden ones counted (PR #98 review C3).
{
  const source = changesSource(LIST, {}, { revertAll: () => ({ left: [
    { path: "/home/u/big.bin", code: "not_revertible", message: "Its previous version could not be kept…" },
    { path: "/home/u/a.txt", code: "changed", message: "" }], hidden: 1 }) });
  const { files, asked } = editorPane({}, { changes: source, confirms: [false, true] });
  await files.openChanges();
  await files.tools.children.find((c) => c.textContent === "Revert all").listeners.click[0]();
  assert.equal(source.calls.filter(([k]) => k === "revertAll").length, 0); // "no": nothing done
  await files.tools.children.find((c) => c.textContent === "Revert all").listeners.click[0]();
  assert.deepEqual(asked.map(([title]) => title), ["Revert 1 file?", "Revert 1 file?"]);
  assert.ok(files.body.text().includes(
    "Reverted all but big.bin (cannot be reverted), a.txt (changed since), 1 in a file hidden now."));
}

// A list that changed under the owner (PR #98 re-review): refused as outdated, the list again, told why.
{
  const source = changesSource(LIST);
  source.accept = async (of, paths) => {
    source.calls.push(["accept", paths, of]);
    throw Object.assign(new Error("Some of HIMARI's changes could not be recorded and are now marked unknown: look at the list again."),
                        { code: "outdated" });
  };
  const { files } = editorPane({}, { changes: source });
  await files.openChanges();
  await files.tools.children.find((c) => c.textContent === "Accept all").listeners.click[0]();
  assert.equal(source.calls.filter(([k]) => k === "list").length, 2);
  assert.equal(files.page, "changes");
  assert.ok(files.body.children[0].textContent.includes("now marked unknown"));
}

// Another conversation shown meanwhile (PR #99 review C2): an answer for the last one is not shown, and a page
// of its changes acts on nothing.
{
  let open;
  const source = changesSource(LIST, { "/home/u/a.txt": DIFF });
  const { files } = editorPane({}, { changes: source });
  source.gate = new Promise((resolve) => { open = resolve; });
  const listing = files.openChanges();
  await Promise.resolve();
  source.shown = "c2"; // switched, the pane not told yet
  open();
  await listing;
  assert.equal(files.page, null);
  assert.deepEqual(source.told, []);
  source.gate = new Promise((resolve) => { open = resolve; });
  const diffing = files.openChange("/home/u/a.txt");
  await Promise.resolve();
  source.shown = "c3";
  files.reset(); // as the app does at the switch
  open();
  await diffing;
  assert.equal(files.page, null);
  assert.equal(files.body.children.length, 0);
  // A page shown for c3, then c4 shown without a reset: its controls reach nothing.
  source.gate = null;
  await files.openChange("/home/u/a.txt");
  source.shown = "c4";
  await files.tools.children.find((c) => c.textContent === "Accept").listeners.click[0]();
  await files.tools.children.find((c) => c.textContent === "Revert").listeners.click[0]();
  assert.ok(!source.calls.some(([k]) => k === "accept" || k === "revert"));
  assert.deepEqual(source.calls.filter(([k]) => k !== "diff").map(([k, of]) => [k, of]), [["list", "c1"]]);
  assert.deepEqual(source.calls.filter(([k]) => k === "diff").map(([, , of]) => of), ["c2", "c3"]);
  files.reset();
  assert.equal(files.page, null);
  assert.equal(files.tools.children.length, 0);
  // Away and back before the answer came: the same conversation, but not asked for since.
  source.gate = new Promise((resolve) => { open = resolve; });
  const again = files.openChanges();
  await Promise.resolve();
  source.shown = "c5";
  files.reset();
  source.shown = "c4";
  files.reset();
  open();
  await again;
  assert.equal(files.page, null);
}

// One change's page while HIMARI works (PR #99 review C4): its controls off, and told; one that starts meanwhile
// turns them off, and a click that was on its way does nothing.
{
  let working = true;
  const changedSince = { ...DIFF, changed: true, now: { exists: true, identity: ["9", "9", "9", "9"], text: "owner's" } };
  const source = changesSource(LIST, { "/home/u/a.txt": changedSince });
  const { files } = editorPane({}, { changes: source, working: () => working });
  await files.openChange("/home/u/a.txt");
  const controls = () => files.reviewControls;
  assert.deepEqual(controls().map((c) => [c.textContent, c.disabled]),
                   [["Revert", true], ["Accept", true], ["Revert anyway", true]]);
  assert.ok(files.body.text().includes("HIMARI is working: accepting and reverting wait"));
  working = false;
  await files.openChange("/home/u/a.txt");
  assert.deepEqual(controls().map((c) => [c.textContent, c.disabled]),
                   [["Revert", true], ["Accept", false], ["Revert anyway", false]]);
  working = true;
  files.workingChanged(); // a turn started
  assert.ok(controls().every((c) => c.disabled));
  await controls()[1].listeners.click[0]();
  assert.ok(!source.calls.some(([k]) => k === "accept"));
  assert.ok(files.body.children[0].textContent.startsWith("HIMARI is working"));
  // The list's too.
  working = false;
  await files.openChanges();
  working = true;
  files.workingChanged();
  assert.ok(files.tools.children.every((c) => c.disabled));
}

// The dots: on a changed file's row and its folders' rows; the open file links to its change.
{
  const { files } = editorPane({ "GET /api/machine/file": TEXT, "GET /api/machine/stat": HOME },
                               { changes: changesSource(LIST, { "/home/u/a.txt": DIFF }) });
  files.markChanged({ items: [CHANGED] });
  const dotted = (row) => row.children[0].children[1].children.some((c) => c.className === "files-changed-dot");
  assert.ok(dotted(files.row("/home/u", { name: "a.txt", kind: "file", size: 1, modified: 0 })));
  assert.ok(dotted(files.row("/home", { name: "u", kind: "directory", size: null, modified: 0 })));
  assert.ok(!dotted(files.row("/home/u", { name: "b.txt", kind: "file", size: 1, modified: 0 })));
  await files.openFile(TEXT.path);
  const link = files.crumbs.children.at(-1).children.find((c) => c.className.includes("files-changed-link"));
  await link.listeners.click[0]();
  assert.deepEqual(files.page, { path: "/home/u/a.txt" });
  assert.equal(files.doc, null); // the editor closed for the change's diff
  files.markChanged(null);
  assert.ok(!dotted(files.row("/home/u", { name: "a.txt", kind: "file", size: 1, modified: 0 })));
  // Without a review here (the Files surface): no link.
  const plain = editorPane({ "GET /api/machine/file": TEXT });
  plain.files.markChanged({ items: [CHANGED] });
  await plain.files.openFile(TEXT.path);
  assert.ok(!plain.files.crumbs.children.at(-1).children.length);
  plain.files.closeDoc();
}

/* V3-P77 §3: a PDF opens in the PDF view, whose module and bytes the test hands out. */
/* A stroke as the view carries it (pdfink.js's, page coordinates). */
const STROKE = { name: "himari-ink-aa", page: 1, tool: "pen", color: "black", rgb: [0, 0, 0], size: 1, pressure: false,
                 points: [[1, 2, 0.5], [3, 4, 0.5]], saved: null };

function pdfPane(routes, { fail = null, confirms = null, appended = null, answer = null, failing = new Set(),
                           keep = null } = {}) {
  const views = [];
  const reads = [];
  const sent = [];
  const saves = [];
  const module = {
    loadPdfjs: async () => ({ fake: true }),
    strokeFromSaved,
    PdfView: class {
      constructor({ pdfjs, bytes, onPage, onInk, onNotice }) {
        Object.assign(this, { pdfjs, bytes, onPage, onInk, onNotice, current: 0, destroyed: false, went: null,
                              writable: true, readOnly: null, tool: null, frozen: false, cut: null, next: null,
                              savedWith: null, carried: null, left: 0, file: { size: 2048 } });
        // The ink as the pane sees it: dirty or not, undo and redo; what a conflict or the browser carries.
        this.ink = { dirty: false, done: [], undone: [] };
        this.carrying = { add: [STROKE], erase: new Set(), erasePages: [], refs: new Map() };
        this.element = el("div", "pdf-view");
        views.push(this);
      }
      show(page) { this.current = page - 1; if (page > 1) this.went = page; }
      setTool(tool) { this.tool = tool; }
      undo() {}
      redo() {}
      carry() { return this.carrying; }
      async withInk() { return Uint8Array.of(1, 2, 3); }
      // The next save, as pdfpane.js makes it: {bytes, changes, added}, or null for nothing to save.
      async increment() { return this.next; }
      async saved(made) { this.savedWith = made; this.ink.dirty = false; }
      async carryIn(carried) {
        this.carried = carried;
        if (carried.add.length || carried.erase.size) this.ink.dirty = true; // unsaved here, as pdfpane.js's
        return this.left;
      }
      get numPages() { return 3; }
      async open() {
        if (fail) throw Object.assign(new Error(fail), { code: "password" });
        if (failing.has(views.indexOf(this))) throw Object.assign(new Error("This PDF could not be opened here."),
                                                                   { code: "unreadable" });
        this.onPage(1, 3);
      }
      goTo(number) { this.went = number; this.current = number - 1; this.onPage(number, 3); }
      destroy() { this.destroyed = true; }
    },
  };
  const api = async (method, path, body) => {
    if (path === "/api/machine/stat?path=~") return HOME;
    sent.push([method, path, body]);
    const key = Object.keys(routes).find((k) => `${method} ${path}`.startsWith(k));
    assert.ok(key, `no route for ${method} ${path}`);
    const answer = routes[key];
    return typeof answer === "function" ? answer(path, body) : answer;
  };
  const pending = [];
  const readFile = (path, limit) => {
    assert.equal(limit, 64 * 1024 * 1024, "the view's limit goes with the read");
    reads.push(path);
    const answer = deferred();
    pending.push(answer);
    return answer.promise;
  };
  const appendPdf = async (path, increment, identity) => {
    if (appended) appended.push([path, Array.from(increment), identity]);
    const given = typeof answer === "function" ? await answer() : answer;
    if (given instanceof Error) throw given;
    return given || { path, identity: ["9", "9", "9", "9"] };
  };
  const files = new FilesPane({ api, el, loadPdf: async () => module, readFile, appendPdf, keep, writer: "me",
    onSaved: (saved) => saves.push(saved),
    confirm: async (title, lines, action) => { if (confirms) confirms.push([title, lines, action]); return true; } });
  return { files, views, reads, pending, sent, saves };
}

const PDF = { path: "/home/u/notes.pdf", size: 2048, identity: [1, 2, 3, 4], modified: 0, kind: "pdf", text: null,
              truncated: false, editable: false };
const pageLabel = (files) => files.tools.children.find((c) => (c.className || "").includes("files-page"));

{
  const { files, views, reads, pending } = pdfPane({ "GET /api/machine/file": PDF });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  assert.deepEqual(reads, [PDF.path]);
  assert.ok(pageLabel(files).hidden, "no page shown before it is open");
  pending[0].resolve({ bytes: new Uint8Array([37, 80, 68, 70]), identity: null });
  await opening;
  assert.equal(views.length, 1);
  assert.deepEqual(Array.from(views[0].bytes), [37, 80, 68, 70]);
  assert.deepEqual(views[0].pdfjs, { fake: true });
  assert.ok(files.body.children.includes(views[0].element));
  assert.equal(pageLabel(files).textContent, "1 / 3");
  assert.ok(!pageLabel(files).hidden);
  // Opening something else lets the view go.
  files.closeDoc();
  assert.ok(views[0].destroyed);
  assert.equal(files.pdf, null);
}

// Larger than the view takes (64 MiB): its size and a download, nothing read.
{
  const large = { ...PDF, size: 64 * 1024 * 1024 + 1 };
  const { files, views, reads } = pdfPane({ "GET /api/machine/file": large });
  await files.openFile(PDF.path);
  assert.equal(reads.length, 0);
  assert.equal(views.length, 0);
  assert.ok(files.body.text().includes("too large to view here") && files.body.text().includes("Download"));
  assert.equal(pageLabel(files), undefined);
}

// Bytes arriving after another place was opened are not shown.
{
  const { files, views, pending } = pdfPane({ "GET /api/machine/file": PDF, "GET /api/machine/list": listing("/home/u", ["x"]) });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  await files.openFolder("/home/u");
  pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  assert.ok(views.every((view) => view.destroyed), "opened late, let go unseen");
  assert.ok(!views.some((view) => files.body.children.includes(view.element)));
  assert.ok(files.body.text().includes("x"));
}

// Grown past the limit since its size was told: the read is refused, and the pane says so with its download.
{
  const { files, views, pending } = pdfPane({ "GET /api/machine/file": PDF });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  pending[0].reject(Object.assign(new Error("This file is now too large to view here (at most 64 MB)."), { status: 413 }));
  await opening;
  assert.equal(views.length, 0);
  assert.ok(files.body.text().includes("now too large to view here") && files.body.text().includes("Download"));
}

// A PDF that needs a password: told so, with its download; the view let go.
{
  const message = "This PDF is password-protected; download it to open.";
  const { files, views, pending } = pdfPane({ "GET /api/machine/file": PDF }, { fail: message });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  assert.ok(views[0].destroyed);
  assert.ok(files.body.text().includes(message) && files.body.text().includes("Download"));
}

// After HIMARI's turn: an unchanged PDF is not read again; a changed one is, on the page it was at.
{
  let identity = PDF.identity;
  const { files, views, reads, pending } = pdfPane({
    "GET /api/machine/file": () => ({ ...PDF, identity }),
    "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", identity }),
  });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  views[0].goTo(3);
  await files.refresh();
  assert.equal(reads.length, 1);
  assert.ok(!views[0].destroyed);
  identity = [1, 2, 9, 9];
  const again = files.refresh();
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  pending[1].resolve({ bytes: new Uint8Array(4), identity: null });
  await again;
  assert.equal(reads.length, 2);
  assert.ok(views[0].destroyed);
  assert.equal(views[1].went, 3);
  assert.equal(pageLabel(files).textContent, "3 / 3");
  assert.deepEqual(files.pdf.identity, [1, 2, 9, 9]);
  files.closeDoc(); // its check on disk stops with it
}

// PR #103 review P3: a PDF over 1 MiB (up to 64 MiB) is deleted with its content kept: told so, asked once.
{
  const big = { ...PDF, size: 3 * 1024 * 1024 };
  const confirms = [];
  const sent = [];
  const { files, pending } = pdfPane({
    "GET /api/machine/file": big,
    "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", size: big.size, identity: big.identity }),
    "POST /api/machine/file/delete": () => { sent.push("delete"); return { path: PDF.path, previous_kept: true }; },
    "GET /api/machine/list": listing("/home/u", []),
  }, { confirms });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  await files.deleteFile();
  assert.equal(confirms.length, 1, "no second warning that it cannot be put back");
  assert.match(confirms[0][1].join(" "), /kept as a previous version/);
  assert.deepEqual(sent, ["delete"]);
  assert.equal(files.pdf, null);
}

/* V3-P77 §4, §5: ink on an open PDF — the tools, a save against the identity read, a change on the server. */
async function openPdf(routes, options = {}) {
  const made = pdfPane(routes, options);
  const opening = made.files.openFile(PDF.path);
  for (let i = 0; i < 20 && !made.pending.length; i++) await settle();
  made.pending[0].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "5"] });
  await opening;
  return made;
}

{
  const appended = [];
  const { files, views, saves } = await openPdf({ "GET /api/machine/file": PDF }, { appended });
  const view = views[0];
  const tools = files.ink.tools;
  assert.ok(!files.ink.group.hidden);
  await tools.pen.listeners.click[0]();
  assert.deepEqual(view.tool, { tool: "pen", color: "black", size: "fine" });
  assert.equal(tools.pen.attributes["aria-pressed"], "true");
  const red = files.ink.swatches.children.find((c) => c.dataset.color === "red");
  await red.listeners.click[0]();
  assert.deepEqual(view.tool, { tool: "pen", color: "red", size: "fine" });
  await tools.eraser.listeners.click[0]();
  assert.equal(view.tool.tool, "eraser");
  await tools.none.listeners.click[0]();
  assert.equal(view.tool, null);
  // V3-P82 §3.8: no Save button — the state only (Retry once a save failed); the save goes against the
  // identity of the bytes read, and the chips pointing at it follow (§6.3).
  assert.ok(files.ink.save.hidden);
  view.ink.dirty = true;
  view.onInk();
  assert.ok(files.ink.save.hidden && files.ink.state.textContent === "Unsaved");
  assert.ok(files.dirty);
  view.next = { bytes: Uint8Array.of(7, 8), changes: [], added: new Map() };
  assert.equal(await files.savePdf(), true);
  assert.deepEqual(appended, [[PDF.path, [7, 8], ["1", "2", "3", "5"]]]);
  assert.equal(view.savedWith, view.next, "what the save held, saved where it is in the view");
  assert.deepEqual(files.pdf.identity, ["9", "9", "9", "9"]);
  assert.deepEqual(saves, [{ path: PDF.path, from: ["1", "2", "3", "5"], to: ["9", "9", "9", "9"] }]);
  assert.equal(files.ink.state.textContent, "Saved");
  assert.ok(!files.dirty);
  files.closeDoc();
}

// V3-P82 §3.6: a save keeps no previous version (the file holds it), so it never says one was not kept.
{
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF },
                                         { answer: { path: PDF.path, identity: ["9"] } });
  views[0].ink.dirty = true;
  views[0].next = { bytes: Uint8Array.of(1), changes: [], added: new Map() };
  await files.savePdf();
  assert.equal(files.ink.state.textContent, "Saved");
  files.closeDoc();
}
// Refused as changed, the ink stays and the user picks.
{
  const confirms = [];
  const { files, views, pending, reads } = await openPdf({ "GET /api/machine/file": PDF }, {
    confirms, answer: Object.assign(new Error("changed since"), { code: "changed", status: 409 }) });
  views[0].ink.dirty = true;
  views[0].next = { bytes: Uint8Array.of(1), changes: [], added: new Map() };
  assert.equal(await files.savePdf(), false);
  const bar = files.pdf.bar;
  assert.ok(!bar.hidden && bar.text().includes("Not saved: this PDF was changed on the server"));
  // V3-P82 §3.7: no save on its own until the user picks; leaving cannot save, so it asks.
  assert.ok(files.pdf.waiting);
  files.scheduleSave(files.pdf);
  assert.equal(files.pdf.saveTimer, null);
  assert.equal(await files.savePdf(), false, "nothing sent while the user picks");
  assert.ok(await files.mayLeave());
  assert.deepEqual(confirms.map((c) => c[0]), ["Leave notes.pdf with ink not saved?"]);
  assert.match(confirms[0][1].join(" "), /kept in this browser/);
  const keep = bar.children.find((c) => c.textContent === "Load the server's version and keep my ink");
  const again = keep.listeners.click[0]();
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  pending[1].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "6"] });
  await again;
  assert.equal(reads.length, 2);
  assert.ok(views[0].destroyed);
  assert.deepEqual(views[1].carried.add, [STROKE], "the unsaved ink put on the server's version");
  assert.deepEqual(files.pdf.identity, ["1", "2", "3", "6"]);
  assert.ok(!files.pdf.waiting, "saved on its own again");
  files.closeDoc();
}

// Checked on disk: changed with no ink unsaved, read again unasked; with ink unsaved, told instead.
{
  let identity = ["1", "2", "3", "5"];
  const { files, views, pending, reads } = await openPdf({
    "GET /api/machine/file": PDF,
    "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", identity }),
  });
  identity = ["1", "2", "3", "7"];
  views[0].ink.dirty = true;
  await files.checkPdf();
  assert.equal(reads.length, 1);
  assert.ok(!files.pdf.bar.hidden && files.pdf.bar.text().includes("This PDF was changed on the server"));
  views[0].ink.dirty = false;
  const quiet = files.checkPdf();
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  pending[1].resolve({ bytes: new Uint8Array(4), identity });
  await quiet;
  assert.equal(reads.length, 2);
  assert.equal(views[1].carried, null);
  files.closeDoc();
}

// A PDF that cannot be written on (encrypted, its structure unread): viewed, its tools hidden, told why.
{
  const made = pdfPane({ "GET /api/machine/file": PDF });
  const real = made.files.loadPdf;
  made.files.loadPdf = async () => {
    const module = await real();
    return { ...module, PdfView: class extends module.PdfView {
      async open() { await super.open(); this.writable = false; this.readOnly = "This PDF is encrypted: it can be viewed here, not written on."; }
    } };
  };
  const opening = made.files.openFile(PDF.path);
  for (let i = 0; i < 20 && !made.pending.length; i++) await settle();
  made.pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  assert.ok(made.files.ink.tools.pen.hidden && made.files.ink.save.hidden);
  assert.equal(made.files.ink.state.textContent, "View only");
  assert.match(made.files.ink.state.title, /encrypted/);
  assert.deepEqual(made.files.pdf.identity, PDF.identity, "no identity told: the shown one");
  made.files.closeDoc();
}

// V3-P82 §3.4 (review C2): drawing goes on while a save is on its way — the view is never frozen by a save,
// one save at a time, and what changed meanwhile goes in the next. A save that cannot be made stops saving.
{
  const appended = [];
  let sending = deferred();
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF },
                                         { appended, answer: () => sending.promise });
  const view = views[0];
  view.ink.dirty = true;
  const made = deferred();
  view.increment = () => made.promise;
  const saving = files.savePdf();
  assert.equal(files.savePdf(), saving, "the save on its way, not a second");
  await settle();
  assert.ok(!view.frozen, "drawing goes on");
  made.resolve({ bytes: Uint8Array.of(1), changes: [], added: new Map() });
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(files.ink.state.textContent, "Saving…");
  view.saved = async (m) => { view.savedWith = m; }; // drawn meanwhile: still unsaved after it
  sending.resolve({ path: PDF.path, identity: ["9", "9", "9", "9"] });
  assert.equal(await saving, true);
  assert.equal(appended.length, 1);
  assert.ok(files.pdf.saveTimer !== null, "the rest waited for");
  assert.equal(files.ink.state.textContent, "Unsaved");
  // An increment that cannot be made: told, and no more saves on their own.
  view.increment = async () => { throw Object.assign(new Error("Not saved: the PDF could not be updated in place."),
                                                     { code: "unsaved" }); };
  assert.equal(await files.savePdf(), false);
  assert.equal(files.ink.state.textContent, "Not saved");
  assert.match(files.ink.state.title, /could not be updated in place/);
  assert.ok(files.ink.save.hidden, "nothing to retry");
  files.scheduleSave(files.pdf);
  assert.equal(files.pdf.saveTimer, null);
  files.closeDoc();
}

const SAVE = { bytes: Uint8Array.of(1), changes: [], added: new Map() };
const STAT = (identity) => () => ({ path: PDF.path, kind: "file", size: 2048, identity });

// V3-P82 §3.2, Q1: saved 5 s after the ink last changed — each change counts again — and at most 60 s after
// it first changed while it keeps changing (review N4).
{
  const clock = fakeClock();
  try {
    const appended = [];
    const holder = {}; // the disk as this tab left it
    const { files, views } = await openPdf({ "GET /api/machine/file": PDF,
      "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", identity: holder.files.pdf.identity }) },
      { appended });
    holder.files = files;
    const view = views[0];
    view.next = SAVE;
    view.ink.dirty = true;
    view.onInk();
    await clock.advance(4000);
    view.onInk(); // drawn again: 5 s from now
    await clock.advance(4000);
    assert.equal(appended.length, 0);
    await clock.advance(1000);
    assert.equal(appended.length, 1, "5 s after the last change");
    assert.equal(files.ink.state.textContent, "Saved");
    // Written on without a pause of 5 s: saved 60 s after the first change.
    view.ink.dirty = true;
    view.onInk();
    for (let t = 0; t < 56_000; t += 4000) {
      await clock.advance(4000);
      view.onInk();
    }
    assert.equal(appended.length, 1);
    await clock.advance(4000);
    assert.equal(appended.length, 2, "at most 60 s after it first changed");
    files.closeDoc();
  } finally {
    clock.restore();
  }
}

// Q5: a PDF in a cloud-synced folder is saved only when left (or hidden, or with Ctrl/Cmd+S) — never on its
// own; leaving saves it and asks nothing.
{
  const clock = fakeClock();
  try {
    const appended = [];
    const confirms = [];
    const { files, views } = await openPdf({ "GET /api/machine/file": { ...PDF, cloud_synced: true },
                                             "GET /api/machine/stat": STAT(["1", "2", "3", "5"]) },
                                           { appended, confirms });
    const view = views[0];
    view.next = SAVE;
    view.ink.dirty = true;
    view.onInk();
    assert.equal(files.ink.state.textContent, "Unsaved · saved when you leave");
    await clock.advance(120_000);
    assert.equal(appended.length, 0, "no save on its own");
    assert.equal(await files.mayLeave(), true);
    assert.equal(appended.length, 1, "saved on the way out");
    assert.deepEqual(confirms, [], "nothing asked");
    // Ctrl/Cmd+S saves now.
    view.ink.dirty = true;
    view.element.listeners.keydown[0]({ ctrlKey: true, key: "s", preventDefault() {} });
    await settle();
    assert.equal(appended.length, 2);
    files.closeDoc();
  } finally {
    clock.restore();
  }
}

// §3.7: a save lost on the way (network, 5xx) is tried again after 5 s, 30 s, 2 min — the file checked first:
// as expected, sent again; changed (ours landed though its answer was lost, or another wrote it), the user
// picks. Retry tries now.
{
  const clock = fakeClock();
  try {
    let identity = ["1", "2", "3", "5"];
    const answers = [];
    const appended = [];
    const { files, views, sent } = await openPdf({ "GET /api/machine/file": PDF,
                                                   "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", identity }) },
      { appended, answer: () => answers.shift() || { path: PDF.path, identity: ["9", "9", "9", "9"] } });
    const view = views[0];
    view.next = SAVE;
    view.ink.dirty = true;
    answers.push(Object.assign(new Error("Bad gateway"), { status: 502 }), Object.assign(new Error("Offline"), {}));
    assert.equal(await files.savePdf(), false);
    assert.equal(files.ink.state.textContent, "Not saved");
    assert.ok(!files.ink.save.hidden && files.ink.save.textContent === "Retry");
    assert.equal(files.ink.dot.className, "files-state-dot is-dirty");
    assert.ok(clock.waits().includes(5000));
    const stats = () => sent.filter(([, path]) => path.startsWith("/api/machine/stat")).length;
    const before = stats();
    await clock.advance(5000);
    assert.ok(stats() > before, "checked before it is sent again"); // (the 5 s check on disk too)
    assert.equal(appended.length, 2);
    assert.ok(clock.waits().includes(30_000), "then after 30 s");
    // Retry: now.
    files.ink.save.listeners.click[0]();
    for (let i = 0; i < 5; i++) await settle();
    assert.equal(appended.length, 3);
    assert.equal(files.ink.state.textContent, "Saved");
    assert.ok(files.ink.save.hidden);
    // Lost again, and the file changed meanwhile: the user picks, the strokes named (never written twice).
    view.ink.dirty = true;
    answers.push(Object.assign(new Error("Offline"), {}));
    await files.savePdf();
    identity = ["9", "9", "9", "10"];
    clearTimeout(files.pdf.pollTimer); // the retry's own check, not the 5 s one
    await clock.advance(5000);
    assert.equal(appended.length, 4, "not sent again");
    assert.ok(files.pdf.waiting && files.pdf.bar.text().includes("changed on the server"));
    files.closeDoc();
  } finally {
    clock.restore();
  }
}

// §3.7: the session ended (401): not tried again, kept in the browser (the sign-in screen comes); the runner's
// failed write cut back: the next save against the identity it told; left in part: read again and cut back.
{
  const keep = keepStore();
  const answers = [];
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF },
    { keep, answer: () => answers.shift() || { path: PDF.path, identity: ["9", "9", "9", "9"] } });
  const view = views[0];
  view.next = SAVE;
  view.ink.dirty = true;
  answers.push(Object.assign(new Error("Sign in again."), { status: 401 }));
  await files.savePdf();
  assert.equal(files.pdf.retryTimer, null);
  assert.match(files.ink.state.title, /sign in again/);
  assert.equal(keep.map.get(PDF.path).kind, "ink", "kept in the browser at once");
  answers.push(Object.assign(new Error("Could not write."), { status: 503, code: "unavailable",
                                                               identity: ["1", "2", "3", "6"] }));
  files.retrySave();
  for (let i = 0; i < 5; i++) await settle();
  assert.deepEqual(files.pdf.identity, ["1", "2", "3", "6"], "the file as it is after the write was undone");
  answers.push(Object.assign(new Error("Could not write."), { status: 503, partial: true, identity: ["1", "2", "3", "7"] }));
  files.retrySave();
  for (let i = 0; i < 5; i++) await settle();
  assert.ok(files.pdf.waiting && files.pdf.bar.text().includes("The last save was cut short."));
  files.closeDoc();
}

// §3.7: too large to save — told, no more saves, no drawing (nothing drawn could be saved), and the PDF with
// its ink can be downloaded from the browser.
for (const keep of [keepStore(), null]) {
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF }, { keep });
  const view = views[0];
  view.tool = { tool: "pen" };
  view.ink.dirty = true;
  view.increment = async () => { throw Object.assign(new Error("Too large to save here: the PDF would be over 64 MiB."),
                                                     { code: "too_large" }); };
  await files.savePdf();
  assert.equal(view.tool, null);
  assert.equal(files.ink.state.textContent, "Not saved");
  assert.ok(files.pdf.bar.text().includes("over 64 MiB"));
  assert.equal(files.pdf.bar.text().includes("kept in this browser"), keep !== null, "said only when it is");
  assert.ok(files.pdf.bar.children.some((c) => c.textContent === "Download a copy with your ink"));
  // PR #119 review C4: no drawing tool to pick again (nothing drawn could be saved).
  assert.ok(files.ink.tools.pen.hidden && files.ink.tools.eraser.hidden && !files.ink.tools.none.hidden);
  files.pickInkTool({ tool: "pen", color: "black", size: "fine" });
  assert.equal(view.tool, null);
  files.closeDoc();
}

// §3.4 (review C2): a check on disk sent before this tab saved is dropped — never a false conflict.
{
  const stat = deferred();
  const { files, views, pending } = await openPdf({ "GET /api/machine/file": PDF, "GET /api/machine/stat": () => stat.promise });
  views[0].next = SAVE;
  views[0].ink.dirty = true;
  const checking = files.checkPdf();
  await settle();
  await files.savePdf(); // the file is ["9", …] now
  views[0].ink.dirty = true; // drawn since: a change would be told
  stat.resolve({ path: PDF.path, kind: "file", identity: ["1", "2", "3", "5"] }); // as it was before the save
  await checking;
  assert.ok(files.pdf.bar.hidden, "the old answer dropped");
  assert.equal(pending.length, 1, "not read again");
  files.closeDoc();
}

// §3.2: the page hidden (iPad: the app put away) — kept in the browser at once, and saved.
{
  const keep = keepStore();
  const appended = [];
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF }, { keep, appended });
  views[0].next = SAVE;
  views[0].ink.dirty = true;
  const puts = [];
  const put = keep.put;
  keep.put = async (store, key, value) => { puts.push(value); return put(store, key, value); };
  files.pageHidden();
  assert.equal(puts.length, 1, "kept before anything is awaited");
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(appended.length, 1);
  assert.ok(!keep.map.has(PDF.path), "saved: nothing kept");
  files.closeDoc();
}

// §4: unsaved ink kept in this browser — the strokes whole, against the version drawn on; gone once nothing
// is unsaved; on the sign-in screen kept, and the PDF closed unasked.
{
  const keep = keepStore();
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF }, { keep });
  const view = views[0];
  view.ink.dirty = true;
  view.carrying = { add: [STROKE], erase: new Set(["himari-ink-old"]), erasePages: [2],
                    refs: new Map([[1, { num: 7, gen: 0 }]]) };
  files.keepInkNow(files.pdf);
  const kept = keep.map.get(PDF.path);
  assert.equal(kept.kind, "ink");
  assert.equal(kept.writer, "me");
  assert.deepEqual(kept.base, ["1", "2", "3", "5"]);
  assert.ok(kept.add[0].points instanceof Float32Array);
  assert.deepEqual(Array.from(kept.add[0].points), [1, 2, 0.5, 3, 4, 0.5]);
  assert.deepEqual(kept.erase, ["himari-ink-old"]);
  assert.deepEqual(kept.refs, [[1, { num: 7, gen: 0 }]]);
  // Too much to keep here: not kept, said beside the state.
  view.carrying = { ...view.carrying, add: [{ ...STROKE, points: Array.from({ length: 1_500_000 }, () => [1, 2, 0.5]) }] };
  files.keepInkNow(files.pdf);
  files.paintInk();
  assert.equal(files.ink.state.textContent, "Unsaved · Not kept in this browser");
  view.carrying = { add: [STROKE], erase: new Set(), erasePages: [], refs: new Map() };
  files.keepInkNow(files.pdf);
  view.ink.dirty = false;
  files.keepInkNow(files.pdf);
  assert.ok(!keep.map.has(PDF.path), "nothing unsaved: this pane's record goes");
  view.ink.dirty = true;
  files.closeForSignIn();
  assert.equal(keep.map.get(PDF.path).kind, "ink");
  assert.equal(files.pdf, null);
}

// §4: kept ink offered when the PDF opens — on the same version put back at once; kept by another tab open
// now, or on another version, the user picks; Discard drops it. Strokes are checked as saved ones are.
for (const [why, base, alive, restored, label] of [
  ["same version", ["1", "2", "3", "5"], false, true, null],
  ["kept by a tab open now", ["1", "2", "3", "5"], true, false, "Restore"],
  ["another version", ["1", "2", "3", "4"], false, false, "Put it on the current version"],
]) {
  const keep = keepStore({ [PDF.path]: { kind: "ink", path: PDF.path, writer: "tab-2:panel", base, at: Date.now(),
    add: [{ ...STROKE, points: Float32Array.of(1, 2, 0.5, 3, 4, 0.5) }, { ...STROKE, name: "not ours" }],
    erase: ["himari-ink-gone", 3], erasePages: [0], refs: [[1, { num: 7, gen: 0 }], ["bad"]] } });
  const asked = [];
  keep.alive = async (tab) => { asked.push(tab); return alive; };
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF }, { keep });
  for (let i = 0; i < 10; i++) await settle();
  assert.deepEqual(asked, ["tab-2"], `${why}: the tab that kept it asked after`);
  const view = views[0];
  if (restored) {
    assert.equal(view.carried.add.length, 1, `${why}: only checked strokes`);
    assert.deepEqual(view.carried.add[0].points, [[1, 2, 0.5], [3, 4, 0.5]]);
    assert.deepEqual([...view.carried.erase], ["himari-ink-gone"]);
    assert.deepEqual([...view.carried.refs], [[1, { num: 7, gen: 0 }]]);
    assert.match(files.pdf.bar.text(), /Unsaved ink from before was restored/);
    assert.equal(keep.map.get(PDF.path).writer, "me", `${why}: this pane's from now on`);
  } else {
    assert.equal(view.carried, null, `${why}: not put back unasked`);
    assert.ok(view.frozen, `${why}: no drawing until it is answered (PR #119 review C2)`);
    const button = files.pdf.bar.children.find((c) => c.textContent === label);
    assert.ok(button, `${why}: ${label}`);
    files.keepInkNow(files.pdf); // nothing replaces the only copy meanwhile
    assert.equal(keep.map.get(PDF.path).writer, "tab-2:panel");
    const discard = files.pdf.bar.children.find((c) => c.textContent === "Discard");
    view.ink.dirty = true; // drawn just before the offer showed
    discard.listeners.click[0]();
    await settle();
    assert.ok(!view.frozen, `${why}: drawn on again`);
    assert.ok(files.pdf.saveTimer !== null, `${why}: what was drawn before is saved (re-review N3)`);
    assert.ok(files.pdf.keepTimer !== null, `${why}: and kept in this browser once the pen rests`);
  }
  files.closeDoc();
}

// §3.6: a save cut short at its end — cut back before anything is drawn, read again (with ink carried here:
// read again after a write left in part, PR #118 re-review N-d), and told; if it cannot be cut, viewed only.
for (const [fails, carrying] of [[false, false], [false, true], [true, false]]) {
  const made = pdfPane({ "GET /api/machine/file": PDF,
    "POST /api/machine/pdf/truncate": () => { if (fails) throw new Error("No."); return { path: PDF.path, identity: ["1", "2", "3", "4"] }; } });
  const real = made.files.loadPdf;
  made.files.loadPdf = async () => {
    const module = await real();
    return { ...module, PdfView: class extends module.PdfView {
      async open() {
        await super.open();
        if (made.views.length > 1) return;
        this.cut = 1500;
        this.ink.dirty = carrying;
      }
    } };
  };
  const opening = made.files.openFile(PDF.path);
  for (let i = 0; i < 20 && !made.pending.length; i++) await settle();
  made.pending[0].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "5"] });
  await opening;
  for (let i = 0; i < 20 && !fails && made.pending.length < 2; i++) await settle();
  const cut = made.sent.find(([, path]) => path === "/api/machine/pdf/truncate");
  assert.deepEqual(cut[2], { path: PDF.path, to: 1500, expected: ["1", "2", "3", "5"] });
  if (fails) {
    for (let i = 0; i < 5; i++) await settle();
    assert.equal(made.files.ink.state.textContent, "View only");
    assert.match(made.files.ink.state.title, /cut short and could not be mended here \(No\.\)/);
  } else {
    made.pending[1].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "4"] });
    for (let i = 0; i < 10; i++) await settle();
    assert.equal(made.files.pdf.view, made.views[1]);
    assert.match(made.files.pdf.bar.text(), /The end of this PDF was cut short; the last save was lost/);
    assert.deepEqual(made.views[1].carried ? made.views[1].carried.add : null, carrying ? [STROKE] : null,
                     carrying ? "the ink carried over" : "nothing carried");
  }
  made.files.closeDoc();
}

// PR #119 review B3: after "keep my ink", the ink carried over is this pane's record on the new version —
// never offered back as from another; "Discard my ink" drops this pane's record too.
{
  const keep = keepStore();
  const { files, views, pending } = await openPdf({ "GET /api/machine/file": PDF }, {
    keep, answer: Object.assign(new Error("changed since"), { code: "changed", status: 409 }) });
  views[0].ink.dirty = true;
  views[0].next = SAVE;
  await files.savePdf();
  assert.deepEqual(keep.map.get(PDF.path).base, ["1", "2", "3", "5"]);
  const press = (label) => files.pdf.bar.children.find((c) => c.textContent === label).listeners.click[0]();
  const again = press("Load the server's version and keep my ink");
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  pending[1].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "6"] });
  await again;
  for (let i = 0; i < 10; i++) await settle();
  assert.deepEqual(keep.map.get(PDF.path).base, ["1", "2", "3", "6"], "kept on the version it is on now");
  assert.ok(files.pdf.bar.hidden || !files.pdf.bar.text().includes("Unsaved ink"), "not offered or put back again");
  assert.ok(!views[1].frozen);
  files.showPdfChanged(true);
  press("Discard my ink");
  for (let i = 0; i < 20 && pending.length < 3; i++) await settle();
  assert.ok(!keep.map.has(PDF.path), "Discard my ink: its kept copy goes too");
  pending[2].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "7"] });
  for (let i = 0; i < 10; i++) await settle();
  files.closeDoc();
}

// PR #119 review B4: the save landed but could not be put in the view (out of memory): read again once that
// save is over — never waiting on itself — with the rest of the ink carried.
{
  const { files, views, pending } = await openPdf({ "GET /api/machine/file": PDF });
  views[0].ink.dirty = true;
  views[0].next = SAVE;
  views[0].saved = async () => { throw new RangeError("Array buffer allocation failed"); };
  assert.equal(await files.savePdf(), true);
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  assert.equal(pending.length, 2, "read again");
  pending[1].resolve({ bytes: new Uint8Array(4), identity: ["9", "9", "9", "9"] });
  for (let i = 0; i < 10; i++) await settle();
  assert.equal(files.pdf.view, views[1]);
  assert.deepEqual(views[1].carried.add, [STROKE]);
  assert.ok(!files.pdf.waiting && !views[1].frozen && files.pdf.inFlight === null, "not stuck");
  files.closeDoc();
}

// PR #119 review C1: the real append tells the runner's failure — the file's identity after it, and partial.
{
  const realFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ error: {
      code: "unavailable", message: "Could not write.", identity: ["1", "2", "3", "6"], partial: true } }) });
    await assert.rejects(postAppend(PDF.path, Uint8Array.of(1), ["1", "2", "3", "5"]),
      (e) => e.status === 503 && e.code === "unavailable" && e.partial === true
        && JSON.stringify(e.identity) === JSON.stringify(["1", "2", "3", "6"]));
    globalThis.fetch = async () => ({ ok: false, status: 409, json: async () => ({ error: { code: "changed", message: "x" } }) });
    await assert.rejects(postAppend(PDF.path, Uint8Array.of(1), ["1"]), (e) => e.identity === undefined && !e.partial);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// Ctrl/Cmd+S while a save is on its way: what changed since it began goes as soon as it lands; leaving waits
// at most 10 s for a save, then asks.
{
  const clock = fakeClock();
  try {
    let sending = deferred();
    const appended = [];
    const confirms = [];
    const { files, views } = await openPdf({ "GET /api/machine/file": PDF, "GET /api/machine/stat": STAT(["9"]) },
                                           { appended, confirms, answer: () => sending.promise });
    const view = views[0];
    view.next = SAVE;
    view.ink.dirty = true;
    view.saved = async (m) => { view.savedWith = m; }; // drawn meanwhile: still unsaved after it
    const first = files.savePdf();
    await settle();
    view.element.listeners.keydown[0]({ ctrlKey: true, key: "s", preventDefault() {} });
    sending.resolve({ path: PDF.path, identity: ["9"] });
    await first;
    sending = deferred();
    for (let i = 0; i < 10; i++) await settle();
    assert.equal(appended.length, 2, "the next save at once, not in 5 s");
    // That one never answers: leaving asks after 10 s.
    const leaving = files.mayLeave();
    await clock.advance(10_000);
    assert.equal(await leaving, true);
    assert.deepEqual(confirms.map((c) => c[0]), ["Leave notes.pdf with ink not saved?"]);
    assert.match(confirms[0][1][0], /not kept in this browser: it is lost/);
    files.closeDoc();
  } finally {
    clock.restore();
  }
}

// Re-review N2: Ctrl/Cmd+S during a save that then leaves nothing unsaved — the next stroke waits its 5 s.
{
  const sending = deferred();
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF }, { answer: () => sending.promise });
  views[0].next = SAVE;
  views[0].ink.dirty = true;
  const saving = files.savePdf();
  await settle();
  views[0].element.listeners.keydown[0]({ ctrlKey: true, key: "s", preventDefault() {} });
  sending.resolve({ path: PDF.path, identity: ["9"] });
  await saving;
  assert.ok(!files.pdf.soon);
  files.closeDoc();
}

// V3-P82 §4: whether the tab that kept ink is open now, asked over the browser's channel (local_store.js).
{
  const store = await import(new URL("../src/himari/interfaces/web/static/local_store.js", import.meta.url));
  const other = new BroadcastChannel("himari-local"); // another tab
  const heard = [];
  other.onmessage = (event) => {
    heard.push(event.data);
    if (event.data.alive === "tab-2") other.postMessage({ here: "tab-2" });
  };
  assert.equal(await store.alive(store.WRITER), true, "this tab");
  assert.equal(await store.alive("tab-2"), true, "an open tab answers");
  assert.equal(await store.alive("tab-3", 30), false, "a closed one does not");
  other.postMessage({ alive: store.WRITER });
  for (let i = 0; i < 20 && !heard.some((m) => m.here); i++) await new Promise((r) => setTimeout(r, 5));
  assert.ok(heard.some((m) => m.here === store.WRITER), "this tab answers for itself");
  other.close();
}

// PR #104 review P1-2: "keep my ink" — the view shown, with its ink, stays until the server's version is open
// and the ink is on it; if that cannot be, nothing is lost and the user is told.
for (const [why, routes, setup, told] of [
  ["its read fails", { "GET /api/machine/file": PDF }, (made) => made.pending, /could not be opened: Gone/],
  ["it is no PDF now", { "GET /api/machine/file": () => (made_count++ ? { ...PDF, kind: "binary" } : PDF) }, null,
   /not a PDF that can be opened here/],
  ["it cannot be opened", { "GET /api/machine/file": PDF }, "fail-open", /could not be opened: This PDF could not/],
]) {
  var made_count = 0;
  const made = await openPdf(routes, setup === "fail-open" ? { failing: new Set([1]) } : {});
  const { files, views, pending } = made;
  const old = views[0];
  old.ink.dirty = true;
  files.showPdfChanged(true);
  const keep = files.pdf.bar.children.find((c) => c.textContent === "Load the server's version and keep my ink");
  const reopening = keep.listeners.click[0]();
  if (setup) {
    for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
    assert.ok(old.frozen, `${why}: no drawing while the other version is fetched`);
    if (typeof setup === "function") pending[1].reject(Object.assign(new Error("Gone"), { status: 404 }));
    else pending[1].resolve({ bytes: new Uint8Array(4), identity: null });
  }
  await reopening;
  assert.equal(files.pdf.view, old, `${why}: the view shown stays`);
  assert.ok(!old.destroyed && old.ink.dirty && !old.frozen, `${why}: its ink kept, drawn on again`);
  assert.ok(files.body.children.includes(old.element));
  assert.match(files.pdf.bar.text(), told);
  assert.ok(files.pdf.bar.text().includes("Your ink is still here"));
  assert.ok(views.slice(1).every((view) => view.destroyed), `${why}: a new view that failed is let go`);
  files.closeDoc();
}
{
  const { files, views, pending } = await openPdf({ "GET /api/machine/file": PDF });
  views[0].ink.dirty = true;
  const reopening = files.reopenPdf(true);
  for (let i = 0; i < 20 && pending.length < 2; i++) await settle();
  assert.ok(!views[0].destroyed, "the old view stays until the new one is ready");
  pending[1].resolve({ bytes: new Uint8Array(4), identity: ["1", "2", "3", "8"] });
  await reopening;
  assert.ok(views[0].destroyed && files.pdf.view === views[1]);
  assert.deepEqual(views[1].carried.add, [STROKE]);
  files.closeDoc();
}

// PR #104 review P2: a signed PDF is told when a drawing tool is picked, never under a pen.
{
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF });
  views[0].signed = true;
  await files.ink.tools.pen.listeners.click[0]();
  assert.match(files.pdf.bar.text(), /This PDF is signed/);
  files.pdf.bar.hidden = true;
  await files.ink.tools.highlighter.listeners.click[0]();
  assert.ok(files.pdf.bar.hidden, "told once");
  files.closeDoc();
}

// PR #103 re-review: a PDF the view could not open is still told kept on its delete (by its kind).
{
  const confirms = [];
  const big = { ...PDF, size: 3 * 1024 * 1024 };
  const { files, pending } = pdfPane({
    "GET /api/machine/file": big,
    "GET /api/machine/stat": () => ({ path: PDF.path, kind: "file", size: big.size, identity: big.identity }),
    "POST /api/machine/file/delete": { path: PDF.path, previous_kept: true },
    "GET /api/machine/list": listing("/home/u", []),
  }, { confirms, fail: "This PDF is password-protected; download it to open." });
  const opening = files.openFile(PDF.path);
  for (let i = 0; i < 20 && !pending.length; i++) await settle();
  pending[0].resolve({ bytes: new Uint8Array(4), identity: null });
  await opening;
  assert.equal(files.pdf, null);
  await files.deleteFile();
  assert.equal(confirms.length, 1);
  assert.match(confirms[0][1].join(" "), /kept as a previous version/);
}

// PR #102 review: a PDF read as a download is held to the limit whatever its size was told — its length
// is checked before any byte is read, and the bytes as they come; past it the download is stopped.
{
  const realFetch = globalThis.fetch;
  const served = ({ chunks, length = null, status = 200, json = null, identity = null }) => {
    const log = { read: 0, cancelled: false };
    let i = 0;
    const body = {
      getReader: () => ({
        read: async () => { if (i >= chunks.length) return { done: true }; log.read += 1; return { done: false, value: chunks[i++] }; },
        cancel: async () => { log.cancelled = true; },
      }),
      cancel: async () => { log.cancelled = true; },
    };
    globalThis.fetch = async (url) => {
      log.url = url;
      return { ok: status === 200, status, body, json: async () => json,
               headers: { get: (name) => (name === "content-length" && length !== null ? String(length)
                 : name === "x-himari-identity" ? identity : null) } };
    };
    return log;
  };
  try {
    let log = served({ chunks: [new Uint8Array(4)], length: 11 });
    await assert.rejects(readDownload("/home/u/a.pdf", 10), (e) => e.status === 413 && /too large/.test(e.message));
    assert.equal(log.read, 0);
    assert.ok(log.cancelled);
    assert.equal(log.url, "/api/machine/download?path=%2Fhome%2Fu%2Fa.pdf");
    log = served({ chunks: [new Uint8Array(4), new Uint8Array(4), new Uint8Array(4), new Uint8Array(4)], length: 8 });
    await assert.rejects(readDownload("/home/u/a.pdf", 10), (e) => e.status === 413);
    assert.equal(log.read, 3, "stopped at the chunk past the limit");
    assert.ok(log.cancelled);
    served({ chunks: [Uint8Array.of(1, 2), Uint8Array.of(3), Uint8Array.of(4, 5, 6, 7, 8, 9, 10)], identity: "1,2,10,4" });
    const got = await readDownload("/home/u/a.pdf", 10);
    assert.deepEqual(Array.from(got.bytes), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    assert.deepEqual(got.identity, ["1", "2", "10", "4"], "the identity of the bytes read (V3-P77 §5.3)");
    served({ chunks: [], status: 404, json: { error: { code: "not_found", message: "No such file." } } });
    await assert.rejects(readDownload("/home/u/a.pdf", 10), (e) => e.status === 404 && e.message === "No such file.");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- the web UX review's small bugs --------------------------------------------------------------------

// HIMARI changed the open file (no unsaved text): the editor shows its version, the cursor and scroll where
// they were (cut to the new text), the focus not taken.
{
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT,
    "GET /api/machine/stat": { ...TEXT, identity: [9, 9, 9, 9] } });
  await files.openFile(TEXT.path);
  const editor = module.made[0];
  const placed = [];
  editor.view = { state: { selection: { main: { anchor: 2, head: 3 } }, get doc() { return { length: editor.value.length }; } },
                  scrollDOM: { scrollTop: 120 }, dispatch(change) { placed.push(change); this.scrollDOM.scrollTop = 0; } };
  await files.loadDisk();
  assert.deepEqual(placed, [{ selection: { anchor: 2, head: 3 } }]);
  assert.equal(editor.view.scrollDOM.scrollTop, 120);
  files.closeDoc();
}

// A previous version put in the editor over unsaved text: asked first; "no" leaves the text.
{
  const { files, module, asked } = editorPane({ "GET /api/machine/file": TEXT }, { confirms: [false, true] });
  await files.openFile(TEXT.path);
  module.made[0].type("mine, unsaved");
  await files.restore("an old version");
  assert.equal(module.made[0].text(), "mine, unsaved");
  assert.deepEqual(asked.at(-1), [`Replace your unsaved changes to a.txt?`, "Replace"]);
  await files.restore("an old version");
  assert.equal(module.made[0].text(), "an old version");
  const before = asked.length;
  await files.restore("compared one", { seen: true }); // compared just now: no question
  assert.equal(asked.length, before);
  files.closeDoc();
}

// Ask HIMARI about a PDF after its ink was saved: the identity it has now, not the one it was opened with;
// unsaved ink saved first (V3-P82 §3.2), and if it cannot be, told in the ink's state slot (review N1).
{
  const asks = [];
  const { files, views } = await openPdf({ "GET /api/machine/file": PDF });
  files.onAsk = (reference) => asks.push(reference);
  files.showPdfNote = () => { throw new Error("the bar is left as it is"); };
  await files.askWhole({ path: PDF.path, identity: [1, 1, 1, 1], kind: "pdf" });
  assert.deepEqual(asks, [{ path: PDF.path, name: "notes.pdf", identity: ["1", "2", "3", "5"], whole: true }]);
  views[0].ink.dirty = true;
  views[0].next = { bytes: Uint8Array.of(1), changes: [], added: new Map() };
  await files.askWhole({ path: PDF.path, identity: [1, 1, 1, 1], kind: "pdf" });
  assert.deepEqual(asks[1].identity, ["9", "9", "9", "9"], "saved, then asked about as saved");
  views[0].ink.dirty = true;
  files.pdf.waiting = true; // changed on the server: the user picks, nothing is saved
  await files.askWhole({ path: PDF.path, identity: [1, 1, 1, 1], kind: "pdf" });
  assert.equal(asks.length, 2);
  assert.equal(files.ink.state.textContent, "Not saved: HIMARI reads the saved file");
  files.closeDoc();
}

// A note beside the file's name (here a refused save) stays a while: the next keystroke does not wipe it.
{
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": () => { throw refusal("too_large"); } });
  await files.openFile(TEXT.path);
  module.made[0].type("x");
  await files.save();
  assert.equal(files.stateLabel.textContent, "too_large");
  module.made[0].type("xy");
  assert.equal(files.stateLabel.textContent, "too_large", "still there after a keystroke");
  files.doc.note.until = 0; // its time is up
  module.made[0].type("xyz");
  assert.equal(files.stateLabel.textContent, "Unsaved");
  files.closeDoc();
}

// Review B1: a note about the text before a save never outlives the save.
{
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": { path: TEXT.path, identity: [5, 5, 5, 5], previous_kept: true } });
  await files.openFile(TEXT.path);
  await files.restore("an old version");
  assert.equal(files.stateLabel.textContent, "Put in the editor — Save to keep it");
  await files.save();
  assert.equal(files.stateLabel.textContent, "Saved");
  files.closeDoc();
}

// The PDF's zoom buttons: out, back to the width, in — around the middle of what shows.
{
  const files = new FilesPane({ api: async () => HOME, el });
  const zooms = [];
  const view = { zoom: 2, element: { getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 100 }) },
                 zoomBy(factor, x, y) { zooms.push([Math.round(factor * 100) / 100, x, y]); }, current: 0 };
  files.pdf = { path: "/home/u/p.pdf", view };
  const group = files.zoomTools();
  for (const button of group.children) button.listeners.click[0]();
  assert.deepEqual(zooms, [[0.8, 100, 50], [0.5, 100, 50], [1.25, 100, 50]]);
  files.pdf = null;
}

// --- unsaved text kept in the browser (V3-P78 §4) ------------------------------------------------------

/* The browser's store (local_store.js) as a map of "unsaved" records. */
function keepStore(records = {}) {
  const map = new Map(Object.entries(records));
  return { map, get: async (store, key) => map.get(key) ?? null, put: async (store, key, value) => { map.set(key, value); },
           remove: async (store, key, writer = null) => { if (writer === null || map.get(key)?.writer === writer) map.delete(key); },
           prune: async () => {} };
}
const KEPT = { kind: "text", path: TEXT.path, base: [1, 2, 3, 4], text: "kept\n", lineBreak: "\n", at: Date.now(),
               writer: "another tab" };
const barLabels = (files) => files.doc.bar.children.map((c) => c.textContent);
const press = (files, label) => files.doc.bar.children.find((c) => c.textContent === label).listeners.click[0]();

// §4.1: kept text is offered, never put in by itself; the editor is read-only until Restore or Discard (C2).
{
  const keep = keepStore({ [TEXT.path]: KEPT });
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT }, { keep });
  await files.openFile(TEXT.path);
  const editor = module.made[0];
  assert.equal(editor.text(), "one");
  assert.equal(editor.readOnly, true);
  assert.equal(files.dirty, false);
  assert.ok(files.doc.bar.text().includes("were kept in this browser"));
  assert.deepEqual(barLabels(files).slice(1), ["Restore", "Discard"]);
  files.keepNow(); // nothing typed can replace the only copy meanwhile
  assert.equal(keep.map.get(TEXT.path).writer, "another tab");
  press(files, "Restore");
  assert.equal(editor.text(), "kept\n");
  assert.equal(editor.readOnly, false);
  assert.equal(files.dirty, true);
  assert.equal(files.doc.bar.hidden, true);
  assert.equal(keep.map.get(TEXT.path).writer, "me", "this pane's from now on");
  files.closeDoc();
}

// PR #106 review B1: while Restore is offered, a save (Ctrl+S) or a check of the disk changes nothing.
{
  const keep = keepStore({ [TEXT.path]: { ...KEPT, writer: "me" } });
  const { files, sent, module } = editorPane({ "GET /api/machine/file": TEXT,
    "GET /api/machine/stat": { path: TEXT.path, kind: "file", identity: [9, 9, 9, 9], size: 3 },
    "PUT /api/machine/file": { path: TEXT.path, identity: [5, 5, 5, 5], previous_kept: true },
    "POST /api/machine/file/delete": { path: TEXT.path, previous_kept: true },
    "GET /api/machine/list": listing("/home/u", []) }, { keep });
  await files.openFile(TEXT.path);
  await files.save();
  await files.checkDisk();
  assert.equal(sent.filter(([m]) => m === "PUT").length, 0);
  assert.equal(keep.map.get(TEXT.path).text, "kept\n");
  assert.deepEqual(barLabels(files).slice(1), ["Restore", "Discard"]);
  files.restore("a previous version"); // Previous versions → Put in the editor waits too (re-review N1)
  assert.equal(module.made[0].text(), "one");
  files.fileKind = "text";
  await files.deleteFile(); // deleted while offered: the kept text stays, offered again there (re-review C1)
  assert.equal(keep.map.get(TEXT.path).text, "kept\n");
}

// …kept on a version since changed: Restore leads to the conflict bar, so a save never goes over it.
{
  const keep = keepStore({ [TEXT.path]: { ...KEPT, base: [1, 1, 1, 1] } });
  const { files, sent } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": () => { throw refusal("changed"); },
    "GET /api/machine/stat": { path: TEXT.path, kind: "file", identity: [1, 2, 3, 4], size: 3 } }, { keep });
  await files.openFile(TEXT.path);
  press(files, "Restore");
  assert.ok(files.doc.bar.text().includes("changed on the server"));
  await files.save();
  assert.deepEqual(sent.find(([m]) => m === "PUT")[2].expected, [1, 1, 1, 1]);
  assert.ok(barLabels(files).includes("Save mine over it"));
  files.closeDoc();
}

// Discard drops it, whoever kept it; open unsaved in the other pane, it is not offered there (B4).
{
  const keep = keepStore({ [TEXT.path]: KEPT });
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT }, { keep });
  await files.openFile(TEXT.path);
  press(files, "Discard");
  assert.equal(keep.map.has(TEXT.path), false);
  assert.equal(module.made[0].readOnly, false);
  files.closeDoc();
  const other = keepStore({ [TEXT.path]: KEPT });
  const elsewhere = editorPane({ "GET /api/machine/file": TEXT }, { keep: other, openElsewhere: () => true });
  await elsewhere.files.openFile(TEXT.path);
  assert.equal(elsewhere.files.doc.bar.hidden, true);
  elsewhere.files.closeDoc();
}

// Typing is kept with the identity it was read at; a save drops this pane's record; so does undoing to the
// saved text (N8). A record another writer made is not this pane's to drop.
{
  const keep = keepStore();
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": { path: TEXT.path, identity: [5, 5, 5, 5], previous_kept: true } }, { keep });
  await files.openFile(TEXT.path);
  module.made[0].type("two");
  files.keepNow();
  assert.deepEqual(keep.map.get(TEXT.path), { ...keep.map.get(TEXT.path), kind: "text", base: [1, 2, 3, 4],
                                               text: "two", writer: "me" });
  await files.save();
  assert.equal(keep.map.has(TEXT.path), false);
  keep.map.set(TEXT.path, KEPT);
  module.made[0].type("three");
  module.made[0].type("two"); // the saved text again
  await new Promise((r) => setTimeout(r, 1100));
  assert.equal(keep.map.get(TEXT.path).writer, "another tab");
  files.closeDoc();
}

// §6: the session ended — unsaved text is kept at once and the editor closes without asking (C4).
{
  const keep = keepStore();
  const { files, module, asked } = editorPane({ "GET /api/machine/file": TEXT }, { keep });
  await files.openFile(TEXT.path);
  module.made[0].type("typed");
  files.closeForSignIn();
  assert.equal(keep.map.get(TEXT.path).text, "typed");
  assert.equal(files.doc, null);
  assert.equal(asked.length, 0);
}

// §4.2: the file is gone (the runner says "changed"): with unsaved text, make it again or download it.
{
  const { files, sent, module } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": () => { throw refusal("changed"); },
    "GET /api/machine/stat": () => { throw refusal("not_found"); },
    "POST /api/machine/file/install": { path: TEXT.path, identity: [8, 8, 8, 8] } }, { keep: keepStore() });
  await files.openFile(TEXT.path);
  module.made[0].type("mine");
  await files.save();
  assert.ok(files.doc.bar.text().includes("no longer on the server"));
  assert.deepEqual(barLabels(files).slice(1), ["Save it here again", "Download my text"]);
  await press(files, "Save it here again");
  assert.deepEqual(sent.find(([m, p]) => m === "POST" && p === "/api/machine/file/install")[2],
                   { path: TEXT.path, content: "mine" });
  assert.equal(files.dirty, false);
  assert.deepEqual(files.doc.identity, [8, 8, 8, 8]);
  assert.equal(files.doc.bar.hidden, true);
  files.closeDoc();
}

// …the folder is gone too, or the file system cannot place it safely: download only. A name taken
// meanwhile: the conflict bar, never over that file.
for (const [code, said, labels] of [["not_found", "folder is gone too", ["Download my text"]],
                                    ["unsupported", "can't take a new file safely", ["Download my text"]],
                                    ["changed", "changed on the server", ["Compare", "Load the server's version", "Save mine over it"]]]) {
  const { files, module } = editorPane({ "GET /api/machine/file": TEXT,
    "PUT /api/machine/file": () => { throw refusal("changed"); },
    "GET /api/machine/stat": () => { throw refusal("not_found"); },
    "POST /api/machine/file/install": () => { throw refusal(code); } });
  await files.openFile(TEXT.path);
  module.made[0].type("mine");
  await files.save();
  await press(files, "Save it here again");
  assert.ok(files.doc.bar.text().includes(said), code);
  assert.deepEqual(barLabels(files).slice(1), labels, code);
  assert.equal(module.made[0].text(), "mine");
  files.closeDoc();
}

// C5: gone while the conflict bar shows — its buttons lead to the gone bar; N7: a clean file is only told.
{
  let gone = false;
  const { files, module } = editorPane({ "GET /api/machine/file": () => { if (gone) throw refusal("not_found"); return TEXT; },
    "PUT /api/machine/file": () => { throw refusal("changed"); },
    "GET /api/machine/stat": () => { if (gone) throw refusal("not_found"); return { ...TEXT, identity: [9, 9, 9, 9] }; } });
  await files.openFile(TEXT.path);
  module.made[0].type("mine");
  await files.save();
  assert.ok(barLabels(files).includes("Load the server's version"));
  gone = true;
  await press(files, "Load the server's version");
  assert.ok(barLabels(files).includes("Save it here again"));
  files.closeDoc();
  gone = false;
  await files.openFile(TEXT.path);
  gone = true;
  await files.checkDisk();
  assert.equal(files.doc.bar.hidden, true);
  assert.equal(files.stateLabel.textContent, "This file is no longer on the server");
  files.closeDoc();
}

// The same folder read again after HIMARI's turn stays where the user had scrolled; another folder starts at the top.
{
  const files = new FilesPane({ api: async (method, path) => (path === "/api/machine/stat?path=~" ? HOME
    : listing(path.includes("other") ? "/home/u/other" : "/home/u", ["a", "b"])), el });
  await files.openFolder("/home/u");
  files.body.scrollTop = 420;
  await files.refresh();
  assert.equal(files.body.scrollTop, 420);
  files.body.scrollTop = 420;
  await files.openFolder("/home/u/other");
  assert.notEqual(files.body.scrollTop, 420);
}

// A PDF opens where it was left in this browser (its page and zoom); one opened at a page asked for does not.
{
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)),
                              removeItem: (k) => store.delete(k) };
  const shownAt = [];
  const fakeView = () => ({ current: 0, zoom: 1, numPages: 9, writable: false, element: el("div"), destroyed: false,
    show(page) { this.current = page - 1; shownAt.push([page, this.zoom]); }, setTool() {}, destroy() { this.destroyed = true; },
    ink: { dirty: false } });
  const files = new FilesPane({ api: async () => HOME, el });
  files.loadPdfView = async () => ({ view: fakeView(), identity: [1, 1, 1, 1], left: 0 });
  files.schedulePdfPoll = () => {};
  const shown = { path: "/home/u/book.pdf", size: 10, identity: [1, 1, 1, 1], kind: "pdf" };
  await files.showPdf(shown, files.ticket);
  assert.deepEqual(shownAt.pop(), [1, 1]); // never seen: page 1
  files.pdf.view.current = 6; // read on to page 7, zoomed in
  files.pdf.view.zoom = 1.5;
  files.closeDoc();
  await files.showPdf(shown, files.ticket);
  assert.deepEqual(shownAt.pop(), [7, 1.5]);
  files.closeDoc();
  await files.showPdf(shown, files.ticket, 2); // a page asked for (a reference): that page, its own zoom
  assert.deepEqual(shownAt.pop(), [2, 1]);
  files.closeDoc();
  delete globalThis.localStorage;
}

console.log("files smoke: ok");
