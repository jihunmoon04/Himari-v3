/* The file explorer's parts (V3-P75 §3): a folder list and a file view in
 * one pane, placed by the caller — the main area (Files in the sidebar) or
 * the panel beside a conversation. Part 1 reads only.
 *
 * Every name, path and file text goes in with textContent. A file's bytes
 * are never shown as they are: text as text, an image as the copy the
 * server's image sandbox made, anything else as a download (an attachment).
 */

const KIND_ICON = { directory: "#side-folder", file: "#side-note" };

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
  constructor({ api, el, onOpenFolder = null, onOpenFile = null, actions = [] }) {
    this.api = api;
    this.el = el;
    this.onOpenFolder = onOpenFolder;
    this.onOpenFile = onOpenFile;
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

  /* ~ as the server resolves it, so paths under it are shown from ~. */
  async learnHome() {
    if (this.home !== null) return;
    try {
      const found = await this.api("GET", "/api/machine/stat?path=~");
      if (this.home === null) this.home = found.path;
    } catch (ignored) { /* shown from / */ }
  }

  async openFolder(path) {
    const ticket = ++this.ticket;
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
      this.paintTools(null);
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
    li.append(entry.kind === "file" ? this.menu(path, entry.name) : this.el("span", "files-actions-space"));
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

  menu(path, name) {
    const actions = this.el("details", "conversation-actions files-actions");
    const toggle = this.el("summary", null, "⋯");
    toggle.setAttribute("aria-label", `Actions for ${name}`);
    toggle.title = `Actions for ${name}`;
    const list = this.el("div", "conversation-action-list");
    list.append(this.download(path, "Download"), this.copyPath(path, () => { actions.open = false; }));
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
    const ticket = ++this.ticket;
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
      this.body.replaceChildren(this.view(shown));
      if (this.onOpenFile) this.onOpenFile(shown);
    } catch (error) {
      if (ticket === this.ticket) this.failed(path, error);
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

  paintTools(shown) {
    this.tools.replaceChildren();
    if (shown) {
      this.tools.append(this.copyPath(shown.path, null), this.download(shown.path, "Download"));
      return;
    }
    const reload = this.el("button", "link-button", "Refresh");
    reload.type = "button";
    reload.addEventListener("click", () => this.refresh());
    this.tools.append(reload);
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

  /* After the agent's turn: the folder again, or the file again. */
  refresh() {
    if (this.file) return this.openFile(this.file);
    if (this.folder) return this.openFolder(this.folder);
    return null;
  }
}
