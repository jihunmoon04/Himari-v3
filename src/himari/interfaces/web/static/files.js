/* The file explorer's parts (V3-P75 §3): a folder list and a file view in
 * one pane, placed by the caller — the main area (Files in the sidebar) or
 * the panel beside a conversation. It only shows (V3-P99): nothing here
 * writes, makes or deletes a file.
 *
 * Every name, path and file text goes in with textContent. A file's bytes
 * are never shown as they are: text as text, an image as the copy the
 * server's image sandbox made, anything else as a download (an attachment).
 *
 * Ask HIMARI (V3-P76 §3): a selection, or the whole file, handed to the
 * place as a reference — the file's path and identity, and for a selection
 * its positions and the SHA-256 of its text, never the text.
 */

const KIND_ICON = { directory: "#side-folder", file: "#side-note" };

/* A file's text as its positions count it (V3-P76 §12.2): \r\n, \r and \n each one "\n". */
function shownText(text) {
  return text.replace(/\r\n?/g, "\n");
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

/* [line, column] of a position in ``text``, both from 1. */
function lineAt(text, position) {
  const before = text.slice(0, position);
  const line = before.split("\n").length;
  return [line, position - (before.lastIndexOf("\n") + 1) + 1];
}

/* What of ``text`` (shown in ``pre``, one span a line) the user selected: {from, to, text}, or null. */
export function selectionIn(pre, text, selection) {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!pre.contains(range.startContainer) || !pre.contains(range.endContainer)) return null;
  const upTo = document.createRange();
  upTo.selectNodeContents(pre);
  upTo.setEnd(range.startContainer, range.startOffset);
  const from = Math.min(upTo.toString().length, text.length);
  const to = Math.min(from + range.toString().length, text.length); // never the break shown after a last line without one
  return from < to ? { from, to, text: text.slice(from, to) } : null;
}

/* The DOM place of a position in the text ``pre`` shows. */
function placeIn(pre, position) {
  let left = position;
  for (const line of pre.children) {
    const node = line.firstChild;
    const length = node ? node.length : 0;
    if (node && left <= length) return [node, left];
    left -= length;
  }
  const last = pre.lastElementChild && pre.lastElementChild.firstChild;
  return last ? [last, last.length] : [pre, 0];
}

export class FilesPane {
  /* api(method, path) → JSON or throws {message, status, code}; el(tag, class, text). */
  /* folderActions(listing): the shown folder's own controls (V3-P75 §3.3, e.g.
   * "Open conversation"); folderRowActions(path): [label, action] pairs for a
   * folder row's ⋯ menu. Both from the place the pane is in. */
  /* onAsk(reference): Ask HIMARI (V3-P76 §3.1) — {path, name, identity, whole} and, for a selection,
   * {start, end, sha256, lines: [fromLine, fromColumn, toLine, toColumn]}; none: not offered here. */
  constructor({ api, el, onOpenFolder = null, onOpenFile = null, actions = [], folderActions = null,
                folderRowActions = null, onAsk = null }) {
    this.api = api;
    this.el = el;
    this.onAsk = onAsk;
    this.onOpenFolder = onOpenFolder;
    this.onOpenFile = onOpenFile;
    this.folderActions = folderActions;
    this.folderRowActions = folderRowActions;
    this.root = el("div", "files-pane");
    this.locations = [];
    this.locationIndex = -1;
    this.locationFailed = false;
    this.backPending = false;
    this.folderScrolls = new Map();
    this.backButton = this.button("", () => this.back());
    this.backButton.title = "Back";
    this.backButton.className = "files-nav-button files-back";
    this.backButton.setAttribute("aria-label", "Go back in files");
    this.backButton.prepend(icon("#side-back"));
    this.upButton = this.button("", () => this.up());
    this.upButton.title = "Up";
    this.upButton.className = "files-nav-button files-up";
    this.upButton.setAttribute("aria-label", "Go to parent folder");
    this.upButton.prepend(icon("#side-up"));
    const navigation = el("div", "files-navigation");
    navigation.append(this.backButton, this.upButton);
    this.syncNavigation();
    this.crumbs = el("nav", "files-crumbs");
    this.crumbs.setAttribute("aria-label", "Path");
    this.tools = el("div", "files-tools");
    const head = el("div", "files-head");
    this.head = head; // compact local controls; the shell fits menus to this pane's available height
    const end = el("div", "files-head-end"); // one group: it goes under the path together when the head is tight
    end.append(this.tools, ...actions); // the place's own controls (the panel's expand, close)
    head.append(navigation, this.crumbs, end);
    this.body = el("div", "files-body");
    this.root.append(head, this.body);
    this.home = null; // the user's home as the server resolves ~
    this.folder = null; // the folder shown, or the file's folder
    this.file = null; // the file shown
    this.shown = null; // that file's text, when it is shown whole: {path, name, identity, text, pre}
    this.ticket = 0; // the latest request: an older answer is not applied
  }

  /* The folder the pane shows (reloaded), or the file. */
  get place() {
    return this.file || this.folder;
  }

  // A successful read commits a move. Failed and stale reads never
  // consume the previous location; refreshes do not add duplicate entries.
  rememberLocation(path, kind, index = null) {
    const current = this.locations[this.locationIndex];
    if (index !== null) this.locationIndex = index;
    else if (!current || current.path !== path || current.kind !== kind) {
      this.locations.splice(this.locationIndex + 1);
      this.locations.push({ path, kind });
      this.locationIndex = this.locations.length - 1;
    }
    this.locationFailed = false;
    if (kind === "folder") this.body.scrollTop = this.folderScrolls.get(path) || 0;
    this.syncNavigation();
  }

  saveLocationScroll() {
    const here = this.locations[this.locationIndex];
    if (here && here.kind === "folder" && !this.locationFailed) this.folderScrolls.set(here.path, this.body.scrollTop);
  }

  syncNavigation() {
    this.backButton.disabled = this.backPending || this.locationIndex < (this.locationFailed ? 0 : 1);
    const here = this.locations[this.locationIndex];
    this.upButton.disabled = !here || (here.kind === "folder" && here.path === "/");
  }

  async back() {
    if (this.backButton.disabled) return;
    const index = this.locationFailed ? this.locationIndex : this.locationIndex - 1;
    const target = this.locations[index];
    this.backPending = true;
    this.syncNavigation();
    try {
      return await (target.kind === "file" ? this.openFile(target.path, index) : this.openFolder(target.path, { historyIndex: index }));
    } finally {
      this.backPending = false;
      this.syncNavigation();
    }
  }

  up() {
    const here = this.locations[this.locationIndex];
    if (!here || this.upButton.disabled) return;
    const parent = here.path.slice(0, here.path.lastIndexOf("/")) || "/";
    return this.openFolder(parent);
  }

  /* Another conversation is shown (PR #99 review C2): no answer still on its way is applied. */
  reset() {
    this.ticket += 1;
    this.locations = [];
    this.locationIndex = -1;
    this.locationFailed = false;
    this.folderScrolls.clear();
    this.shown = null;
    this.syncNavigation();
  }

  /* ~ as the server resolves it, so paths under it are shown from ~. */
  async learnHome() {
    if (this.home !== null) return;
    try {
      const found = await this.api("GET", "/api/machine/stat?path=~");
      if (this.home === null) this.home = found.path;
    } catch (ignored) { /* shown from / */ }
  }

  async openFolder(path, { keepScroll = false, historyIndex = null } = {}) {
    this.saveLocationScroll();
    const scrolled = keepScroll && path === this.folder ? this.body.scrollTop : 0;
    const ticket = ++this.ticket;
    this.file = null;
    this.shown = null;
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
      this.rememberLocation(listing.path, "folder", historyIndex);
      if (historyIndex === null) this.body.scrollTop = scrolled;
      if (this.onOpenFolder) this.onOpenFolder(listing);
      return true;
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
    button.append(icon(KIND_ICON[kind]), this.el("span", "files-name", entry.name));
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

  /* A ⋯ menu: the given controls. */
  actionMenu(label, className, controls) {
    const actions = this.el("details", `conversation-actions files-actions${className ? ` ${className}` : ""}`);
    const toggle = this.el("summary");
    toggle.append(icon("#side-more"));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    const list = this.el("div", "conversation-action-list");
    list.append(...controls(() => { actions.open = false; }));
    actions.append(toggle, list);
    actions.addEventListener("toggle", () => {
      if (!actions.open) return;
      for (const other of this.body.querySelectorAll("details[open]")) if (other !== actions) other.open = false;
    });
    return actions;
  }

  /* A row's ⋯: the given actions, then (a file only) Download, and Copy path. */
  menu(path, name, given, isFile) {
    return this.actionMenu(`Actions for ${name}`, "", (close) => [
      ...given.map(([label, action]) => this.button(label, () => { close(); action(); })),
      ...(isFile ? [this.download(path, "Download")] : []),
      this.copyPath(path, close),
    ]);
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

  async openFile(path, historyIndex = null) {
    this.saveLocationScroll();
    const ticket = ++this.ticket;
    this.busy(path);
    try {
      await this.learnHome();
      const shown = await this.api("GET", `/api/machine/file?${query(path)}`);
      if (ticket !== this.ticket) return;
      this.file = shown.path;
      this.folder = shown.path.slice(0, shown.path.lastIndexOf("/")) || "/";
      this.shown = null;
      this.body.removeAttribute("aria-busy");
      this.paintCrumbs(shown.path, true);
      this.body.replaceChildren(this.view(shown));
      this.paintTools(shown);
      this.rememberLocation(shown.path, "file", historyIndex);
      if (this.onOpenFile) this.onOpenFile(shown);
      return true;
    } catch (error) {
      if (ticket === this.ticket) this.failed(path, error);
    }
  }

  /* V3-P76 §3.1: the selection, or with none the whole file, as a reference. The server checks it
   * against the file. A selection is offered only in a file shown whole. */
  async ask() {
    const shown = this.shown;
    if (shown === null || !this.onAsk) return;
    const picked = typeof window === "undefined" ? null : selectionIn(shown.pre, shown.text, window.getSelection());
    const reference = { path: shown.path, name: shown.name, identity: shown.identity, whole: picked === null };
    if (picked === null) {
      this.onAsk(reference);
      return;
    }
    let sha256;
    try {
      sha256 = await sha256Hex(picked.text);
    } catch (ignored) {
      this.note("Asking about a selection needs the page over HTTPS.");
      return;
    }
    if (this.shown !== shown) return; // another file meanwhile
    this.onAsk({ ...reference, start: picked.from, end: picked.to, sha256,
                 lines: [...lineAt(shown.text, picked.from), ...lineAt(shown.text, picked.to)] });
  }

  /* The whole file from its ⋯ menu: any file, shown or not. */
  askWhole(shown) {
    this.onAsk({ path: shown.path, name: shown.path.slice(shown.path.lastIndexOf("/") + 1),
                 identity: shown.identity, whole: true });
  }

  /* A reference's place: its file, with ``start``–``end`` selected when given (§3.1). */
  async reveal(path, start = null, end = null) {
    if (this.file !== path) await this.openFile(path);
    const shown = this.shown;
    if (start === null || shown === null || shown.path !== path || typeof window === "undefined") return;
    const range = document.createRange();
    range.setStart(...placeIn(shown.pre, Math.min(start, shown.text.length)));
    range.setEnd(...placeIn(shown.pre, Math.min(end === null ? start : end, shown.text.length)));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    const line = range.startContainer.parentElement;
    if (line && line.scrollIntoView) line.scrollIntoView({ block: "center" });
  }

  view(shown) {
    const box = this.el("div", "files-view");
    if (shown.kind === "text") {
      if (shown.truncated) {
        box.append(this.el("p", "files-note", `Showing the start of this file (${humanSize(shown.size)}); it is too large to show whole.`));
      }
      const text = shownText(shown.text);
      const pre = this.el("pre", "files-text");
      const lines = text.split("\n");
      if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
      for (const line of lines) pre.append(this.el("span", "files-line", `${line}\n`));
      box.append(pre);
      if (!shown.truncated) {
        this.shown = { path: shown.path, name: shown.path.slice(shown.path.lastIndexOf("/") + 1),
                       identity: shown.identity, text, pre };
      }
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
    box.append(this.el("p", "files-note", `No preview for this file (${humanSize(shown.size)}).`),
               this.download(shown.path, "Download"));
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
    if (shown) {
      if (this.onAsk && this.shown !== null) { // a file shown whole: what is selected in it, or all of it
        const ask = this.button("Ask HIMARI", () => this.ask(), "link-button files-ask");
        ask.title = "Ask HIMARI about what you selected in this file, or about the whole file";
        this.tools.append(ask);
      }
      this.tools.append(this.fileMenu(shown));
      return;
    }
    const reload = this.el("button", "link-button files-icon-button");
    reload.append(icon("#side-refresh"));
    reload.title = "Refresh";
    reload.setAttribute("aria-label", "Refresh");
    reload.type = "button";
    reload.addEventListener("click", () => this.refresh());
    this.tools.append(reload);
    if (listing && this.folderActions) this.tools.append(...this.folderActions(listing));
  }

  /* The open file's ⋯: ask about it, download, copy path. */
  fileMenu(shown) {
    return this.actionMenu("File actions", "files-file-menu", (close) => [
      ...(this.onAsk ? [this.button("Ask HIMARI about this file", () => { close(); this.askWhole(shown); })] : []),
      this.download(shown.path, "Download"),
      this.copyPath(shown.path, close),
    ]);
  }

  /* A line above what is shown, until the next thing is opened. */
  note(text) {
    this.body.prepend(this.el("p", "files-note is-error files-lasting", text));
  }

  button(label, action, className = "link-button") {
    const control = this.el("button", className, label);
    control.type = "button";
    control.addEventListener("click", action);
    return control;
  }

  busy(path) {
    this.body.setAttribute("aria-busy", "true");
    this.crumbs.title = path;
  }

  failed(path, error) {
    this.locationFailed = true;
    this.shown = null;
    this.syncNavigation();
    this.body.removeAttribute("aria-busy");
    this.paintCrumbs(path.startsWith("~") && this.home ? this.home + path.slice(1) : path, false);
    this.body.replaceChildren(this.el("p", "files-note is-error", error.message || "This could not be opened."));
  }

  /* After the agent's turn: the folder, or the file, read again. */
  refresh() {
    if (this.file) return this.openFile(this.file);
    if (this.folder) return this.openFolder(this.folder, { keepScroll: true });
    return null;
  }
}
