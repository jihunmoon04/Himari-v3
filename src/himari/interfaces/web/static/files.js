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
 */

const KIND_ICON = { directory: "#side-folder", file: "#side-note" };
const EDIT_MAX_BYTES = 1024 * 1024; // the server's limit for the editor (§7.7, Q3)
const POLL_MS = 5000; // how often an open file is checked on disk (§5.3)

function defaultConfirm(title, lines) { // the browser's own: no action label
  return Promise.resolve(window.confirm([title, ...lines].join("\n\n")));
}

function sameIdentity(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
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

export class FilesPane {
  /* api(method, path) → JSON or throws {message, status, code}; el(tag, class, text). */
  /* folderActions(listing): the shown folder's own controls (V3-P75 §3.3, e.g.
   * "Open conversation"); folderRowActions(path): [label, action] pairs for a
   * folder row's ⋯ menu. Both from the place the pane is in. */
  /* loadEditor(): the CodeMirror module (createEditor, createDiff); confirm(title, lines, action) → Promise<bool>;
   * working(): HIMARI is at work in this place now (told beside the editor, §7.3). */
  constructor({ api, el, onOpenFolder = null, onOpenFile = null, actions = [], folderActions = null,
                folderRowActions = null, loadEditor = () => import("/assets/codemirror/codemirror.js"),
                confirm = defaultConfirm, working = () => false }) {
    this.api = api;
    this.el = el;
    this.loadEditor = loadEditor;
    this.confirm = confirm;
    this.working = working;
    this.doc = null; // the file in the editor: {path, name, identity, saved, editor, dirty, saving, pollTimer, bar}
    this.onOpenFolder = onOpenFolder;
    this.onOpenFile = onOpenFile;
    this.folderActions = folderActions;
    this.folderRowActions = folderRowActions;
    this.root = el("div", "files-pane");
    this.crumbs = el("nav", "files-crumbs");
    this.crumbs.setAttribute("aria-label", "Path");
    this.tools = el("div", "files-tools");
    const head = el("div", "files-head");
    head.append(this.crumbs, this.tools, ...actions); // the place's own controls (the panel's expand, close)
    this.body = el("div", "files-body");
    this.root.append(head, this.body);
    this.home = null; // the user's home as the server resolves ~
    this.folder = null; // the folder shown, or the file's folder
    this.file = null; // the file shown
    this.ticket = 0; // the latest request: an older answer is not applied
  }

  /* The folder the pane shows (reloaded), or the file's. */
  get place() {
    return this.file || this.folder;
  }

  /* Text typed in the editor and not saved. */
  get dirty() {
    return this.doc !== null && this.doc.dirty;
  }

  /* Before anything replaces the editor: unsaved text is dropped only if the user says so. */
  async mayLeave() {
    if (!this.dirty) return true;
    return this.confirm(`Discard your changes to ${this.doc.name}?`,
                        ["They are not saved. The file on the server stays as it was."], "Discard");
  }

  closeDoc() {
    if (this.doc === null) return;
    clearTimeout(this.doc.pollTimer);
    if (this.doc.editor) this.doc.editor.destroy();
    this.doc = null;
  }

  /* ~ as the server resolves it, so paths under it are shown from ~. */
  async learnHome() {
    if (this.home !== null) return;
    try {
      const found = await this.api("GET", "/api/machine/stat?path=~");
      if (this.home === null) this.home = found.path;
    } catch (ignored) { /* shown from / */ }
  }

  async openFolder(path, { asked = false } = {}) {
    if (!asked && !(await this.mayLeave())) return;
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
    if (!(await this.mayLeave())) return null;
    const ticket = ++this.ticket;
    this.busy(path);
    try {
      const target = await this.api("GET", `/api/machine/stat?${query(path)}`);
      if (ticket !== this.ticket) return null;
      return target.kind === "directory" ? this.openFolder(target.path, { asked: true })
        : this.openFile(target.path, { asked: true });
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

  async openFile(path, { asked = false } = {}) {
    if (!asked && !(await this.mayLeave())) return;
    const ticket = ++this.ticket;
    this.closeDoc();
    this.busy(path);
    try {
      await this.learnHome();
      const shown = await this.api("GET", `/api/machine/file?${query(path)}`);
      if (ticket !== this.ticket) return;
      this.file = shown.path;
      this.folder = shown.path.slice(0, shown.path.lastIndexOf("/")) || "/";
      this.body.removeAttribute("aria-busy");
      this.paintCrumbs(shown.path, true);
      this.paintTools(shown);
      if (shown.kind === "text" && shown.editable) await this.edit(shown, ticket);
      else this.body.replaceChildren(this.view(shown));
      if (this.onOpenFile) this.onOpenFile(shown);
    } catch (error) {
      if (ticket === this.ticket) this.failed(path, error);
    }
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
    if (ticket !== this.ticket) return;
    const bar = this.el("div", "files-bar");
    bar.hidden = true;
    const area = this.el("div", "files-editor");
    this.body.replaceChildren(bar, area);
    const doc = { path: shown.path, name, identity: shown.identity, saved: shown.text, editor: null, dirty: false,
                  saving: false, pollTimer: null, bar, module };
    this.doc = doc;
    doc.editor = module.createEditor(area, {
      text: shown.text, name,
      onChange: () => { if (this.doc === doc) this.setDirty(doc.editor.text() !== doc.saved); },
      onSave: () => { if (this.doc === doc) this.save(); },
    });
    this.paintState();
    this.schedulePoll(doc);
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
    const content = doc.editor.text();
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
      doc.saved = content;
      doc.saving = false;
      doc.dirty = doc.editor.text() !== content; // typed on while it was saving
      this.hideBar();
      this.paintState(answer.previous_kept ? null : "Saved, but its previous version could not be kept");
    } catch (error) {
      if (this.doc !== doc) return;
      doc.saving = false;
      if (error.code === "changed") this.showChanged(true);
      else this.paintState(error.message || "Not saved");
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

  async loadDisk() {
    const doc = this.doc;
    if (doc === null) return;
    try {
      const shown = await this.disk();
      if (this.doc !== doc) return;
      if (shown.kind !== "text" || !shown.editable) {
        this.paintState("The server's version can no longer be edited here");
        return;
      }
      doc.saved = shown.text;
      doc.identity = shown.identity;
      doc.editor.setText(shown.text);
      doc.dirty = false;
      this.hideBar();
      this.paintState();
    } catch (error) {
      if (this.doc === doc) this.paintState(error.message);
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
      if (this.doc === doc) this.paintState(error.message);
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
      if (this.doc === doc) this.paintState(error.message);
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
                this.el("span", "files-meta", `${item.reason === "delete" ? "before a delete" : "before a save"} · ${humanSize(item.size)}`));
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
    this.setDirty(text !== this.doc.saved);
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
    const big = now.size > EDIT_MAX_BYTES;
    const lines = big ? ["This file is over 1 MiB: no previous version of it is kept, so it cannot be put back."]
      : ["Its content is kept as a previous version on the server for 30 days."];
    if (doc && doc.dirty) lines.push("Your unsaved changes are dropped too.");
    if (!(await this.confirm(`Delete ${name}?`, lines, "Delete"))) return;
    if (big && !(await this.confirm(`Delete ${name} for good?`, ["It cannot be put back."], "Delete for good"))) return;
    try {
      await this.api("POST", "/api/machine/file/delete", { path, expected: now.identity });
      if (this.doc === doc) this.closeDoc();
      await this.openFolder(path.slice(0, path.lastIndexOf("/")) || "/", { asked: true });
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
    try {
      const now = await this.api("GET", `/api/machine/stat?${query(doc.path)}`);
      if (this.doc !== doc || doc.saving || sameIdentity(now.identity, doc.identity)) {
        this.paintState();
        return;
      }
      if (doc.dirty) this.showChanged(false);
      else await this.loadDisk();
    } catch (error) {
      if (this.doc === doc && error.code === "not_found") this.paintState("This file is no longer on the server");
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
      this.tools.append(this.fileMenu(shown));
      return;
    }
    const reload = this.el("button", "link-button", "Refresh");
    reload.type = "button";
    reload.addEventListener("click", () => this.refresh());
    this.tools.append(reload, this.newMenu(listing));
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
    if (shown.kind === "text" && shown.editable) list.append(item("Previous versions", () => this.showVersions()));
    list.append(this.download(shown.path, "Download"), this.copyPath(shown.path, () => { actions.open = false; }),
                item("Delete", () => this.deleteFile()));
    actions.append(toggle, list);
    return actions;
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
      try {
        if (kind === "file") {
          const made = await this.api("POST", "/api/machine/file", { path });
          form.remove();
          await this.openFile(made.path, { asked: true });
        } else {
          await this.api("POST", "/api/machine/folder", { path });
          form.remove();
          await this.openFolder(folder, { asked: true });
        }
      } catch (error) {
        create.disabled = false;
        note.textContent = error.code === "exists" ? `${name} exists already.` : error.message;
      }
    });
    this.body.prepend(form);
    if (input.focus) input.focus();
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
   * over unsaved text); another file is read again. */
  refresh() {
    if (this.doc) return this.checkDisk();
    if (this.file) return this.openFile(this.file, { asked: true });
    if (this.folder) return this.openFolder(this.folder, { asked: true });
    return null;
  }
}
