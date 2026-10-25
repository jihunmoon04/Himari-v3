/* The file explorer's pane (files.js, V3-P75 §3) against a fake DOM: no
 * browser, packages or live service. Usage: node scripts/files_smoke.mjs */
import assert from "node:assert/strict";

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
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
    assert.deepEqual(labels(node(fileRow)), ["Download", "Copy path"]);
  }
}

// --- the editor (V3-P75 §7), with a fake CodeMirror and a fake server ---------------------------------

function fakeModule() {
  const made = [];
  return {
    made,
    createEditor(parent, opts) {
      const editor = { value: opts.text, opts, text: () => editor.value, destroyed: false, picked: [0, 0],
        setText(text) { editor.value = text; }, type(text) { editor.value = text; if (opts.onChange) opts.onChange(); },
        destroy() { editor.destroyed = true; }, setReadOnly() {}, focus() {},
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
function editorPane(routes, { confirms = [], onAsk = null } = {}) {
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
  const files = new FilesPane({ api, el, loadEditor: async () => module, onAsk,
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

// PR #95 review B3: a file deleted from a folder is found there, and put back as a new file with its content.
{
  const { files, sent, module } = editorPane({
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

console.log("files smoke: ok");
