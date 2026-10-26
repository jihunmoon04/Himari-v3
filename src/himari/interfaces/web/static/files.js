/* The file explorer's parts (V3-P75 §3): a folder list and a file view in
 * one pane, placed by the caller — the main area (Files in the sidebar) or
 * the panel beside a conversation.
 *
 * Every name, path and file text goes in with textContent (or into the
 * editor as its text). A file's bytes are never shown as they are: text as
 * text, an image as the copy the server's image sandbox made, anything else
 * as a download (an attachment).
 *
 * The editor (§7): a text file of at most 1 MiB opens in CodeMirror, loaded
 * on first use. A save sends the identity the text was read with; one made
 * on disk since is never overwritten without the user choosing to (compare,
 * load the disk's, or save theirs over it). Every save and delete keeps the
 * previous version on the server; the ⋯ menu lists them to compare and to
 * put back. Unsaved text is not dropped without asking.
 *
 * Ask HIMARI (V3-P76 §3): a selection, or the whole file, handed to the
 * place as a reference — the file's path and identity, and for a selection
 * its positions and the SHA-256 of its text, never the text. Saved text only.
 *
 * Changes (V3-P76 §5, §6), beside a conversation: the files HIMARI changed
 * there and the user has not reviewed — each with a diff of what it was
 * before and what it is now, to accept or revert; a dot on their rows.
 *
 * A PDF (V3-P77 §3) of at most 64 MiB opens in the PDF view (pdfpane.js,
 * loaded on first use), its bytes read as a download.
 */

const KIND_ICON = { directory: "#side-folder", file: "#side-note" };
const EDIT_MAX_BYTES = 1024 * 1024; // the server's limit for the editor (§7.7, Q3)
const POLL_MS = 5000; // how often an open file is checked on disk (§5.3)
const KEEP_MS = 1000; // unsaved text kept in this browser once typing settles (V3-P78 §4.1)
const KEPT_MAX = 20; // V3-P78 Q3: files with unsaved text kept, for at most 30 days
const KEPT_AGE_MS = 30 * 24 * 3600 * 1000;
const PDF_MAX_BYTES = 64 * 1024 * 1024; // the PDF view's limit (V3-P77 §3.3, Q2), as pdfpane.js's
const INK_TOOL_KEY = "himari.pdf.tool"; // the ink tool picked last, in this browser (V3-P77 §4.2)
const SIGNED_NOTICE = "This PDF is signed. Notes are added after the signature; some apps show that as a change.";
const INK_TOOLS = { pen: { tool: "pen", color: "black", size: "fine" },
                    highlighter: { tool: "highlighter", color: "yellow", size: "wide" }, eraser: { tool: "eraser" } };
const CHANGE_KINDS = { created: "made by HIMARI", changed: "changed by HIMARI", deleted: "deleted by HIMARI",
                       none: "made and deleted by HIMARI", unknown: "HIMARI may have changed it" };
const CHANGE_STATES = { no_version: "its previous version could not be kept",
                        uncertain: "whether or how HIMARI changed it is unknown",
                        expired: "its previous version has expired" };
// What a previous version was kept before (V3-P75 §7.4; V3-P76 §4.2: the agent's, and a revert).
const VERSION_REASONS = { save: "before a save", delete: "before a delete", revert: "before a revert",
                          agent: "before HIMARI changed it", agent_delete: "before HIMARI deleted it",
                        ink: "before ink was saved", restore: "before a previous version was put back" };

/* Line breaks: CodeMirror reads \r\n, \r and \n each as one "\n" (V3-P76 §12.2), so the editor's text is
 * compared in that form, and a save writes the file's own break back — the one most of its lines end with. */
function editorText(text) {
  return text.replace(/\r\n?/g, "\n");
}

function lineBreakOf(text) {
  const crlf = (text.match(/\r\n/g) || []).length;
  const cr = (text.match(/\r(?!\n)/g) || []).length;
  const lf = (text.match(/\n/g) || []).length - crlf;
  if (crlf > lf && crlf >= cr) return "\r\n";
  return cr > lf ? "\r" : "\n";
}

function fileText(text, lineBreak) {
  return lineBreak === "\n" ? text : text.replace(/\n/g, lineBreak);
}

/* What a revert of all left (PR #98 review C3): each file and why, and the hidden ones counted. */
function revertedAll({ left, hidden }) {
  if (!left.length && !hidden) return "Reverted.";
  const why = (item) => ({ changed: "changed since", not_revertible: "cannot be reverted" })[item.code] || item.message;
  const parts = left.map((item) => `${item.path.slice(item.path.lastIndexOf("/") + 1)} (${why(item)})`);
  if (hidden) parts.push(`${hidden} in ${hidden === 1 ? "a file" : "files"} hidden now`);
  return `Reverted all but ${parts.join(", ")}.`;
}

function defaultConfirm(title, lines) { // the browser's own: no action label
  return Promise.resolve(window.confirm([title, ...lines].join("\n\n")));
}

function clock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* ``text`` saved by the browser as a file called ``name`` (V3-P78 §4.2): nothing goes to the server. */
function downloadText(text, name) {
  if (typeof document === "undefined" || typeof URL === "undefined" || !URL.createObjectURL) return;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "application/octet-stream" }));
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 60000);
}

function sameIdentity(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}

/* The SHA-256 of ``text``'s UTF-8, as hex (the browser's SubtleCrypto: a secure page only). */
async function sha256Hex(text) {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
}

function icon(href) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "side-icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", href);
  svg.append(use);
  return svg;
}

export function humanSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function shortDate(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const date = new Date(seconds * 1000);
  const now = new Date();
  return date.getFullYear() === now.getFullYear()
    ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : date.toLocaleDateString();
}

function join(folder, name) {
  return folder === "/" ? `/${name}` : `${folder}/${name}`;
}

function query(path) {
  return `path=${encodeURIComponent(path)}`;
}

/* A file's bytes, from the download endpoint (V3-P77 §3.2: no other read of them), at most ``limit``: the
 * file may have grown since its size was told (PR #102 review), so its length is checked before reading
 * and every chunk as it comes; past the limit the download is stopped. {bytes, identity}: the identity of
 * those bytes, as the server told it (§5.3), or null. */
export async function readDownload(path, limit) {
  const response = await fetch(`/api/machine/download?${query(path)}`, { credentials: "same-origin" });
  if (!response.ok) {
    let message = "The file could not be read.";
    try {
      message = (await response.json()).error.message || message;
    } catch (ignored) { /* not JSON */ }
    throw Object.assign(new Error(message), { status: response.status });
  }
  const tooLarge = () => Object.assign(new Error(`This file is now too large to view here (at most ${humanSize(limit)}).`),
                                       { status: 413, code: "too_large" });
  const told = Number(response.headers.get("content-length"));
  if (told > limit) {
    await response.body?.cancel();
    throw tooLarge();
  }
  const sent = response.headers.get("x-himari-identity");
  const identity = sent ? sent.split(",") : null;
  if (!response.body) return { bytes: new Uint8Array(0), identity };
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > limit) {
      await reader.cancel();
      throw tooLarge();
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, at);
    at += chunk.length;
  }
  return { bytes, identity };
}

/* V3-P77 §5.3: ink saved — ``increment`` added after the PDF read with ``identity``. The answer
 * ({path, identity, previous_kept}), or an error with the server's code ("changed": it changed since). */
export async function postAppend(path, increment, identity) {
  let response;
  try {
    response = await fetch(`/api/machine/pdf/append?${query(path)}`, {
      method: "POST", credentials: "same-origin", body: increment,
      headers: { "content-type": "application/octet-stream", "x-himari-expected": identity.join(",") },
    });
  } catch (ignored) {
    throw Object.assign(new Error("Not saved: the server could not be reached."), { code: "network", status: 0 });
  }
  let data = null;
  try {
    data = await response.json();
  } catch (ignored) { /* not JSON */ }
  if (!response.ok) {
    const error = (data && data.error) || {};
    throw Object.assign(new Error(error.message || "Not saved."), { code: error.code || "http_error",
                                                                     status: response.status });
  }
  return data;
}

export class FilesPane {
  /* api(method, path) → JSON or throws {message, status, code}; el(tag, class, text). */
  /* folderActions(listing): the shown folder's own controls (V3-P75 §3.3, e.g.
   * "Open conversation"); folderRowActions(path): [label, action] pairs for a
   * folder row's ⋯ menu. Both from the place the pane is in. */
  /* loadEditor(): the CodeMirror module (createEditor, createDiff); confirm(title, lines, action) → Promise<bool>;
   * working(): HIMARI is at work in this place now (told beside the editor, §7.3).
   * onAsk(reference): Ask HIMARI (V3-P76 §3.1) — {path, name, identity, whole} and, for a selection,
   * {start, end, sha256, lines: [fromLine, fromColumn, toLine, toColumn]}; none: not offered here.
   * changes: the review of HIMARI's changes (V3-P76 §5) — {of(), list(of), diff(of, path), accept(of, paths | null),
   * revert(of, path, seen), revertAll(of), seen(list)}: of() the conversation shown now, and each call the API
   * call of the conversation ``of`` it names (seen: told every list read); none: not offered here. */
  /* loadPdf(): the PDF view's module (pdfpane.js); readFile(path, limit): a file's {bytes, identity}, as a
   * download, refused past ``limit``; appendPdf(path, increment, identity): ink saved (V3-P77 §5.3). */
  /* keep: the browser's store of unsaved text (local_store.js: get, put, remove, prune; V3-P78 §4.1), none:
   * nothing kept; writer: this pane in this tab, on what it keeps; openElsewhere(path): the file is open with
   * unsaved text in this tab's other pane (its kept text is not offered here). */
  constructor({ api, el, onOpenFolder = null, onOpenFile = null, actions = [], folderActions = null,
                folderRowActions = null, loadEditor = () => import("/assets/codemirror/codemirror.js"),
                loadPdf = () => import("/assets/pdfpane.js"), readFile = readDownload, appendPdf = postAppend,
                confirm = defaultConfirm, working = () => false, onAsk = null, changes = null, keep = null,
                writer = "", openElsewhere = () => false }) {
    this.api = api;
    this.el = el;
    this.loadEditor = loadEditor;
    this.loadPdf = loadPdf;
    this.readFile = readFile;
    this.appendPdf = appendPdf;
    this.confirm = confirm;
    this.working = working;
    this.onAsk = onAsk;
    this.changes = changes;
    this.keep = keep;
    this.writer = writer;
    this.openElsewhere = openElsewhere;
    this.changed = new Set(); // the paths HIMARI changed, as the last list said: a dot on their rows
    this.page = null; // "changes", or {path} of one change's diff, while one of them is shown
    this.pageOf = null; // the conversation that page is of
    this.reviewControls = []; // that page's accept and revert controls
    this.pageDiff = null; // that diff's CodeMirror view
    this.doc = null; // the file in the editor: {path, name, identity, saved, lineBreak, editor, dirty, saving, pollTimer, bar, revision}
    this.pdf = null; // the PDF in view: {path, name, identity, view, bar, saving, pollTimer}
    this.ink = null; // its tools in the head
    this.fileKind = null; // the shown file's kind, as the server told it
    this.onOpenFolder = onOpenFolder;
    this.onOpenFile = onOpenFile;
    this.folderActions = folderActions;
    this.folderRowActions = folderRowActions;
    this.root = el("div", "files-pane");
    this.crumbs = el("nav", "files-crumbs");
    this.crumbs.setAttribute("aria-label", "Path");
    this.tools = el("div", "files-tools");
    const head = el("div", "files-head");
    const end = el("div", "files-head-end"); // one group: it goes under the path together when the head is tight
    end.append(this.tools, ...actions); // the place's own controls (the panel's expand, close)
    head.append(this.crumbs, end);
    this.body = el("div", "files-body");
    this.root.append(head, this.body);
    this.home = null; // the user's home as the server resolves ~
    this.folder = null; // the folder shown, or the file's folder
    this.file = null; // the file shown
    this.ticket = 0; // the latest request: an older answer is not applied
  }

  /* The folder the pane shows (reloaded), or the file's, or the Changes shown. */
  get place() {
    return this.page || this.file || this.folder;
  }

  /* Text typed in the editor, or ink drawn on a PDF (V3-P77 §4.5), and not saved. */
  get dirty() {
    return (this.doc !== null && this.doc.dirty) || (this.pdf !== null && this.pdf.view.ink.dirty);
  }

  /* Before anything replaces the editor or the PDF: unsaved text or ink is dropped only if the user says so. */
  async mayLeave() {
    if (!this.dirty) return true;
    if (this.doc === null) {
      return this.confirm(`Discard your ink on ${this.pdf.name}?`,
                          ["It is not saved. The PDF on the server stays as it was."], "Discard");
    }
    const doc = this.doc;
    const discard = await this.confirm(`Discard your changes to ${doc.name}?`,
                                       ["They are not saved. The file on the server stays as it was."], "Discard");
    if (discard) this.dropKept(doc); // the user's own answer: what this pane kept goes too (V3-P78 §4.1, C4)
    return discard;
  }

  closeDoc() {
    if (this.pageDiff) this.pageDiff.destroy();
    this.pageDiff = null;
    this.page = null;
    this.pageOf = null;
    this.reviewControls = [];
    if (this.pdf) {
      clearTimeout(this.pdf.pollTimer);
      this.pdf.view.destroy();
    }
    this.pdf = null;
    this.ink = null;
    this.pageLabel = null;
    if (this.doc === null) return;
    clearTimeout(this.doc.pollTimer);
    clearTimeout(this.doc.keepTimer);
    if (this.doc.editor) this.doc.editor.destroy();
    this.doc = null;
  }

  /* The session ended (V3-P78 §6): unsaved text is kept in the browser now, and the editor closes without
   * asking; reopening the file offers it back (§4.1). Ink waits for V3-P77 2c: a PDF with unsaved ink stays
   * open where nothing asks about it on the way back (``keepInk``), else it goes. */
  closeForSignIn({ keepText = true, keepInk = false } = {}) {
    const doc = this.doc;
    if (doc !== null && doc.dirty && keepText) this.keepNow(doc);
    if (this.pdf !== null && keepInk && this.pdf.view.ink.dirty) return;
    const open = this.doc !== null || this.pdf !== null;
    this.closeDoc();
    if (open) this.body.replaceChildren();
  }

  /* ------------------------------------------------- unsaved text kept in the browser (V3-P78 §4.1) */

  /* The editor's text changed: kept once typing settles while it differs from the saved text; this pane's
   * record goes once it is the saved text again (N8). */
  keepLater(doc) {
    if (this.keep === null || doc.offer) return; // while Restore is offered, the only copy stays (C2)
    clearTimeout(doc.keepTimer);
    doc.keepTimer = setTimeout(() => {
      if (this.doc !== doc) return;
      if (doc.dirty) this.keepNow(doc);
      else this.dropKept(doc);
    }, KEEP_MS);
  }

  keepNow(doc = this.doc) {
    if (this.keep === null || doc === null || doc.offer || !doc.dirty) return;
    clearTimeout(doc.keepTimer);
    this.keep.put("unsaved", doc.path, { kind: "text", path: doc.path, base: doc.identity, text: doc.editor.text(),
                                         lineBreak: doc.lineBreak, at: Date.now(), writer: this.writer });
    this.keep.prune("unsaved", KEPT_MAX, KEPT_AGE_MS);
  }

  /* What this pane kept for ``doc``'s file goes (saved, the server's version loaded, discarded). */
  dropKept(doc) {
    if (this.keep === null || doc === null) return;
    clearTimeout(doc.keepTimer);
    this.keep.remove("unsaved", doc.path, this.writer);
  }

  /* Text kept for ``path`` and not open unsaved in the other pane here, or null. */
  async keptFor(path) {
    if (this.keep === null || this.openElsewhere(path)) return null;
    const kept = await this.keep.get("unsaved", path);
    if (!kept || kept.kind !== "text" || typeof kept.text !== "string" || typeof kept.at !== "number"
        || Date.now() - kept.at > KEPT_AGE_MS) return null;
    return kept;
  }

  /* §4.1: "Unsaved changes … were kept" — Restore or Discard; the editor is read-only until one is picked. */
  offerKept(doc, kept) {
    doc.offer = kept;
    doc.editor.setReadOnly(true);
    const bar = doc.bar;
    bar.replaceChildren(this.el("p", null, `Unsaved changes from ${clock(kept.at)} were kept in this browser.`));
    bar.append(this.button("Restore", () => this.restoreKept(doc)),
               this.button("Discard", () => this.discardKept(doc)));
    bar.hidden = false;
    this.paintState();
  }

  restoreKept(doc) {
    const kept = doc.offer;
    if (this.doc !== doc || !kept) return;
    doc.offer = null;
    doc.editor.setReadOnly(false);
    this.hideBar();
    doc.lineBreak = kept.lineBreak || doc.lineBreak;
    const changed = Array.isArray(kept.base) && !sameIdentity(kept.base, doc.identity);
    if (changed) doc.identity = kept.base; // a save meets the conflict bar: never over the newer version
    doc.editor.setText(kept.text);
    this.setDirty(doc.editor.text() !== doc.saved);
    this.keepNow(doc); // this pane's from now on
    if (changed) this.showChanged(false);
  }

  discardKept(doc) {
    if (this.doc !== doc || !doc.offer) return;
    doc.offer = null;
    doc.editor.setReadOnly(false);
    this.hideBar();
    this.keep.remove("unsaved", doc.path); // the user's answer, whoever kept it
    this.paintState();
  }

  /* §4.1: a file whose kept text cannot go back in the editor (gone, or no longer text it edits): the text
   * can be saved as the file again (gone) or downloaded, or discarded. */
  async offerKeptAside(path, ticket, gone) {
    const kept = await this.keptFor(path);
    if (kept === null || ticket !== this.ticket) return;
    const name = path.slice(path.lastIndexOf("/") + 1);
    const box = this.el("div", "files-bar");
    box.append(this.el("p", null, gone
      ? `This file is no longer on the server. Unsaved changes from ${clock(kept.at)} were kept in this browser.`
      : `Unsaved text from ${clock(kept.at)} can't be put back here: this file is no longer text the editor opens.`));
    const content = fileText(kept.text, kept.lineBreak || "\n");
    if (gone) {
      box.append(this.button("Save it here again", async () => {
        try {
          await this.api("POST", "/api/machine/file/install", { path, content });
          await this.keep.remove("unsaved", path);
          if (ticket === this.ticket) await this.openFile(path);
        } catch (error) {
          if (ticket === this.ticket) box.append(this.el("p", "files-note is-error", error.message));
        }
      }));
    }
    box.append(this.button("Download my text", () => downloadText(content, name)),
               this.button("Discard", () => { this.keep.remove("unsaved", path); box.remove(); }));
    this.body.prepend(box);
  }

  /* Another conversation is shown (PR #99 review C2): no answer still on its way is applied, and a page of the
   * last one's changes goes with its controls. */
  reset() {
    this.ticket += 1;
    const page = this.page;
    this.closeDoc();
    if (!page) return;
    this.tools.replaceChildren();
    this.crumbs.replaceChildren();
    this.body.replaceChildren();
  }

  /* HIMARI started or stopped: told beside the editor; a review page's controls wait while it works (§6). */
  workingChanged() {
    if (this.doc) this.paintState();
    if (!this.page || !this.working()) return;
    for (const control of this.reviewControls) control.disabled = true;
  }

  /* ~ as the server resolves it, so paths under it are shown from ~. */
  async learnHome() {
    if (this.home !== null) return;
    try {
      const found = await this.api("GET", "/api/machine/stat?path=~");
      if (this.home === null) this.home = found.path;
    } catch (ignored) { /* shown from / */ }
  }

  /* Every move asks before unsaved text goes (mayLeave): none skips it (PR #95 re-review C1). */
  async openFolder(path) {
    if (!(await this.mayLeave())) return;
    const ticket = ++this.ticket;
    this.closeDoc();
    this.file = null;
    this.busy(path);
    try {
      await this.learnHome();
      const listing = await this.api("GET", `/api/machine/list?${query(path)}`);
      if (ticket !== this.ticket) return;
      if (path === "~" && this.home === null) this.home = listing.path;
      this.folder = listing.path;
      this.body.removeAttribute("aria-busy");
      this.paintCrumbs(listing.path, false);
      this.paintTools(null, listing);
      const list = this.el("ul", "files-list");
      this.body.replaceChildren(list);
      this.appendRows(list, listing);
      if (!listing.entries.length) this.body.append(this.el("p", "files-note", "This folder is empty."));
      if (this.onOpenFolder) this.onOpenFolder(listing);
    } catch (error) {
      if (ticket === this.ticket) this.failed(path, error);
    }
  }

  appendRows(list, listing) {
    for (const entry of listing.entries) list.append(this.row(listing.path, entry));
    if (listing.next === null) return;
    const more = this.el("button", "link-button files-more", "Load more");
    more.type = "button";
    more.addEventListener("click", async () => {
      const ticket = this.ticket;
      more.disabled = true;
      try {
        const page = await this.api("GET", `/api/machine/list?${query(listing.path)}&after=${encodeURIComponent(listing.next)}`);
        if (ticket !== this.ticket) return;
        more.remove();
        this.appendRows(list, page);
      } catch (error) {
        more.disabled = false;
        more.textContent = `Load more (${error.message})`;
      }
    });
    list.after(more);
  }

  row(folder, entry) {
    const path = join(folder, entry.name);
    const li = this.el("li", "files-row");
    const button = this.el("button", "files-open");
    button.type = "button";
    const kind = entry.kind === "directory" ? "directory" : "file";
    const name = this.el("span", "files-name", entry.name);
    button.append(icon(KIND_ICON[kind]), name);
    if (this.changedUnder(path, kind === "directory")) {
      const dot = this.el("span", "files-changed-dot");
      dot.title = kind === "directory" ? "HIMARI changed files in here: see Changes" : "Changed by HIMARI: see Changes";
      name.append(dot);
    }
    const meta = [entry.kind === "file" ? humanSize(entry.size) : "", shortDate(entry.modified)].filter(Boolean);
    button.append(this.el("span", "files-meta", meta.join(" · ")));
    button.title = path;
    button.addEventListener("click", () => this.follow(path, entry.kind));
    li.append(button);
    const given = entry.kind === "directory" && this.folderRowActions ? this.folderRowActions(path) : [];
    if (entry.kind === "file") li.append(this.menu(path, entry.name, [], true));
    else if (given.length) li.append(this.menu(path, entry.name, given, false));
    else li.append(this.el("span", "files-actions-space")); // nothing to do with it here: no menu (PR #93 review N1)
    return li;
  }

  /* A link is opened as what it points to (the server resolves and checks it).
   * Its answer, or its failure, is dropped once anything else was opened
   * meanwhile (PR #92 review B2). */
  async follow(path, kind) {
    if (kind === "directory") return this.openFolder(path);
    if (kind !== "symlink") return this.openFile(path);
    if (!(await this.mayLeave())) return null;
    const ticket = ++this.ticket;
    this.busy(path);
    try {
      const target = await this.api("GET", `/api/machine/stat?${query(path)}`);
      if (ticket !== this.ticket) return null;
      return target.kind === "directory" ? this.openFolder(target.path) : this.openFile(target.path);
    } catch (error) {
      if (ticket === this.ticket) this.failed(path, error);
      return null;
    }
  }

  /* A row's ⋯: the given actions, then (a file only) Download, and Copy path. */
  menu(path, name, given, isFile) {
    const actions = this.el("details", "conversation-actions files-actions");
    const toggle = this.el("summary", null, "⋯");
    toggle.setAttribute("aria-label", `Actions for ${name}`);
    toggle.title = `Actions for ${name}`;
    const list = this.el("div", "conversation-action-list");
    for (const [label, action] of given) {
      const control = this.el("button", "link-button", label);
      control.type = "button";
      control.addEventListener("click", () => { actions.open = false; action(); });
      list.append(control);
    }
    if (isFile) list.append(this.download(path, "Download"));
    list.append(this.copyPath(path, () => { actions.open = false; }));
    actions.append(toggle, list);
    actions.addEventListener("toggle", () => {
      if (!actions.open) return;
      for (const other of this.body.querySelectorAll("details[open]")) if (other !== actions) other.open = false;
    });
    return actions;
  }

  download(path, label) {
    const link = this.el("a", "link-button files-download", label);
    link.href = `/api/machine/download?${query(path)}`;
    link.setAttribute("download", "");
    return link;
  }

  copyPath(path, done) {
    const button = this.el("button", "link-button", "Copy path");
    button.type = "button";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(path);
        button.textContent = "Copied";
      } catch (ignored) {
        button.textContent = "Copy failed";
      }
      setTimeout(() => { button.textContent = "Copy path"; if (done) done(); }, 1000);
    });
    return button;
  }

  async openFile(path) {
    if (!(await this.mayLeave())) return;
    const ticket = ++this.ticket;
    this.closeDoc();
    this.busy(path);
    try {
      await this.learnHome();
      const shown = await this.api("GET", `/api/machine/file?${query(path)}`);
      if (ticket !== this.ticket) return;
      this.file = shown.path;
      this.fileKind = shown.kind;
      this.folder = shown.path.slice(0, shown.path.lastIndexOf("/")) || "/";
      this.body.removeAttribute("aria-busy");
      this.paintCrumbs(shown.path, true);
      this.paintTools(shown);
      if (shown.kind === "text" && shown.editable) await this.edit(shown, ticket);
      else if (shown.kind === "pdf" && shown.size <= PDF_MAX_BYTES) await this.showPdf(shown, ticket);
      else this.body.replaceChildren(this.view(shown));
      if (!(shown.kind === "text" && shown.editable)) this.offerKeptAside(shown.path, ticket, false);
      if (this.onOpenFile) this.onOpenFile(shown);
    } catch (error) {
      if (ticket !== this.ticket) return;
      this.failed(path, error);
      if (error.code === "not_found") this.offerKeptAside(path, ticket, true);
    }
  }

  /* ----------------------------------------------------------- a PDF (V3-P77 §3) */

  async showPdf(shown, ticket, page = 1) {
    const box = this.el("div", "files-view");
    box.append(this.el("p", "files-note", `Opening ${humanSize(shown.size)}…`));
    this.body.replaceChildren(box);
    let made;
    try {
      made = await this.loadPdfView(shown, null);
    } catch (error) {
      if (ticket === this.ticket) this.pdfFailed(shown, error.message);
      return;
    }
    if (ticket !== this.ticket) {
      made.view.destroy();
      return;
    }
    this.installPdf(shown, made, page);
  }

  /* A PDF read and opened in a view of its own, not yet shown — the one shown stays until this is ready
   * (PR #104 review P1-2). ``carried``: unsaved ink put on it (§5.5); {view, identity, left}. Throws with a
   * message to show. */
  async loadPdfView(shown, carried) {
    let module, pdfjs, read;
    try {
      [module, read] = await Promise.all([this.loadPdf(), this.readFile(shown.path, PDF_MAX_BYTES)]);
      pdfjs = await module.loadPdfjs();
    } catch (error) {
      throw new Error(error.status ? error.message : "The PDF view could not be loaded.");
    }
    const view = new module.PdfView({
      pdfjs, el: this.el, bytes: read.bytes,
      onPage: (number, pages) => { if (this.pdf && this.pdf.view === view) this.paintPage(number, pages); },
      onInk: () => { if (this.pdf && this.pdf.view === view) this.paintInk(); },
    });
    try {
      await view.open();
      const left = carried ? await view.carryIn(carried) : 0;
      return { view, identity: read.identity, left };
    } catch (error) {
      view.destroy();
      throw error;
    }
  }

  /* The view shown: fitted to the pane, on ``page``, its tool picked, checked on disk from now on. */
  installPdf(shown, { view, identity, left }, page) {
    const bar = this.el("div", "files-bar");
    bar.hidden = true;
    // The identity of the bytes read, which a save is made against (V3-P77 §5.3); the shown one if not told.
    const pdf = { path: shown.path, name: shown.path.slice(shown.path.lastIndexOf("/") + 1),
                  identity: identity || shown.identity, view, bar, saving: false, pollTimer: null };
    this.pdf = pdf;
    this.body.replaceChildren(bar, view.element);
    view.show(page);
    const tool = view.writable ? this.inkTool() : null;
    view.setTool(tool);
    if (tool) this.tellSigned();
    if (left) {
      this.showPdfNote(`${left} unsaved stroke${left === 1 ? " was" : "s were"} on pages this version does not have, ` +
                       "and could not be kept.");
    }
    this.paintPage(view.current + 1, view.numPages);
    this.paintInk();
    this.schedulePdfPoll(pdf);
  }

  /* A signed PDF, told once a drawing tool is picked — before any stroke, so no word above the pages moves
   * them under a pen (PR #104 review P2). */
  tellSigned() {
    const pdf = this.pdf;
    if (!pdf || !pdf.view.signed || pdf.toldSigned) return;
    pdf.toldSigned = true;
    this.showPdfNote(SIGNED_NOTICE);
  }

  /* ----------------------------------------------------------- ink (V3-P77 §4, §5) */

  /* The tool picked last, remembered in this browser (§4.2); none where storage is refused. */
  inkTool() {
    try {
      const kept = JSON.parse(localStorage.getItem(INK_TOOL_KEY) || "null");
      if (kept && INK_TOOLS[kept.tool]) return { ...INK_TOOLS[kept.tool], ...kept };
    } catch (ignored) { /* none */ }
    return null;
  }

  pickInkTool(tool) {
    if (!this.pdf) return;
    this.pdf.view.setTool(tool);
    if (tool) this.tellSigned();
    try {
      if (tool) localStorage.setItem(INK_TOOL_KEY, JSON.stringify(tool));
      else localStorage.removeItem(INK_TOOL_KEY);
    } catch (ignored) { /* not remembered */ }
    this.paintInk();
  }

  /* The ink tools in the head: scroll, pen, highlighter, eraser; the pen's colours and widths; undo, redo;
   * the save state and Save. All off until the PDF is open and can be written on. */
  inkTools() {
    const group = this.el("div", "files-ink-tools");
    group.hidden = true;
    const button = (label, symbol, action) => {
      const control = this.el("button", "files-ink-button");
      control.type = "button";
      control.title = label;
      control.setAttribute("aria-label", label);
      control.append(icon(symbol));
      control.addEventListener("click", action);
      return control;
    };
    const tools = {
      none: button("Scroll (no drawing)", "#ink-hand", () => this.pickInkTool(null)),
      pen: button("Pen", "#ink-pen", () => this.pickInkTool({ ...this.penChoice(), tool: "pen" })),
      highlighter: button("Highlighter", "#ink-marker", () => this.pickInkTool({ ...INK_TOOLS.highlighter })),
      eraser: button("Eraser: a whole stroke", "#ink-eraser", () => this.pickInkTool({ ...INK_TOOLS.eraser })),
    };
    const swatches = this.el("span", "files-ink-options");
    for (const [color, label] of [["black", "Black"], ["blue", "Blue"], ["red", "Red"]]) {
      const swatch = this.el("button", `files-ink-swatch is-${color}`);
      swatch.type = "button";
      swatch.title = `${label} pen`;
      swatch.setAttribute("aria-label", swatch.title);
      swatch.dataset.color = color;
      swatch.addEventListener("click", () => this.pickInkTool({ ...this.penChoice(), tool: "pen", color }));
      swatches.append(swatch);
    }
    const width = this.el("button", "files-ink-width", "");
    width.type = "button";
    width.addEventListener("click", () => {
      const now = this.penChoice();
      this.pickInkTool({ ...now, tool: "pen", size: now.size === "bold" ? "fine" : "bold" });
    });
    swatches.append(width);
    const undo = button("Undo (Ctrl+Z)", "#ink-undo", () => this.pdf && this.pdf.view.undo());
    const redo = button("Redo (Shift+Ctrl+Z)", "#ink-redo", () => this.pdf && this.pdf.view.redo());
    const dot = this.el("span", "files-state-dot");
    const state = this.el("span", "files-state", "Saved");
    const save = this.el("button", "primary-button files-save", "Save");
    save.type = "button";
    save.disabled = true;
    save.addEventListener("click", () => this.savePdf());
    group.append(...Object.values(tools), swatches, undo, redo, dot, state, save);
    this.ink = { group, tools, swatches, width, undo, redo, dot, state, save };
    return group;
  }

  /* The pen's colour and width as last picked (black, fine at first). */
  penChoice() {
    const kept = this.inkTool();
    return kept && kept.tool === "pen" ? kept : { ...INK_TOOLS.pen };
  }

  /* The tools as the PDF is now: the one picked, Undo and Redo while there is one, Save while unsaved. */
  paintInk(note = null) {
    const pdf = this.pdf;
    const ink = this.ink;
    if (!pdf || !ink) return;
    const view = pdf.view;
    if (!view.writable) {
      ink.group.hidden = !view.readOnly;
      for (const control of [...Object.values(ink.tools), ink.undo, ink.redo, ink.save, ink.width]) control.hidden = true;
      ink.swatches.hidden = true;
      ink.dot.hidden = true;
      ink.state.textContent = view.readOnly ? "View only" : "";
      ink.state.title = view.readOnly || "";
      return;
    }
    ink.group.hidden = false;
    const picked = view.tool ? view.tool.tool : "none";
    for (const [name, control] of Object.entries(ink.tools)) control.setAttribute("aria-pressed", String(name === picked));
    const pen = this.penChoice();
    ink.swatches.hidden = picked !== "pen";
    for (const swatch of ink.swatches.children) {
      if (swatch.dataset && swatch.dataset.color) swatch.setAttribute("aria-pressed", String(swatch.dataset.color === pen.color));
    }
    ink.width.textContent = pen.size === "bold" ? "Bold" : "Fine";
    ink.width.title = "Pen width";
    ink.undo.disabled = pdf.saving || !view.ink.done.length;
    ink.redo.disabled = pdf.saving || !view.ink.undone.length;
    const dirty = view.ink.dirty;
    ink.dot.className = `files-state-dot${dirty ? " is-dirty" : ""}`;
    ink.state.textContent = note || (pdf.saving ? "Saving…" : dirty ? "Unsaved" : "Saved");
    ink.state.title = note || "";
    ink.save.disabled = pdf.saving || !dirty;
  }

  /* §5: the ink drawn and erased since it was read, added after the PDF's bytes on the server. */
  /* PR #104 review P1-1: no drawing, erasing or undoing from the moment the ink to save is taken until the
   * server's answer is read back: what is saved is all there is, and nothing drawn meanwhile is lost. A
   * stroke still being drawn is finished first. */
  async savePdf() {
    const pdf = this.pdf;
    if (!pdf || pdf.saving) return;
    if (pdf.view.active) {
      this.paintInk("Finish the stroke, then save");
      return;
    }
    pdf.saving = true;
    pdf.view.saving = true;
    this.paintInk();
    const done = (note) => {
      pdf.saving = false;
      pdf.view.saving = false;
      this.paintInk(note);
    };
    let increment;
    try {
      increment = await pdf.view.increment();
    } catch (error) {
      if (this.pdf === pdf) done(error.message);
      return;
    }
    if (this.pdf !== pdf) return;
    if (!increment) {
      done();
      return;
    }
    let answer;
    try {
      answer = await this.appendPdf(pdf.path, increment, pdf.identity);
    } catch (error) {
      if (this.pdf !== pdf) return;
      done(error.code === "changed" ? null : error.message || "Not saved");
      if (error.code === "changed") this.showPdfChanged(true);
      return;
    }
    if (this.pdf !== pdf) return;
    pdf.identity = answer.identity;
    try {
      await pdf.view.saved(increment);
    } catch (ignored) { // saved on the server, not read back here: read it again (no ink is unsaved now)
      done();
      await this.reopenPdf(false);
      return;
    }
    pdf.bar.hidden = true;
    done(answer.previous_kept ? null : "Saved, but its previous version could not be kept");
  }

  /* A word above the pages (a signed PDF; strokes that could not be kept), until dismissed. */
  showPdfNote(text) {
    const pdf = this.pdf;
    pdf.bar.replaceChildren(this.el("p", null, text));
    const ok = this.el("button", "link-button", "OK");
    ok.type = "button";
    ok.addEventListener("click", () => { pdf.bar.hidden = true; });
    pdf.bar.append(ok);
    pdf.bar.hidden = false;
  }

  /* §5.5: the PDF changed on the server since it was read, with ink unsaved here: the user picks. ``problem``:
   * why the server's version could not be put in its place (the ink stays). */
  showPdfChanged(refused, problem = null) {
    const pdf = this.pdf;
    if (!pdf) return;
    pdf.bar.replaceChildren(this.el("p", null, problem ? `${problem} Your ink is still here.` : refused
      ? "Not saved: this PDF was changed on the server since you opened it. Your ink is still here."
      : "This PDF was changed on the server since you opened it. Your ink is still here."));
    const actions = [["Load the server's version and keep my ink", () => this.reopenPdf(true)],
                     ["Discard my ink", () => this.reopenPdf(false)]];
    for (const [label, action] of actions) {
      const button = this.el("button", "link-button", label);
      button.type = "button";
      button.addEventListener("click", action);
      pdf.bar.append(button);
    }
    pdf.bar.hidden = false;
    this.paintInk();
  }

  schedulePdfPoll(pdf) {
    clearTimeout(pdf.pollTimer);
    pdf.pollTimer = setTimeout(() => this.pollPdf(pdf), POLL_MS);
  }

  /* §5.5: checked on disk while it shows: a changed PDF is read again unasked when no ink is unsaved; with
   * ink unsaved, the user is told and picks. */
  async pollPdf(pdf) {
    if (this.pdf !== pdf) return;
    try {
      await this.checkPdf();
    } finally {
      if (this.pdf === pdf) this.schedulePdfPoll(pdf);
    }
  }

  /* The server's version, on the page it was at; ``keep``: the unsaved ink put on it (§5.5). The view shown
   * stays — its ink untouched, nothing drawn on it meanwhile — until the new one is ready (PR #104 review
   * P1-2); if that cannot be, the ink stays where it is and the user is told why. */
  async reopenPdf(keep) {
    const pdf = this.pdf;
    if (!pdf || pdf.reopening) return;
    const page = pdf.view.current + 1;
    const ticket = ++this.ticket;
    pdf.reopening = true;
    pdf.saving = true; // no save, no check on disk
    pdf.view.saving = true; // no drawing: what is carried is all there is
    this.paintInk();
    const carried = keep ? pdf.view.ink.carry() : null;
    const stay = (problem) => {
      pdf.reopening = false;
      pdf.saving = false;
      pdf.view.saving = false;
      if (this.pdf !== pdf) return;
      this.paintInk();
      this.showPdfChanged(false, problem);
    };
    let shown;
    try {
      shown = await this.api("GET", `/api/machine/file?${query(pdf.path)}`);
    } catch (error) {
      if (ticket !== this.ticket) return;
      if (keep) return stay(`The server's version could not be read (${error.message}).`);
      this.closeDoc();
      this.failed(pdf.path, error);
      return;
    }
    if (ticket !== this.ticket) return;
    if (shown.kind !== "pdf" || shown.size > PDF_MAX_BYTES) {
      if (keep) return stay("The server's version is not a PDF that can be opened here.");
      this.closeDoc();
      this.fileKind = shown.kind;
      this.paintTools(shown);
      this.body.replaceChildren(this.view(shown));
      return;
    }
    let made;
    try {
      made = await this.loadPdfView(shown, carried);
    } catch (error) {
      if (ticket !== this.ticket) return;
      if (keep) return stay(`The server's version could not be opened: ${error.message}`);
      this.closeDoc();
      this.fileKind = shown.kind;
      this.paintTools(shown);
      this.pdfFailed(shown, error.message);
      return;
    }
    if (ticket !== this.ticket) {
      made.view.destroy();
      return;
    }
    this.closeDoc();
    this.fileKind = shown.kind;
    this.paintTools(shown);
    this.installPdf(shown, made, page);
  }

  pdfFailed(shown, message) {
    const box = this.el("div", "files-view");
    box.append(this.el("p", "files-note is-error", message), this.download(shown.path, "Download"));
    this.body.replaceChildren(box);
  }

  /* "3 / 12" in the head; pressed, a page number to go to. */
  paintPage(number, pages) {
    if (!this.pageLabel) return;
    this.pageLabel.textContent = `${number} / ${pages}`;
    this.pageLabel.hidden = false;
  }

  pagePicker() {
    const label = this.el("button", "link-button files-page", "");
    label.type = "button";
    label.title = "Go to a page";
    label.hidden = true; // until the PDF is open
    label.addEventListener("click", () => {
      if (!this.pdf) return;
      const view = this.pdf.view;
      const input = this.el("input", "files-page-input");
      input.type = "number";
      input.min = "1";
      input.max = String(view.numPages);
      input.value = String(view.current + 1);
      input.setAttribute("aria-label", `Page, 1 to ${view.numPages}`);
      let done = false;
      const finish = (go) => {
        if (done) return;
        done = true;
        const number = Number(input.value);
        input.replaceWith(label);
        if (go && Number.isInteger(number) && this.pdf && this.pdf.view === view) view.goTo(number);
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") finish(true);
        if (event.key === "Escape") finish(false);
      });
      input.addEventListener("blur", () => finish(true));
      label.replaceWith(input);
      input.focus();
      input.select();
    });
    return label;
  }

  /* After the agent's turn, and every few seconds: read again only if the file changed, on the page it was at;
   * with ink unsaved, never unasked (§5.5). */
  async checkPdf() {
    const pdf = this.pdf;
    if (!pdf || pdf.saving) return;
    try {
      const now = await this.api("GET", `/api/machine/stat?${query(pdf.path)}`);
      if (this.pdf !== pdf || pdf.saving || sameIdentity(now.identity, pdf.identity)) return;
    } catch (error) {
      if (this.pdf !== pdf || error.code !== "not_found") return;
    }
    if (pdf.view.ink.dirty) {
      if (pdf.bar.hidden) this.showPdfChanged(false);
      return;
    }
    await this.reopenPdf(false);
  }

  /* ----------------------------------------------------------- the editor (§7) */

  async edit(shown, ticket) {
    const name = shown.path.slice(shown.path.lastIndexOf("/") + 1);
    let module;
    try {
      module = await this.loadEditor();
    } catch (error) {
      if (ticket !== this.ticket) return;
      const box = this.view(shown); // read only, as before the editor
      box.prepend(this.el("p", "files-note is-error", "The editor could not be loaded; the file is shown read-only."));
      this.body.replaceChildren(box);
      return;
    }
    const kept = await this.keptFor(shown.path); // read before the editor: nothing typed meets the offer (C2)
    if (ticket !== this.ticket) return;
    const bar = this.el("div", "files-bar");
    bar.hidden = true;
    const area = this.el("div", "files-editor");
    this.body.replaceChildren(bar, area);
    // ``revision`` counts every change of the text: a read of the server's version started before one is
    // never applied over it (PR #95 review B1).
    // ``saved``: the saved text as the editor has it; ``lineBreak``: the file's, written back by a save.
    const doc = { path: shown.path, name, identity: shown.identity, saved: editorText(shown.text),
                  lineBreak: lineBreakOf(shown.text), editor: null, dirty: false, saving: false, pollTimer: null, bar,
                  module, revision: 0, keepTimer: null, offer: null };
    this.doc = doc;
    doc.editor = module.createEditor(area, {
      text: shown.text, name,
      onChange: () => {
        if (this.doc !== doc) return;
        doc.revision += 1;
        this.setDirty(doc.editor.text() !== doc.saved);
        this.keepLater(doc);
      },
      onSave: () => { if (this.doc === doc) this.save(); },
      onAsk: this.onAsk ? () => (this.doc === doc ? this.ask() : null) : null,
    });
    this.paintState();
    if (kept !== null) this.offerKept(doc, kept);
    this.schedulePoll(doc);
  }

  /* V3-P76 §3.1: the selection, or with none the whole file, as a reference. The server checks it
   * against the file, so only saved text is pointed at (Q1). */
  async ask() {
    const doc = this.doc;
    if (doc === null || !this.onAsk) return;
    if (doc.dirty || doc.saving) {
      this.paintState("Save first: HIMARI reads the saved file");
      return;
    }
    const picked = doc.editor.selection();
    const reference = { path: doc.path, name: doc.name, identity: doc.identity, whole: picked.from === picked.to };
    if (reference.whole) {
      this.onAsk(reference);
      return;
    }
    let sha256;
    try {
      sha256 = await sha256Hex(picked.text);
    } catch (ignored) {
      this.paintState("Ask HIMARI needs the page over HTTPS");
      return;
    }
    if (this.doc !== doc || doc.dirty) return; // typed in meanwhile: that selection is gone
    this.onAsk({ ...reference, start: picked.from, end: picked.to, sha256,
                 lines: [picked.fromLine, picked.fromColumn, picked.toLine, picked.toColumn] });
  }

  /* The whole file from its ⋯ menu: any file, editable or not. */
  askWhole(shown) {
    if (this.doc !== null && this.doc.path === shown.path) {
      if (this.doc.dirty || this.doc.saving) {
        this.paintState("Save first: HIMARI reads the saved file");
        return;
      }
      this.onAsk({ path: shown.path, name: this.doc.name, identity: this.doc.identity, whole: true });
      return;
    }
    this.onAsk({ path: shown.path, name: shown.path.slice(shown.path.lastIndexOf("/") + 1),
                 identity: shown.identity, whole: true });
  }

  /* A reference's place: its file, with ``start``–``end`` selected when given (§3.1). */
  async reveal(path, start = null, end = null) {
    if (this.file !== path || this.doc === null || this.doc.path !== path) await this.openFile(path);
    if (start !== null && this.doc !== null && this.doc.path === path) this.doc.editor.select(start, end);
  }

  setDirty(dirty) {
    if (this.doc === null) return;
    this.doc.dirty = dirty;
    this.paintState();
  }

  /* The save state beside the file's name: a dot and a word; Save is on only with something to save. */
  paintState(note = null) {
    const doc = this.doc;
    if (doc === null || !this.stateLabel) return;
    const text = doc.saving ? "Saving…" : doc.dirty ? "Unsaved" : "Saved";
    this.stateDot.className = `files-state-dot${doc.dirty ? " is-dirty" : ""}`;
    this.stateLabel.textContent = note || text;
    this.saveButton.disabled = doc.saving || !doc.dirty;
    this.workingNote.hidden = !this.working();
  }

  async save({ over = null } = {}) {
    const doc = this.doc;
    if (doc === null || doc.saving) return;
    const text = doc.editor.text();
    const content = fileText(text, doc.lineBreak);
    if (new TextEncoder().encode(content).length > EDIT_MAX_BYTES) {
      this.paintState("Too large to save from here (at most 1 MiB)");
      return;
    }
    doc.saving = true;
    this.paintState();
    try {
      const answer = await this.api("PUT", "/api/machine/file",
                                    { path: doc.path, content, expected: over || doc.identity });
      if (this.doc !== doc) return;
      doc.identity = answer.identity;
      doc.saved = text;
      doc.saving = false;
      doc.dirty = doc.editor.text() !== text; // typed on while it was saving
      if (doc.dirty) this.keepNow(doc);
      else this.dropKept(doc);
      this.hideBar();
      this.paintState(answer.previous_kept ? null : "Saved, but its previous version could not be kept");
    } catch (error) {
      if (this.doc !== doc) return;
      doc.saving = false;
      if (error.code === "changed") await this.changedOrGone(doc, true);
      else this.paintState(error.message || "Not saved");
    }
  }

  /* V3-P78 §4.2: a save refused as changed — or the file is gone, which the runner also answers as changed. */
  async changedOrGone(doc, refused) {
    try {
      await this.api("GET", `/api/machine/stat?${query(doc.path)}`);
    } catch (error) {
      if (this.doc === doc && error.code === "not_found" && doc.dirty) {
        this.showGone();
        return;
      }
    }
    if (this.doc === doc) this.showChanged(refused);
  }

  /* A ``not_found`` from a read of the open file: the gone bar while there is unsaved text (§4.2, C5, N7). */
  goneOr(doc, error) {
    if (this.doc !== doc) return;
    if (error.code === "not_found" && doc.dirty) this.showGone();
    else if (error.code === "not_found") this.paintState("This file is no longer on the server");
    else this.paintState(error.message);
  }

  /* §4.2: the file is gone (deleted or moved) and the text is still here: make it again, or download it. */
  showGone(note = null) {
    const doc = this.doc;
    if (doc === null) return;
    const bar = doc.bar;
    bar.replaceChildren(this.el("p", null, note || "This file is no longer on the server (deleted or moved). Your "
                                + "text is still here. Saving it here again makes it as a new file."));
    if (note === null) bar.append(this.button("Save it here again", () => this.recreate()));
    bar.append(this.button("Download my text", () => downloadText(fileText(doc.editor.text(), doc.lineBreak), doc.name)));
    bar.hidden = false;
    this.paintState();
  }

  async recreate() {
    const doc = this.doc;
    if (doc === null || doc.saving) return;
    const text = doc.editor.text();
    const content = fileText(text, doc.lineBreak);
    if (new TextEncoder().encode(content).length > EDIT_MAX_BYTES) {
      this.paintState("Too large to save from here (at most 1 MiB)");
      return;
    }
    doc.saving = true;
    this.paintState();
    try {
      const made = await this.api("POST", "/api/machine/file/install", { path: doc.path, content });
      if (this.doc !== doc) return;
      doc.saving = false;
      if (made.identity === null) { // placed, but not seen after: open it as it is now
        this.dropKept(doc);
        doc.dirty = false;
        await this.openFile(doc.path);
        return;
      }
      doc.identity = made.identity;
      doc.saved = text;
      doc.dirty = doc.editor.text() !== text;
      if (doc.dirty) this.keepNow(doc);
      else this.dropKept(doc);
      this.hideBar();
      this.paintState("Saved as a new file");
    } catch (error) {
      if (this.doc !== doc) return;
      doc.saving = false;
      if (error.code === "changed") this.showChanged(true); // a file of that name was made meanwhile
      else if (error.code === "not_found" || error.code === "not_directory") {
        this.showGone("The folder is gone too; download your text.");
      } else if (error.code === "unsupported") {
        this.showGone("This folder can't take a new file safely from here; download your text.");
      } else this.paintState(error.message || "Not saved");
    }
  }

  hideBar() {
    if (this.doc) this.doc.bar.hidden = true;
  }

  /* The file changed on disk since it was read: the user picks (§7.2). ``refused``: a save was refused for it. */
  showChanged(refused) {
    const doc = this.doc;
    if (doc === null) return;
    const bar = doc.bar;
    bar.replaceChildren(this.el("p", null, refused
      ? "Not saved: this file was changed on the server since you opened it. Your text is still here."
      : "This file was changed on the server since you opened it. Your text is still here."));
    const actions = [["Compare", () => this.compareWithDisk()],
                     ["Load the server's version", () => this.loadDisk()],
                     ["Save mine over it", () => this.saveOver()]];
    for (const [label, action] of actions) {
      const button = this.el("button", "link-button", label);
      button.type = "button";
      button.addEventListener("click", action);
      bar.append(button);
    }
    bar.hidden = false;
    this.paintState();
  }

  async disk() {
    return this.api("GET", `/api/machine/file?${query(this.doc.path)}`);
  }

  /* The server's version into the editor. ``since``: the text's revision when an automatic read began; if
   * it was typed in since, the text stays and the user is told instead (PR #95 review B1). */
  async loadDisk({ since = null } = {}) {
    const doc = this.doc;
    if (doc === null) return;
    try {
      const shown = await this.disk();
      if (this.doc !== doc) return;
      if (since !== null && (doc.revision !== since || doc.dirty)) {
        this.showChanged(false);
        return;
      }
      if (shown.kind !== "text" || !shown.editable) {
        this.paintState("The server's version can no longer be edited here");
        return;
      }
      doc.saved = editorText(shown.text);
      doc.lineBreak = lineBreakOf(shown.text);
      doc.identity = shown.identity;
      doc.editor.setText(shown.text);
      doc.dirty = doc.editor.text() !== doc.saved;
      if (!doc.dirty) this.dropKept(doc);
      this.hideBar();
      this.paintState();
    } catch (error) {
      this.goneOr(doc, error);
    }
  }

  async saveOver() {
    const doc = this.doc;
    if (doc === null) return;
    try {
      const now = await this.api("GET", `/api/machine/stat?${query(doc.path)}`);
      if (this.doc !== doc) return;
      await this.save({ over: now.identity }); // the server's version is kept as a previous version
    } catch (error) {
      this.goneOr(doc, error);
    }
  }

  async compareWithDisk() {
    const doc = this.doc;
    if (doc === null) return;
    try {
      const shown = await this.disk();
      if (this.doc !== doc) return;
      this.diffDialog(`${doc.name}: the server's version (left) and yours (right)`,
                      shown.kind === "text" ? shown.text : "", doc.editor.text());
    } catch (error) {
      this.goneOr(doc, error);
    }
  }

  diffDialog(title, left, right, extra = []) {
    const dialog = this.el("dialog", "files-dialog");
    const head = this.el("div", "files-dialog-head");
    head.append(this.el("h2", null, title));
    const close = this.el("button", "icon-button", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    head.append(close);
    const area = this.el("div", "files-diff");
    const foot = this.el("div", "files-dialog-foot");
    for (const button of extra) foot.append(button);
    dialog.append(head, area, foot);
    this.root.append(dialog);
    const diff = this.doc.module.createDiff(area, { left, right, name: this.doc.name });
    const done = () => { diff.destroy(); dialog.remove(); };
    close.addEventListener("click", () => { if (dialog.close) dialog.close(); done(); });
    dialog.addEventListener("cancel", done);
    for (const button of extra) button.addEventListener("click", () => { if (dialog.close) dialog.close(); done(); });
    if (dialog.showModal) dialog.showModal();
    return dialog;
  }

  /* Previous versions (§7.4): compare one with the editor's text, or put it in the editor to save. */
  async showVersions() {
    const doc = this.doc;
    if (doc === null) return;
    let items;
    try {
      items = (await this.api("GET", `/api/machine/versions?${query(doc.path)}`)).items;
    } catch (error) {
      if (this.doc === doc) this.paintState(error.message);
      return;
    }
    if (this.doc !== doc) return;
    const dialog = this.el("dialog", "files-dialog");
    const head = this.el("div", "files-dialog-head");
    head.append(this.el("h2", null, `Previous versions of ${doc.name}`));
    const close = this.el("button", "icon-button", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    head.append(close);
    const list = this.el("ul", "files-list files-versions");
    if (!items.length) list.append(this.el("li", "files-note", "None yet: a version is kept each time the file is saved from here."));
    for (const item of items) {
      const li = this.el("li", "files-row files-version");
      li.append(this.el("span", "files-name", new Date(item.saved_at).toLocaleString()),
                this.el("span", "files-meta", `${VERSION_REASONS[item.reason] || "before a save"} · ${humanSize(item.size)}`));
      const compare = this.el("button", "link-button", "Compare");
      compare.type = "button";
      compare.addEventListener("click", async () => {
        const version = await this.versionText(item.id);
        if (version === null || this.doc !== doc) return;
        if (dialog.close) dialog.close();
        dialog.remove();
        const restore = this.el("button", "primary-button", "Put this version in the editor");
        restore.type = "button";
        restore.addEventListener("click", () => this.restore(version));
        this.diffDialog(`${doc.name}: this version (left) and the editor's text (right)`, version, doc.editor.text(), [restore]);
      });
      const restore = this.el("button", "link-button", "Put in the editor");
      restore.type = "button";
      restore.addEventListener("click", async () => {
        const version = await this.versionText(item.id);
        if (version === null || this.doc !== doc) return;
        if (dialog.close) dialog.close();
        dialog.remove();
        this.restore(version);
      });
      li.append(compare, restore);
      list.append(li);
    }
    dialog.append(head, list);
    this.root.append(dialog);
    close.addEventListener("click", () => { if (dialog.close) dialog.close(); dialog.remove(); });
    dialog.addEventListener("cancel", () => dialog.remove());
    if (dialog.showModal) dialog.showModal();
  }

  async versionText(id) {
    try {
      return (await this.api("GET", `/api/machine/versions/${encodeURIComponent(id)}?${query(this.doc.path)}`)).text;
    } catch (error) {
      this.paintState(error.message);
      return null;
    }
  }

  /* Back into the editor, unsaved: Save keeps it (and keeps what it replaces). */
  restore(text) {
    if (this.doc === null) return;
    this.doc.editor.setText(text);
    this.setDirty(editorText(text) !== this.doc.saved);
    this.paintState("Put in the editor — Save to keep it");
  }

  async deleteFile() {
    const doc = this.doc;
    const path = doc ? doc.path : this.file;
    if (path === null) return;
    let now;
    try {
      now = await this.api("GET", `/api/machine/stat?${query(path)}`);
    } catch (error) {
      this.paintState(error.message);
      return;
    }
    const name = path.slice(path.lastIndexOf("/") + 1);
    // A PDF up to 64 MiB is deleted only with its content kept (V3-P77 §6.3; PR #103 review P3) — told by the
    // file's kind as the server read it, whether the view could open it or not (PR #103 re-review).
    const keptPdf = this.fileKind === "pdf" && now.size <= PDF_MAX_BYTES;
    const big = now.size > EDIT_MAX_BYTES && !keptPdf;
    const lines = big ? ["This file is over 1 MiB: no previous version of it is kept, so it cannot be put back."]
      : ["Its content is kept as a previous version on the server for 30 days."];
    if (doc && doc.dirty) lines.push("Your unsaved changes are dropped too.");
    if (this.pdf && this.pdf.view.ink.dirty) lines.push("Your unsaved ink is dropped too.");
    if (!(await this.confirm(`Delete ${name}?`, lines, "Delete"))) return;
    if (big && !(await this.confirm(`Delete ${name} for good?`, ["It cannot be put back."], "Delete for good"))) return;
    const ticket = this.ticket; // anything opened meanwhile stays (PR #95 re-review C1)
    try {
      const gone = await this.api("POST", "/api/machine/file/delete", { path, expected: now.identity });
      if (doc) this.dropKept(doc); // its unsaved text was dropped with it (told above)
      if (this.doc === doc) this.closeDoc();
      if (this.ticket === ticket) await this.openFolder(path.slice(0, path.lastIndexOf("/")) || "/");
      if (!big && !gone.previous_kept) { // told it would be kept: say it was not, where it stays in sight (review B2)
        this.body.prepend(this.el("p", "files-note is-error files-lasting",
          `${name} was deleted, but its content could not be kept as a previous version: it cannot be put back.`));
      }
    } catch (error) {
      if (error.code === "changed") this.paintState("Not deleted: it changed on the server since; look again");
      else this.paintState(error.message);
    }
  }

  /* §5.3: the open file checked on disk while it shows. Unchanged text follows the disk;
   * text being edited is never replaced: the user is told and picks. */
  schedulePoll(doc) {
    clearTimeout(doc.pollTimer);
    doc.pollTimer = setTimeout(() => this.poll(doc), POLL_MS);
  }

  async poll(doc) {
    if (this.doc !== doc) return;
    const shown = typeof document === "undefined" || document.visibilityState !== "hidden";
    if (shown && !doc.saving && doc.bar.hidden) await this.checkDisk(doc);
    if (this.doc === doc) this.schedulePoll(doc);
  }

  async checkDisk(doc = this.doc) {
    if (doc === null) return;
    const since = doc.revision;
    try {
      const now = await this.api("GET", `/api/machine/stat?${query(doc.path)}`);
      if (this.doc !== doc || doc.saving || sameIdentity(now.identity, doc.identity)) {
        this.paintState();
        return;
      }
      if (doc.dirty || doc.revision !== since) this.showChanged(false);
      else await this.loadDisk({ since });
    } catch (error) {
      if (error.code === "not_found") this.goneOr(doc, error);
    }
  }

  view(shown) {
    const box = this.el("div", "files-view");
    if (shown.kind === "text") {
      if (shown.truncated) {
        box.append(this.el("p", "files-note", `Showing the start of this file (${humanSize(shown.size)}); it is too large to show whole.`));
      }
      const pre = this.el("pre", "files-text");
      const lines = shown.text.split("\n");
      if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
      for (const line of lines) pre.append(this.el("span", "files-line", `${line}\n`));
      box.append(pre);
      return box;
    }
    if (shown.kind === "image") {
      const image = this.el("img", "files-image");
      image.alt = shown.path.slice(shown.path.lastIndexOf("/") + 1);
      image.src = `/api/machine/image?${query(shown.path)}`;
      image.addEventListener("error", () => {
        image.replaceWith(this.el("p", "files-note", "This image cannot be shown here. Download it to see it."));
      });
      box.append(image);
      return box;
    }
    const note = shown.kind === "pdf"
      ? `This PDF is too large to view here (${humanSize(shown.size)}; at most 64 MiB).`
      : `No preview for this file (${humanSize(shown.size)}).`;
    box.append(this.el("p", "files-note", note), this.download(shown.path, "Download"));
    return box;
  }

  paintCrumbs(path, isFile) {
    const parts = [];
    let base = "/";
    let rest = path;
    if (this.home && (path === this.home || path.startsWith(`${this.home}/`))) {
      parts.push(["~", this.home]);
      base = this.home;
      rest = path.slice(this.home.length);
    } else {
      parts.push(["/", "/"]);
    }
    for (const name of rest.split("/").filter(Boolean)) {
      base = join(base, name);
      parts.push([name, base]);
    }
    this.crumbs.replaceChildren();
    parts.forEach(([label, target], index) => {
      if (index > 0) this.crumbs.append(this.el("span", "files-crumb-sep", "›"));
      const last = index === parts.length - 1;
      if (last && isFile) {
        this.crumbs.append(this.el("span", "files-crumb is-current", label));
        return;
      }
      const crumb = this.el("button", last ? "files-crumb is-current" : "files-crumb", label);
      crumb.type = "button";
      crumb.title = target;
      crumb.addEventListener("click", () => this.openFolder(target));
      this.crumbs.append(crumb);
    });
  }

  paintTools(shown, listing = null) {
    this.tools.replaceChildren();
    this.stateLabel = null;
    if (shown) {
      if (shown.kind === "text" && shown.editable) {
        this.workingNote = this.el("span", "files-working", "HIMARI is working");
        this.workingNote.title = "HIMARI may change files here while it works; a save never overwrites its changes unasked";
        this.workingNote.hidden = true;
        this.stateDot = this.el("span", "files-state-dot");
        this.stateLabel = this.el("span", "files-state", "Saved");
        this.saveButton = this.el("button", "primary-button files-save", "Save");
        this.saveButton.type = "button";
        this.saveButton.disabled = true;
        this.saveButton.addEventListener("click", () => this.save());
        this.tools.append(this.workingNote, this.stateDot, this.stateLabel, this.saveButton);
      }
      if (shown.kind === "pdf" && shown.size <= PDF_MAX_BYTES) {
        this.pageLabel = this.pagePicker();
        this.tools.append(this.inkTools(), this.pageLabel);
      }
      if (this.changes && this.changed.has(shown.path)) { // a dot after its name, to its change
        const review = this.el("button", "files-crumb files-changed-link");
        review.type = "button";
        review.append(this.el("span", "files-changed-dot"));
        review.title = "Changed by HIMARI: what it was before and what it is now, to accept or revert";
        review.setAttribute("aria-label", review.title);
        review.addEventListener("click", () => this.openChange(shown.path));
        this.crumbs.children[this.crumbs.children.length - 1].append(review); // the name's: they wrap as one
      }
      this.tools.append(this.fileMenu(shown));
      return;
    }
    const reload = this.el("button", "link-button", "Refresh");
    reload.type = "button";
    reload.addEventListener("click", () => this.refresh());
    this.tools.append(reload, this.newMenu(listing), this.folderMenu(listing));
    if (listing && this.folderActions) this.tools.append(...this.folderActions(listing));
  }

  /* The open file's ⋯: previous versions, download, copy path, delete. */
  fileMenu(shown) {
    const actions = this.el("details", "conversation-actions files-actions files-file-menu");
    const toggle = this.el("summary", null, "⋯");
    toggle.setAttribute("aria-label", "File actions");
    toggle.title = "File actions";
    const list = this.el("div", "conversation-action-list");
    const item = (label, action) => {
      const control = this.el("button", "link-button", label);
      control.type = "button";
      control.addEventListener("click", () => { actions.open = false; action(); });
      return control;
    };
    if (this.onAsk) list.append(item("Ask HIMARI about this file", () => this.askWhole(shown)));
    if (shown.kind === "text" && shown.editable) list.append(item("Previous versions", () => this.showVersions()));
    list.append(this.download(shown.path, "Download"), this.copyPath(shown.path, () => { actions.open = false; }),
                item("Delete", () => this.deleteFile()));
    actions.append(toggle, list);
    return actions;
  }

  /* A folder's ⋯: files deleted from it whose content is kept, to put back (PR #95 review B3). */
  folderMenu(listing) {
    const actions = this.el("details", "conversation-actions files-actions files-folder-menu");
    const toggle = this.el("summary", null, "⋯");
    toggle.setAttribute("aria-label", "Folder actions");
    toggle.title = "Folder actions";
    const list = this.el("div", "conversation-action-list");
    const deleted = this.el("button", "link-button", "Deleted files");
    deleted.type = "button";
    deleted.addEventListener("click", () => { actions.open = false; this.showDeleted(listing ? listing.path : this.folder); });
    list.append(deleted);
    actions.append(toggle, list);
    return actions;
  }

  async showDeleted(folder) {
    let items;
    try {
      items = (await this.api("GET", `/api/machine/versions/deleted?${query(folder)}`)).items;
    } catch (error) {
      this.body.prepend(this.el("p", "files-note is-error", error.message));
      return;
    }
    const dialog = this.el("dialog", "files-dialog");
    const head = this.el("div", "files-dialog-head");
    head.append(this.el("h2", null, "Deleted files in this folder"));
    const close = this.el("button", "icon-button", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    head.append(close);
    const list = this.el("ul", "files-list files-versions");
    if (!items.length) list.append(this.el("li", "files-note", "None: a file deleted from here is kept for 30 days."));
    const note = this.el("p", "files-note is-error");
    for (const item of items) {
      const li = this.el("li", "files-row files-version");
      li.append(this.el("span", "files-name", item.name),
                this.el("span", "files-meta", `deleted ${new Date(item.saved_at).toLocaleString()} · ${humanSize(item.size)}`));
      const restore = this.el("button", "link-button", "Restore");
      restore.type = "button";
      restore.addEventListener("click", async () => {
        restore.disabled = true;
        const error = await this.restoreDeleted(item);
        if (error === null) { if (dialog.close) dialog.close(); dialog.remove(); return; }
        restore.disabled = false;
        note.textContent = error;
      });
      li.append(restore);
      list.append(li);
    }
    dialog.append(head, list, note);
    this.root.append(dialog);
    close.addEventListener("click", () => { if (dialog.close) dialog.close(); dialog.remove(); });
    dialog.addEventListener("cancel", () => dialog.remove());
    if (dialog.showModal) dialog.showModal();
  }

  /* A deleted file back where it was, never over a file now there: in one step (V3-P76 §4.1); where the file
   * system cannot, made new and its kept content saved in it. Two writes: if the save fails after the empty file was made, ``item.made`` keeps that file's identity and
   * Restore again fills that same file, while it is still that empty file (PR #95 re-review C2). Opened
   * afterwards only if nothing else was opened meanwhile; otherwise the user is told (re-review C1). An
   * error's text, or null. */
  async restoreDeleted(item) {
    const ticket = this.ticket;
    let made = null;
    try {
      // V3-P76 §4.1: in one step, where its name is free — the two writes below only where the folder's file
      // system cannot (``unsupported``).
      if (!item.made) {
        const placed = await this.api("POST", `/api/machine/versions/${encodeURIComponent(item.id)}/restore`,
                                      { path: item.path })
          .catch((error) => { if (error.code === "unsupported") return null; throw error; });
        if (placed !== null) {
          if (this.ticket === ticket && !this.dirty) await this.openFile(placed.path);
          else this.notice(`${item.name} is restored.`, placed.path);
          return null;
        }
      }
      const text = (await this.api("GET", `/api/machine/versions/${encodeURIComponent(item.id)}?${query(item.path)}`)).text;
      if (item.made) {
        const now = await this.api("GET", `/api/machine/stat?${query(item.made.path)}`).catch(() => null);
        if (now && sameIdentity(now.identity, item.made.identity)) made = item.made;
      }
      made = made || await this.api("POST", "/api/machine/file", { path: item.path });
      await this.api("PUT", "/api/machine/file", { path: made.path, content: text, expected: made.identity });
      item.made = null;
    } catch (error) {
      if (error.code === "exists" || (error.code === "changed" && !made)) {
        return `${item.name} exists again. Its kept content is among its previous versions: open it, then ⋯ › Previous versions.`;
      }
      if (made) {
        item.made = made;
        return `${item.name} was made, but its content could not be saved in it (${error.message}). Restore again to fill it.`;
      }
      return error.message;
    }
    if (this.ticket === ticket && !this.dirty) await this.openFile(made.path);
    else this.notice(`${item.name} is restored.`, made.path);
    return null;
  }

  /* A note at the top of the pane, with a link to open ``path``; it stays until the next navigation. */
  notice(text, path = null) {
    const note = this.el("p", "files-note files-lasting", text);
    if (path) {
      const open = this.el("button", "link-button", "Open it");
      open.type = "button";
      open.addEventListener("click", () => this.openFile(path));
      note.append(open);
    }
    this.body.prepend(note);
  }

  /* A folder's "New": a file (opened in the editor) or a folder, named in a row at the top. */
  newMenu(listing) {
    const actions = this.el("details", "conversation-actions files-actions files-new-menu");
    const toggle = this.el("summary", "files-new-toggle", "New");
    toggle.title = "New file or folder here";
    const list = this.el("div", "conversation-action-list");
    for (const [label, kind] of [["New file", "file"], ["New folder", "folder"]]) {
      const control = this.el("button", "link-button", label);
      control.type = "button";
      control.addEventListener("click", () => { actions.open = false; this.askName(listing ? listing.path : this.folder, kind); });
      list.append(control);
    }
    actions.append(toggle, list);
    return actions;
  }

  askName(folder, kind) {
    if (!folder) return;
    const form = this.el("form", "files-name-form");
    const input = this.el("input", "files-name-input");
    input.type = "text";
    input.placeholder = kind === "file" ? "File name, e.g. notes.md" : "Folder name";
    input.setAttribute("aria-label", input.placeholder);
    input.spellcheck = false;
    const create = this.el("button", "primary-button", "Create");
    create.type = "submit";
    const cancel = this.el("button", "link-button", "Cancel");
    cancel.type = "button";
    const note = this.el("span", "files-note is-error");
    form.append(input, create, cancel, note);
    cancel.addEventListener("click", () => form.remove());
    form.addEventListener("submit", async (event) => {
      if (event && event.preventDefault) event.preventDefault();
      const name = input.value.trim();
      if (!name || name.includes("/") || name === "." || name === "..") {
        note.textContent = "Give a name without /.";
        return;
      }
      create.disabled = true;
      const path = join(folder, name);
      const ticket = this.ticket; // anything opened meanwhile stays (PR #95 re-review C1)
      try {
        if (kind === "file") {
          const made = await this.api("POST", "/api/machine/file", { path });
          form.remove();
          if (this.ticket === ticket) await this.openFile(made.path);
          else this.notice(`${name} is made.`, made.path);
        } else {
          await this.api("POST", "/api/machine/folder", { path });
          form.remove();
          if (this.ticket === ticket) await this.openFolder(folder);
        }
      } catch (error) {
        create.disabled = false;
        note.textContent = error.code === "exists" ? `${name} exists already.` : error.message;
      }
    });
    this.body.prepend(form);
    if (input.focus) input.focus();
  }

  /* ------------------------------------------------ HIMARI's changes (V3-P76 §5, §6) */

  /* The last list read: its paths get a dot (a folder's, when one is under it). */
  markChanged(list) {
    this.changed = new Set(list ? list.items.map((item) => item.path) : []);
  }

  changedUnder(path, folder) {
    if (!folder) return this.changed.has(path);
    for (const changed of this.changed) if (changed.startsWith(`${path}/`)) return true;
    return false;
  }

  /* Files › Changes [› name]: the way back to the folder last shown. */
  paintPageCrumbs(name = null) {
    this.crumbs.replaceChildren();
    const back = this.el("button", "files-crumb", "Files");
    back.type = "button";
    back.addEventListener("click", () => this.openFolder(this.folder || "~"));
    const list = this.el(name ? "button" : "span", name ? "files-crumb" : "files-crumb is-current", "Changes");
    if (name) {
      list.type = "button";
      list.addEventListener("click", () => this.openChanges());
    }
    this.crumbs.append(back, this.el("span", "files-crumb-sep", "›"), list);
    if (name) this.crumbs.append(this.el("span", "files-crumb-sep", "›"), this.el("span", "files-crumb is-current", name));
  }

  button(label, action, className = "link-button") {
    const control = this.el("button", className, label);
    control.type = "button";
    control.addEventListener("click", action);
    return control;
  }

  /* An accept or revert control: off while HIMARI works (§6). */
  reviewButton(label, action, off = false) {
    const control = this.button(label, action);
    this.reviewControls.push(control);
    control.disabled = off || this.working();
    return control;
  }

  /* Just before an accept or a revert: still the page's conversation, and HIMARI not at work (§6). */
  mayReview(of) {
    if (of !== this.pageOf || of !== this.changes.of()) return false;
    if (!this.working()) return true;
    this.body.prepend(this.el("p", "files-note is-error files-lasting", "HIMARI is working: accepting and reverting wait until it has finished."));
    return false;
  }

  /* An accept or a revert refused: in sight; the list again when it changed meanwhile (PR #98 re-review). */
  async refused(error) {
    if (error.code === "outdated") await this.openChanges(error.message);
    else this.body.prepend(this.el("p", "files-note is-error files-lasting", error.message));
  }

  /* The answer is still for what is shown: no newer request, and the same conversation. */
  current(ticket, of) {
    return ticket === this.ticket && of === this.changes.of();
  }

  /* The list (§5): what can no longer be reverted first. ``note``: what the last action did. */
  async openChanges(note = null) {
    if (!this.changes || !(await this.mayLeave())) return;
    const of = this.changes.of();
    this.closeDoc();
    const ticket = ++this.ticket;
    this.busy("Changes");
    let list;
    try {
      list = await this.changes.list(of);
    } catch (error) {
      if (this.current(ticket, of)) this.failedPage(null, error);
      return;
    }
    if (!this.current(ticket, of)) return;
    this.page = "changes";
    this.pageOf = of;
    this.file = null;
    this.markChanged(list);
    this.changes.seen(list);
    this.body.removeAttribute("aria-busy");
    this.paintPageCrumbs();
    const waiting = list.running || this.working();
    const acceptAll = this.reviewButton("Accept all", () => this.review(of, () => this.changes.accept(of, null), "Accepted."),
                                        waiting || !(list.items.length || list.hidden));
    acceptAll.title = "Keep the files as they are, and clear this list";
    const revertAll = this.reviewButton("Revert all", () => this.revertAll(of, list),
                                        waiting || !list.items.some((item) => item.revertible));
    revertAll.title = "Put each file back as it was before HIMARI changed it";
    this.tools.replaceChildren(acceptAll, revertAll);
    const parts = [];
    if (note) parts.push(this.el("p", "files-note files-lasting", note));
    if (waiting) parts.push(this.el("p", "files-note", "HIMARI is working: accepting and reverting wait until it has finished."));
    if (list.commands_ran && (list.items.length || list.hidden)) {
      parts.push(this.el("p", "files-note is-warn", "HIMARI also ran commands since: they may have changed other files, which are not listed here."));
    }
    const rows = this.el("ul", "files-list files-changes");
    for (const item of list.items) rows.append(this.changeRow(of, item, waiting));
    if (list.items.length) parts.push(rows);
    else if (!list.hidden) parts.push(this.el("p", "files-note", "Nothing to review: HIMARI has changed no files here since you last reviewed."));
    if (list.hidden) {
      parts.push(this.el("p", "files-note", `${list.hidden} ${list.hidden === 1 ? "change is" : "changes are"} in files hidden now; Accept all clears ${list.hidden === 1 ? "it" : "them"}.`));
    }
    this.body.replaceChildren(...parts);
  }

  changeRow(of, item, waiting) {
    const li = this.el("li", `files-row files-change${item.revertible ? "" : " is-warn"}`);
    const open = this.el("button", "files-open");
    open.type = "button";
    open.title = item.path;
    const meta = [CHANGE_KINDS[item.kind] || item.kind];
    if (item.count > 1) meta.push(`${item.count} times`);
    if (item.interleaved) meta.push("also changed by something else");
    if (CHANGE_STATES[item.state]) meta.push(CHANGE_STATES[item.state]);
    open.append(icon(KIND_ICON.file), this.el("span", "files-name", item.name), this.el("span", "files-meta", meta.join(" · ")));
    open.addEventListener("click", () => this.openChange(item.path));
    const revert = this.reviewButton("Revert", () => this.revertOne(of, item.path, item.name), waiting || !item.revertible);
    const accept = this.reviewButton("Accept", () => this.review(of, () => this.changes.accept(of, [item.path]), `${item.name}: accepted.`),
                                     waiting);
    li.append(open, revert, accept);
    return li;
  }

  /* One change (§5): what it was before HIMARI's first change (left) and what it is now (right). */
  async openChange(path) {
    if (!this.changes || !(await this.mayLeave())) return;
    const of = this.changes.of();
    this.closeDoc();
    const ticket = ++this.ticket;
    this.busy(path);
    let diff;
    let module;
    try {
      [diff, module] = await Promise.all([this.changes.diff(of, path), this.loadEditor()]);
    } catch (error) {
      if (this.current(ticket, of)) this.failedPage(path.slice(path.lastIndexOf("/") + 1), error);
      return;
    }
    if (!this.current(ticket, of)) return;
    this.page = { path };
    this.pageOf = of;
    this.file = null;
    this.body.removeAttribute("aria-busy");
    this.paintPageCrumbs(diff.name);
    const revert = this.reviewButton("Revert", () => this.revertOne(of, path, diff.name),
                                     !diff.revertible || diff.changed || diff.interleaved);
    revert.title = "Put it back as it was before HIMARI changed it";
    const accept = this.reviewButton("Accept", () => this.review(of, () => this.changes.accept(of, [path]), `${diff.name}: accepted.`));
    this.tools.replaceChildren(revert, accept);
    if (diff.now.exists) this.tools.append(this.button("Open", () => this.openFile(path)));
    const parts = [];
    if (this.working()) parts.push(this.el("p", "files-note", "HIMARI is working: accepting and reverting wait until it has finished."));
    if (diff.changed || diff.interleaved) {
      const bar = this.el("div", "files-bar");
      bar.append(this.el("p", null, diff.interleaved
        ? "Something other than this conversation's HIMARI also changed it, between HIMARI's changes. Reverting undoes that too."
        : "It was changed by something other than this conversation's HIMARI since (on the right as it is now). Reverting undoes that too."));
      if (diff.revertible) bar.append(this.reviewButton("Revert anyway", () => this.revertAnyway(of, diff)));
      parts.push(bar);
    }
    if (CHANGE_STATES[diff.state]) parts.push(this.el("p", "files-note is-warn", `It cannot be reverted: ${CHANGE_STATES[diff.state]}. Accept clears it; earlier versions are under ⋯ › Previous versions.`));
    const sides = [];
    if (diff.before.state === "absent") sides.push("Before: there was no such file (HIMARI made it).");
    else if (diff.before.text === null && diff.before.state === "present") sides.push("Before: not text, so it is not shown.");
    if (!diff.now.exists) sides.push("Now: there is no such file.");
    else if (diff.now.text === null) sides.push("Now: not text, or over 1 MiB, so it is not shown.");
    for (const side of sides) parts.push(this.el("p", "files-note", side));
    const area = this.el("div", "files-diff files-change-diff");
    parts.push(area);
    this.body.replaceChildren(...parts);
    this.pageDiff = module.createDiff(area, { left: diff.before.text ?? "", right: diff.now.text ?? "", name: diff.name });
  }

  /* An accept or a revert, then the list again, told what happened; a refusal stays in sight. */
  async review(of, act, done) {
    if (!this.mayReview(of)) return false;
    const ticket = this.ticket;
    try {
      await act();
    } catch (error) {
      if (this.current(ticket, of)) await this.refused(error);
      return false;
    }
    if (this.current(ticket, of)) await this.openChanges(done);
    return true;
  }

  /* §6: only as HIMARI left it; changed since, its diff is shown with the choice to revert anyway. */
  async revertOne(of, path, name) {
    if (!this.mayReview(of)) return;
    const ticket = this.ticket;
    try {
      await this.changes.revert(of, path, null);
    } catch (error) {
      if (!this.current(ticket, of)) return;
      if (error.code === "changed") await this.openChange(path);
      else await this.refused(error);
      return;
    }
    if (this.current(ticket, of)) await this.openChanges(`${name} is back as it was before HIMARI changed it.`);
  }

  /* With the state shown (``diff.now``): reverted only if the file is still so. */
  async revertAnyway(of, diff) {
    const ticket = this.ticket;
    const sure = await this.confirm(`Revert ${diff.name} anyway?`, [
      "It was changed by something other than this conversation's HIMARI; reverting undoes that too.",
      "What it holds now is kept as a previous version."], "Revert anyway");
    if (!sure || !this.current(ticket, of) || !this.mayReview(of)) return;
    try {
      await this.changes.revert(of, diff.path, diff.now);
    } catch (error) {
      if (!this.current(ticket, of)) return;
      if (error.code === "changed") await this.openChange(diff.path); // changed again since it was shown
      else await this.refused(error);
      return;
    }
    if (this.current(ticket, of)) await this.openChanges(`${diff.name} is back as it was before HIMARI changed it.`);
  }

  async revertAll(of, list) {
    const ticket = this.ticket;
    const count = list.items.filter((item) => item.revertible).length;
    const sure = await this.confirm(`Revert ${count} ${count === 1 ? "file" : "files"}?`, [
      "Each goes back to what it was before HIMARI changed it. What it holds now is kept as a previous version.",
      "A file changed by something else since, or one that cannot be reverted, is left as it is."], "Revert all");
    if (!sure || !this.current(ticket, of) || !this.mayReview(of)) return;
    let done;
    try {
      done = await this.changes.revertAll(of);
    } catch (error) {
      if (this.current(ticket, of)) await this.refused(error);
      return;
    }
    if (!this.current(ticket, of)) return;
    await this.openChanges(revertedAll(done));
  }

  failedPage(name, error) {
    this.body.removeAttribute("aria-busy");
    this.paintPageCrumbs(name);
    this.tools.replaceChildren();
    this.body.replaceChildren(this.el("p", "files-note is-error", error.message || "This could not be opened."));
  }

  busy(path) {
    this.body.setAttribute("aria-busy", "true");
    this.crumbs.title = path;
  }

  failed(path, error) {
    this.body.removeAttribute("aria-busy");
    this.paintCrumbs(path.startsWith("~") && this.home ? this.home + path.slice(1) : path, false);
    this.body.replaceChildren(this.el("p", "files-note is-error", error.message || "This could not be opened."));
  }

  /* After the agent's turn: the folder again; a file in the editor is checked on disk (never reloaded
   * over unsaved text); another file is read again; Changes, or a change, read again. */
  refresh() {
    if (this.page === "changes") return this.openChanges();
    if (this.page) return this.openChange(this.page.path);
    if (this.doc) return this.checkDisk();
    if (this.pdf) return this.checkPdf();
    if (this.file) return this.openFile(this.file);
    if (this.folder) return this.openFolder(this.folder);
    return null;
  }
}
