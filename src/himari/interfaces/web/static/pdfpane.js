/* The PDF view (V3-P77 §3): a file pane's PDF, its pages one under another,
 * drawn by PDF.js (static/pdfjs, loaded on the first PDF opened).
 *
 * PDF.js runs on this page's origin, so a PDF is untrusted input to it (§3.4):
 * no scripts, no XFA, no WebAssembly (the page's CSP has no
 * 'wasm-unsafe-eval'; the JavaScript image decoders stand in) and no
 * annotation layer — annotations are drawn into the page's canvas as
 * pictures, never made into links or form fields. PDFJS_OPTIONS holds it all
 * in one place, checked by scripts/pdf_smoke.mjs.
 *
 * Only the pages in view and one screen around them have a canvas; a canvas
 * stays under MAX_CANVAS_PIXELS (iOS Safari's limit) and MAX_CANVAS_SIDE,
 * giving up sharpness first; a page that would be drawn coarser than
 * MIN_OUTPUT_SCALE is not drawn but told. Each page fits the pane's width, a turned one too, so nothing needs
 * scrolling sideways until zoomed; Ctrl+wheel and two fingers zoom the pages,
 * never the whole screen.
 *
 * Ink (§4): over each drawn page, a canvas of its strokes (pdfink.js). A pen
 * draws whenever a tool is picked; a finger never does — it scrolls and
 * zooms, so a palm resting while writing does nothing; a mouse draws on a
 * desktop. Apple Pencil's touches are kept from scrolling (§4.1 A: the box
 * pans, and a stylus touch's default is prevented). A stroke cut short by the
 * system (pointercancel) keeps what was drawn. HIMARI's saved ink is drawn by
 * this layer only: the page's canvas is drawn without it (§4.4 B), so an
 * erased stroke goes at once. A save is an incremental update made here
 * (pdfdoc.js) and sent by the pane. An encrypted PDF, or one whose structure
 * pdfdoc.js cannot read, is viewed and never written on.
 */

import { HIGHLIGHT_ALPHA, Ink, MAX_POINTS, hits, newStroke, outline, specOf, strokeFromSaved } from "./pdfink.js";
import { Ref, appendIncrement, checkIncrement, openPdf } from "./pdfdoc.js";

export const MAX_PDF_BYTES = 64 * 1024 * 1024; // §3.3, Q2: viewed here up to this; larger ones are downloaded
// Every page has a box (its canvas only near the view): a few bytes can hold tens of thousands of pages,
// so their count is bounded too (PR #102 review).
export const MAX_PDF_PAGES = 2000;
export const MAX_CANVAS_PIXELS = 16_000_000;
export const MAX_CANVAS_SIDE = 16_384; // px on either side: under every browser's, iOS Safari's included
// Canvas pixels per CSS pixel below which a page is not drawn (a sliver of a page, kilometres long, can be a
// few bytes): it would be a blur, and is told instead (PR #102 review).
export const MIN_OUTPUT_SCALE = 0.25;
export const ZOOM_MIN = 0.5; // of the fitted width
export const ZOOM_MAX = 5;
const PAGE_GAP = 12; // px between pages, and around them
const AROUND = "100% 0px"; // pages drawn beyond the view: one screen up and down
const REDRAW_MS = 150; // after a zoom, before the pages are drawn again at the new scale
const ERASER_PX = 8; // the eraser's reach on screen, CSS px
const HIDDEN_PREFIX = "pdfjs_internal_editor_himari_"; // PDF.js's own prefix for edited annotations (§14.3)
export const SIGNED_NOTICE = "This PDF is signed. Notes are added after the signature; some apps show that as a change.";

export const PDFJS_OPTIONS = Object.freeze({
  enableXfa: false,
  useWasm: false, // the JavaScript fallbacks: no 'wasm-unsafe-eval' (§3.4)
  wasmUrl: "/assets/pdfjs/wasm/", // where those fallbacks are imported from
  cMapUrl: "/assets/pdfjs/cmaps/", // CJK text in fonts the PDF does not carry
  cMapPacked: true,
  isEvalSupported: false, // gone from PDF.js 6, which never evaluates code; kept should an older one come back
  stopAtErrors: false,
});

export async function loadPdfjs() {
  const pdfjs = await import("/assets/pdfjs/pdf.min.mjs");
  pdfjs.GlobalWorkerOptions.workerSrc = "/assets/pdfjs/pdf.worker.min.mjs";
  return pdfjs;
}

/* The canvas's pixels per CSS pixel: the screen's, unless the canvas would pass MAX_CANVAS_PIXELS or
 * MAX_CANVAS_SIDE — never more than those allow, however small that is (below MIN_OUTPUT_SCALE the page
 * is not drawn). */
export function outputScale(width, height, ratio) {
  const byArea = Math.sqrt(MAX_CANVAS_PIXELS / Math.max(1, width * height));
  const bySide = MAX_CANVAS_SIDE / Math.max(1, width, height);
  return Math.min(ratio || 1, byArea, bySide);
}

/* The page that holds the view's middle: ``tops`` are the pages' tops, in order. */
export function pageAt(tops, middle) {
  let low = 0;
  let high = tops.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (tops[mid] <= middle) low = mid;
    else high = mid - 1;
  }
  return low;
}

export class PdfError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code; // "password" | "unreadable" | "too_many"; saving: "unsaved" | "too_large"
  }
}

export class PdfView {
  /* el(tag, class, text) as the pane's; onPage(page, pages) when the page in view changes; onInk() when the
   * ink changed (drawn, erased, undone, saved); onNotice(text) for a word to the user. */
  constructor({ pdfjs, el, bytes, onPage = () => {}, onInk = () => {}, onNotice = () => {} }) {
    this.pdfjs = pdfjs;
    this.el = el;
    this.bytes = bytes;
    this.onPage = onPage;
    this.onInk = onInk;
    this.onNotice = onNotice;
    this.ink = new Ink();
    this.file = null; // pdfdoc.js's reading of the bytes, to write on
    this.writable = false;
    this.readOnly = null; // why it is not written on, when it is not
    this.signed = false;
    this.tool = null; // {tool: "pen" | "highlighter" | "eraser", color, size}; null: scrolling only
    this.active = null; // the stroke or erasing under way
    this.saving = false; // no drawing while a save is made
    this.element = el("div", "pdf-view");
    this.element.tabIndex = 0;
    this.pagesBox = el("div", "pdf-pages");
    this.element.append(this.pagesBox);
    this.doc = null;
    this.pages = []; // {box, size: {width, height} at scale 1 or null, canvas, task, scale}
    this.zoom = 1; // times the fitted width, the same for every page
    this.current = 0;
    this.near = new Set(); // the pages to draw
    this.destroyed = false;
    this.redrawTimer = null;
    this.pinch = null;
  }

  async open() {
    // PDF.js takes its bytes over (to its worker): it gets a copy, the original stays to write on.
    this.task = this.pdfjs.getDocument({ data: this.bytes.slice(), ...PDFJS_OPTIONS });
    try {
      this.doc = await this.task.promise;
    } catch (error) {
      if (error && error.name === "PasswordException") {
        throw new PdfError("password", "This PDF is password-protected; download it to open.");
      }
      throw new PdfError("unreadable", "This PDF could not be opened here.");
    }
    if (this.destroyed) return;
    if (this.doc.numPages > MAX_PDF_PAGES) {
      throw new PdfError("too_many", `This PDF has ${this.doc.numPages.toLocaleString("en")} pages; up to ` +
                         `${MAX_PDF_PAGES.toLocaleString("en")} are shown here. Download it to open.`);
    }
    const first = await this.doc.getPage(1);
    const base = first.getViewport({ scale: 1 });
    for (let i = 0; i < this.doc.numPages; i += 1) {
      const box = this.el("div", "pdf-page");
      box.dataset.page = String(i + 1);
      box.setAttribute("aria-label", `Page ${i + 1}`);
      this.pagesBox.append(box);
      // Until a page is read, it is as large as the first (PDF.js's own viewer guesses alike).
      this.pages.push({ box, size: i === 0 ? { width: base.width, height: base.height } : null, canvas: null,
                        task: null, scale: 0, note: null });
    }
    this.guess = { width: base.width, height: base.height };
    await this.readStructure();
    this.layout();
    this.watch();
    this.reportPage(true);
  }

  /* §5.4: what can be written on — never an encrypted PDF, nor one whose cross-reference pdfdoc.js cannot
   * read (PDF.js, which repairs, may still show it). */
  async readStructure() {
    try {
      this.file = await openPdf(this.bytes);
    } catch (ignored) {
      this.readOnly = "This PDF's structure could not be read here: it can be viewed, not written on.";
      return;
    }
    if (this.file.encrypted) {
      this.readOnly = "This PDF is encrypted: it can be viewed here, not written on.";
      return;
    }
    this.writable = true;
    try {
      this.signed = await this.file.signed();
    } catch (ignored) { /* told nothing */ }
  }

  get numPages() {
    return this.pages.length;
  }

  /* --------------------------------------------------------------- size */

  /* The page's scale now: its width fitted to the pane's, times the zoom. */
  scaleOf(page) {
    const width = Math.max(1, this.element.clientWidth - 2 * PAGE_GAP);
    return (width / (page.size || this.guess).width) * this.zoom;
  }

  layout() {
    this.tops = null;
    for (const page of this.pages) {
      const size = page.size || this.guess;
      const scale = this.scaleOf(page);
      page.box.style.width = `${Math.floor(size.width * scale)}px`;
      page.box.style.height = `${Math.floor(size.height * scale)}px`;
    }
  }

  /* Zoom by ``factor`` keeping the point at (x, y) on screen where it is. */
  zoomBy(factor, x, y) {
    const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, this.zoom * factor));
    if (zoom === this.zoom) return;
    const rect = this.element.getBoundingClientRect();
    const px = x - rect.left;
    const py = y - rect.top;
    const contentX = this.element.scrollLeft + px;
    const contentY = this.element.scrollTop + py;
    const ratio = zoom / this.zoom;
    this.zoom = zoom;
    this.layout();
    this.element.scrollLeft = contentX * ratio - px;
    this.element.scrollTop = contentY * ratio - py;
    clearTimeout(this.redrawTimer);
    this.redrawTimer = setTimeout(() => this.drawNear(), REDRAW_MS);
  }

  /* --------------------------------------------------------------- input */

  watch() {
    this.visible = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const index = Number(entry.target.dataset.page) - 1;
        if (entry.isIntersecting) this.near.add(index);
        else this.near.delete(index);
      }
      this.drawNear();
    }, { root: this.element, rootMargin: AROUND });
    for (const page of this.pages) this.visible.observe(page.box);
    let width = this.element.clientWidth;
    this.resized = new ResizeObserver(() => {
      if (this.element.clientWidth === width || this.element.clientWidth === 0) return;
      width = this.element.clientWidth; // the panel resized or expanded: fit its width again
      const page = this.current;
      this.layout();
      this.goTo(page + 1);
      clearTimeout(this.redrawTimer);
      this.redrawTimer = setTimeout(() => this.drawNear(), REDRAW_MS);
    });
    this.resized.observe(this.element);
    let pending = false;
    this.element.addEventListener("scroll", () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; this.reportPage(); });
    }, { passive: true });
    this.element.addEventListener("wheel", (event) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      // A mouse wheel's notch is a step; a trackpad's pinch, also Ctrl+wheel, comes in small deltas.
      const step = Math.abs(event.deltaY) >= 50 ? Math.sign(event.deltaY) * 10 : event.deltaY;
      this.zoomBy(Math.exp(-step / 100), event.clientX, event.clientY);
    }, { passive: false });
    // Two fingers zoom the pages (§4.1 A): the box allows panning only, so the browser leaves pinches to us.
    const distance = (touches) => Math.hypot(touches[0].clientX - touches[1].clientX,
                                             touches[0].clientY - touches[1].clientY);
    const middle = (touches) => [(touches[0].clientX + touches[1].clientX) / 2,
                                 (touches[0].clientY + touches[1].clientY) / 2];
    // An Apple Pencil's touch (touchType "stylus") is kept from scrolling while a tool is picked (§4.1 A);
    // a pinch is two fingers, never a pencil and a resting palm.
    const stylus = (event) => [...event.changedTouches].some((t) => t.touchType === "stylus");
    const fingers = (event) => [...event.touches].filter((t) => t.touchType !== "stylus");
    this.element.addEventListener("touchstart", (event) => {
      if (this.tool && this.writable && stylus(event)) {
        event.preventDefault();
        return;
      }
      const touches = fingers(event);
      if (touches.length !== 2 || this.active) return;
      event.preventDefault();
      const [x, y] = middle(touches);
      const rect = this.pagesBox.getBoundingClientRect();
      this.pinch = { from: distance(touches), ratio: 1, x, y };
      this.pagesBox.style.transformOrigin = `${x - rect.left}px ${y - rect.top}px`;
    }, { passive: false });
    this.element.addEventListener("touchmove", (event) => {
      if (this.tool && this.writable && stylus(event)) {
        event.preventDefault();
        return;
      }
      const touches = fingers(event);
      if (!this.pinch || touches.length !== 2) return;
      event.preventDefault();
      const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, this.zoom * distance(touches) / this.pinch.from));
      this.pinch.ratio = zoom / this.zoom;
      this.pagesBox.style.transform = `scale(${this.pinch.ratio})`; // drawn again at that scale once let go
    }, { passive: false });
    const endPinch = (event) => {
      if (!this.pinch || fingers(event).length >= 2) return;
      const { ratio, x, y } = this.pinch;
      this.pinch = null;
      this.pagesBox.style.transform = "";
      this.zoomBy(ratio, x, y);
    };
    this.element.addEventListener("touchend", endPinch);
    this.element.addEventListener("touchcancel", endPinch);
    for (const type of ["gesturestart", "gesturechange"]) { // Safari's own pinch of the whole page
      this.element.addEventListener(type, (event) => event.preventDefault(), { passive: false });
    }
    this.inputs();
  }

  /* Each page's top in the view's scrolled content, the coordinates of its scrollTop — never offsetTop,
   * which counts from whatever positioned box holds the view (PR #102 review). Kept until a layout. */
  pageTops() {
    if (!this.tops) {
      const origin = this.element.getBoundingClientRect().top + this.element.clientTop - this.element.scrollTop;
      this.tops = this.pages.map((page) => page.box.getBoundingClientRect().top - origin);
    }
    return this.tops;
  }

  reportPage(always = false) {
    if (!this.pages.length || this.pinch) return; // mid-pinch the boxes are scaled: read again once let go
    const index = pageAt(this.pageTops(), this.element.scrollTop + this.element.clientHeight / 2);
    if (index === this.current && !always) return;
    this.current = index;
    this.onPage(index + 1, this.pages.length);
  }

  goTo(number) {
    if (!this.pages.length) return;
    const index = Math.min(this.pages.length, Math.max(1, number)) - 1;
    this.element.scrollTop = this.pageTops()[index] - PAGE_GAP;
    this.reportPage(true);
  }

  /* --------------------------------------------------------------- drawing */

  drawNear() {
    if (this.destroyed) return;
    this.pages.forEach((page, index) => {
      if (this.near.has(index)) {
        if (page.scale !== this.scaleOf(page)) this.draw(index);
      } else if (page.canvas || page.task) {
        this.drop(page);
      }
    });
  }

  drop(page) {
    if (page.task) page.task.cancel();
    page.task = null;
    if (page.canvas) {
      page.canvas.width = 0; // gives its memory back at once on iOS
      page.canvas.height = 0;
      page.canvas.remove();
    }
    page.canvas = null;
    page.scale = 0;
    for (const layer of ["inkCanvas", "liveCanvas"]) {
      if (!page[layer]) continue;
      page[layer].width = 0;
      page[layer].height = 0;
      page[layer].remove();
      page[layer] = null;
    }
  }

  async draw(index) {
    const page = this.pages[index];
    if (page.task) page.task.cancel();
    let scale = this.scaleOf(page);
    page.scale = scale;
    let pdfPage;
    try {
      pdfPage = await this.doc.getPage(index + 1);
    } catch (ignored) {
      return; // a page PDF.js cannot read stays blank
    }
    if (this.destroyed || page.scale !== scale) return;
    if (!page.size) {
      const own = pdfPage.getViewport({ scale: 1 });
      page.size = { width: own.width, height: own.height };
      if (own.width !== this.guess.width || own.height !== this.guess.height) {
        this.layout(); // its own shape, now known
        scale = this.scaleOf(page);
        page.scale = scale;
      }
    }
    page.pdfPage = pdfPage;
    if (this.writable && !this.ink.loaded(index)) {
      await this.readInk(index);
      if (this.destroyed || page.scale !== scale) return;
    }
    const viewport = pdfPage.getViewport({ scale });
    const ratio = outputScale(viewport.width, viewport.height, globalThis.devicePixelRatio);
    if (ratio < MIN_OUTPUT_SCALE) {
      if (page.canvas) this.drop(page);
      page.scale = scale; // not tried again until the zoom changes
      if (!page.note) {
        page.note = this.el("p", "pdf-page-note", "This page is too large to draw here. Zoom out, or download the PDF.");
        page.box.append(page.note);
      }
      return;
    }
    const canvas = this.el("canvas", "pdf-canvas");
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    const task = pdfPage.render({
      canvasContext: canvas.getContext("2d"), viewport,
      transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
      // Annotations as pictures only (§3.4); HIMARI's ink left out, being this view's own layer (§4.4 B).
      annotationMode: this.writable ? this.pdfjs.AnnotationMode.ENABLE_STORAGE : this.pdfjs.AnnotationMode.ENABLE,
    });
    page.task = task;
    try {
      await task.promise;
    } catch (ignored) {
      return; // cancelled, or a page PDF.js could not draw
    } finally {
      if (page.task === task) page.task = null;
    }
    if (this.destroyed || page.scale !== scale || !this.near.has(index)) {
      canvas.width = 0;
      canvas.height = 0;
      return;
    }
    if (page.canvas) {
      page.canvas.width = 0;
      page.canvas.height = 0;
      page.canvas.remove();
    }
    if (page.note) {
      page.note.remove();
      page.note = null;
    }
    page.canvas = canvas;
    page.scale = scale;
    page.viewport = viewport;
    page.ratio = ratio;
    page.box.append(canvas);
    this.paintInk(index);
  }

  /* --------------------------------------------------------------- ink (§4) */

  /* A page's saved HIMARI ink: hidden from PDF.js's drawing of the page (marked deleted in its annotation
   * storage), held by the ink layer. A page whose ink cannot be read keeps it in its drawing, not erasable. */
  async readInk(index) {
    const page = this.pages[index];
    const pdfPage = page.pdfPage || await this.doc.getPage(index + 1);
    page.pdfPage = pdfPage;
    let entries = [];
    try {
      entries = await this.file.ink(new Ref(pdfPage.ref.num, pdfPage.ref.gen));
    } catch (ignored) { /* left in the page's drawing */ }
    if (this.ink.loaded(index)) return; // read meanwhile
    for (const entry of entries) {
      this.doc.annotationStorage.setValue(`${HIDDEN_PREFIX}${entry.ref.key}`,
                                          { deleted: true, id: entry.ref.key, pageIndex: index });
    }
    this.ink.load(index, entries.map((entry) => strokeFromSaved(index, entry)));
  }

  layer(page, name, className) {
    if (!page[name]) {
      page[name] = this.el("canvas", className);
      page[name].width = page.canvas.width;
      page[name].height = page.canvas.height;
      page.box.append(page[name]);
    }
    return page[name];
  }

  /* The strokes on the page's canvas of ink, the ones being erased left out. */
  paintInk(index) {
    const page = this.pages[index];
    if (!page.canvas || !page.viewport) return;
    const strokes = this.ink.on(index);
    if (!strokes.length && !page.inkCanvas) return;
    const canvas = this.layer(page, "inkCanvas", "pdf-ink");
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    const erasing = this.active && this.active.index === index ? this.active.erased : [];
    paint(context, strokes.filter((s) => !erasing.includes(s)), page.viewport, page.ratio);
  }

  /* The stroke being drawn, with the pen's predicted points ahead of it (never kept). */
  paintLive(stroke, predicted) {
    const page = this.pages[stroke.page];
    if (!page.canvas || !page.viewport) return;
    const canvas = this.layer(page, "liveCanvas", "pdf-ink");
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    paint(context, [predicted.length ? { ...stroke, points: [...stroke.points, ...predicted], cached: null } : stroke],
          page.viewport, page.ratio);
  }

  /* A tool picked, or none (scrolling only). */
  setTool(tool) {
    this.tool = tool && this.writable ? tool : null;
    this.element.classList.toggle("is-inking", this.tool !== null);
  }

  inputs() {
    const box = this.pagesBox;
    box.addEventListener("pointerdown", (event) => this.pointerDown(event));
    box.addEventListener("pointermove", (event) => this.pointerMove(event));
    box.addEventListener("pointerup", (event) => this.pointerEnd(event, false));
    box.addEventListener("pointercancel", (event) => this.pointerEnd(event, true));
    box.addEventListener("lostpointercapture", (event) => this.pointerEnd(event, true));
    this.element.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
      event.preventDefault();
      if (event.shiftKey) this.redo();
      else this.undo();
    });
  }

  pageIndexOf(node) {
    for (let at = node; at && at !== this.pagesBox; at = at.parentNode) {
      if (at.dataset && at.dataset.page) return Number(at.dataset.page) - 1;
    }
    return -1;
  }

  /* The event's point in the page's PDF space, with the pen's pressure (a mouse has none: 0.5). */
  pointOf(event) {
    const { viewport, rect, pen } = this.active;
    const [x, y] = viewport.convertToPdfPoint(event.clientX - rect.left, event.clientY - rect.top);
    return [x, y, pen && event.pressure > 0 ? event.pressure : 0.5];
  }

  pointerDown(event) {
    if (!this.tool || !this.writable || this.saving || this.pinch || this.active) return;
    if (event.pointerType === "touch") return; // fingers scroll and zoom; a resting palm does nothing (§4.1)
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const index = this.pageIndexOf(event.target);
    const page = this.pages[index];
    if (!page || !page.pdfPage || !page.canvas || !this.ink.loaded(index)) return;
    event.preventDefault();
    try {
      this.pagesBox.setPointerCapture(event.pointerId);
    } catch (ignored) { /* drawn while over the page */ }
    if (this.element.focus) this.element.focus({ preventScroll: true });
    this.active = { id: event.pointerId, index, pen: event.pointerType === "pen", erased: [], stroke: null,
                    viewport: page.pdfPage.getViewport({ scale: this.scaleOf(page) }),
                    rect: page.box.getBoundingClientRect() };
    if (this.tool.tool === "eraser") {
      this.eraseAt(event);
      return;
    }
    if (this.signed && !this.toldSigned) {
      this.toldSigned = true;
      this.onNotice(SIGNED_NOTICE);
    }
    this.active.stroke = newStroke(index, this.tool, this.active.pen);
    this.extend([event], []);
  }

  pointerMove(event) {
    const active = this.active;
    if (!active || event.pointerId !== active.id) return;
    const events = (event.getCoalescedEvents && event.getCoalescedEvents()) || [];
    if (!active.stroke) {
      for (const each of events.length ? events : [event]) this.eraseAt(each);
      return;
    }
    const predicted = (event.getPredictedEvents && event.getPredictedEvents()) || [];
    this.extend(events.length ? events : [event], predicted);
  }

  extend(events, predicted) {
    const stroke = this.active.stroke;
    for (const event of events) stroke.points.push(this.pointOf(event));
    if (stroke.points.length >= MAX_POINTS) {
      this.finish(false);
      return;
    }
    this.paintLive(stroke, predicted.map((event) => this.pointOf(event)));
  }

  eraseAt(event) {
    const { index, erased } = this.active;
    const [x, y] = this.pointOf(event);
    const reach = ERASER_PX / this.scaleOf(this.pages[index]);
    const found = this.ink.on(index).filter((s) => !erased.includes(s) && hits(s, x, y, reach));
    if (!found.length) return;
    erased.push(...found);
    this.paintInk(index);
  }

  /* §4.1: a stroke ends when the pen lifts; cut short (pointercancel, a lost capture: a system gesture, an
   * alert), it keeps the points it got, unless fewer than two. */
  pointerEnd(event, cut) {
    if (!this.active || event.pointerId !== this.active.id) return;
    this.finish(cut);
  }

  finish(cut) {
    const active = this.active;
    this.active = null;
    try {
      this.pagesBox.releasePointerCapture(active.id);
    } catch (ignored) { /* released already */ }
    const page = this.pages[active.index];
    if (page.liveCanvas) {
      page.liveCanvas.width = 0;
      page.liveCanvas.height = 0;
      page.liveCanvas.remove();
      page.liveCanvas = null;
    }
    if (active.stroke) {
      if (active.stroke.points.length >= (cut ? 2 : 1)) this.ink.add(active.stroke);
    } else {
      this.ink.erase(active.index, active.erased);
    }
    this.paintInk(active.index);
    this.onInk();
  }

  undo() {
    if (this.active || this.saving) return;
    const page = this.ink.undo();
    if (page !== null) {
      this.paintInk(page);
      this.onInk();
    }
  }

  redo() {
    if (this.active || this.saving) return;
    const page = this.ink.redo();
    if (page !== null) {
      this.paintInk(page);
      this.onInk();
    }
  }

  /* --------------------------------------------------------------- saving (§5) */

  /* The bytes to add after the file's own for the ink drawn and erased since it was read, checked as the
   * server checks them (§5.2), within 64 MiB in all; null when there is nothing to save. */
  async increment() {
    const changes = this.ink.changes();
    if (!changes.length) return null;
    const pages = [];
    for (const { page, add, drop } of changes) {
      const pdfPage = this.pages[page].pdfPage || await this.doc.getPage(page + 1);
      pages.push({ ref: new Ref(pdfPage.ref.num, pdfPage.ref.gen), drop, add: add.map(specOf) });
    }
    let increment;
    try {
      increment = await appendIncrement(this.file, pages);
    } catch (ignored) {
      throw new PdfError("unsaved", "Not saved: the PDF could not be updated in place.");
    }
    if (!checkIncrement(this.file.size, increment)) {
      throw new PdfError("unsaved", "Not saved: the PDF could not be updated in place.");
    }
    if (this.file.size + increment.length > MAX_PDF_BYTES) {
      throw new PdfError("too_large", "Too large to save here: the PDF would be over 64 MiB.");
    }
    return increment;
  }

  /* After the server took ``increment``: the file is its bytes and the increment (never read again from the
   * server), its ink read back — saved now, with names to erase by; undo starts again. */
  async saved(increment) {
    const bytes = new Uint8Array(this.file.size + increment.length);
    bytes.set(this.file.bytes);
    bytes.set(increment, this.file.size);
    this.bytes = bytes;
    this.file = await openPdf(bytes);
    const read = [...this.ink.saved.keys()];
    this.ink = new Ink();
    for (const index of read) await this.readInk(index);
    for (const index of read) this.paintInk(index);
    this.onInk();
  }

  /* §5.5: unsaved ink from another version put on this one; the strokes left (on pages it lacks) counted. */
  async carryIn(carried) {
    const left = this.ink.put(carried, this.pages.length);
    for (const index of new Set(carried.erasePages)) if (index < this.pages.length) await this.readInk(index);
    for (let index = 0; index < this.pages.length; index += 1) this.paintInk(index);
    this.onInk();
    return left;
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.redrawTimer);
    if (this.visible) this.visible.disconnect();
    if (this.resized) this.resized.disconnect();
    for (const page of this.pages) this.drop(page);
    if (this.task) this.task.destroy(); // the document and its worker
  }
}

function paint(context, strokes, viewport, ratio) {
  for (const stroke of strokes) {
    const polygon = outline(stroke);
    if (!polygon.length) continue;
    context.globalAlpha = stroke.tool === "highlighter" ? HIGHLIGHT_ALPHA : 1;
    context.fillStyle = `rgb(${stroke.rgb.map((v) => Math.round(v * 255)).join(", ")})`;
    context.beginPath();
    polygon.forEach(([x, y], i) => {
      const [vx, vy] = viewport.convertToViewportPoint(x, y);
      if (i === 0) context.moveTo(vx * ratio, vy * ratio);
      else context.lineTo(vx * ratio, vy * ratio);
    });
    context.closePath();
    context.fill();
  }
  context.globalAlpha = 1;
}
