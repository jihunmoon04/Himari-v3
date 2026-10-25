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

console.log("files smoke: ok");
