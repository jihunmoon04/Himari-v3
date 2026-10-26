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
 *
 * A page's layers, bottom to top: its drawing, its ink, the stroke being
 * drawn. That last has a canvas of its own (the live layer): only as large
 * as the part of the page in view (followed if the view scrolls), kept from
 * one stroke to the next, painted once a frame (however many points came)
 * where it changed, and multiplied with the page as the ink is, so over the
 * page a stroke looks the same before and after the pen lifts (where it
 * crosses older ink, the ink's own painting decides once it is lifted). The pen lifted, the stroke
 * is added on top of the page's ink, not the whole page painted again. With
 * the ink lab on (inklab.js), each stroke's numbers are kept for it.
 */

import { DEFAULT_TUNING, HIGHLIGHT_ALPHA, Ink, MAX_POINTS, currentTuning, hits, newStroke, outline, setTuning, specOf,
         strokeFromSaved } from "./pdfink.js";
import { Ref, appendIncrement, checkIncrement, openPdf } from "./pdfdoc.js";
import { InkLab, labOn } from "./inklab.js";

export const MAX_PDF_BYTES = 64 * 1024 * 1024; // §3.3, Q2: viewed here up to this; larger ones are downloaded
export const MAX_INCREMENT_BYTES = 8 * 1024 * 1024; // one save's bytes, as the server takes (V3-P77 §5.3)
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
   * ink changed (drawn, erased, undone, saved). It may be opened before it is shown (show()). */
  constructor({ pdfjs, el, bytes, onPage = () => {}, onInk = () => {} }) {
    this.pdfjs = pdfjs;
    this.el = el;
    this.bytes = bytes;
    this.onPage = onPage;
    this.onInk = onInk;
    this.ink = new Ink();
    this.file = null; // pdfdoc.js's reading of the bytes, to write on
    this.writable = false;
    this.readOnly = null; // why it is not written on, when it is not
    this.signed = false;
    this.tool = null; // {tool: "pen" | "highlighter" | "eraser", color, size}; null: scrolling only
    this.active = null; // the stroke or erasing under way
    this.frozen = false; // no drawing while another version is read in its place (V3-P82 §3.4); a save never
    this.cut = null; // V3-P82 §3.6: where a save cut short at its end is to be cut off, when there is one
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
    // The live layer: {canvas, page, left, top, width, height, ratio, painted: its pixels drawn on, or null}.
    this.live = null;
    this.lab = labOn() ? new InkLab({ el, tuning: { defaults: DEFAULT_TUNING, current: currentTuning, set: setTuning },
                                      retuned: () => this.pages.forEach((page, index) => this.paintInk(index)) })
      : null;
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
    if (this.lab && this.writable) this.element.append(this.lab.element);
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
    this.cut = this.file.cut;
    try {
      this.signed = await this.file.signed();
    } catch (ignored) { /* told nothing */ }
  }

  get numPages() {
    return this.pages.length;
  }

  /* Its element now in place: fitted to its width, on ``page``. */
  show(page = 1) {
    this.current = Math.min(this.pages.length, Math.max(1, page)) - 1;
    this.layout();
    if (page > 1) this.goTo(page);
    else this.reportPage(true);
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
    // The ink lab's controls take a pencil's touch as any other.
    const stylus = (event) => [...event.changedTouches].some((t) => t.touchType === "stylus")
      && !(this.lab && this.lab.element.contains && this.lab.element.contains(event.target));
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
    if (page.inkCanvas) {
      page.inkCanvas.width = 0;
      page.inkCanvas.height = 0;
      page.inkCanvas.remove();
      page.inkCanvas = null;
    }
    if (this.live && this.live.page === page) this.dropLive();
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
    page.box.prepend(canvas); // under its ink and the live layer, which were there before it (review B1)
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

  /* The page's canvas of ink, as large as its drawing (made again at a new zoom, empty then); over the
   * drawing, under the live layer. */
  inkLayer(page) {
    if (!page.inkCanvas) {
      page.inkCanvas = this.el("canvas", "pdf-ink");
      page.canvas.after(page.inkCanvas);
    }
    if (page.inkCanvas.width !== page.canvas.width || page.inkCanvas.height !== page.canvas.height) {
      page.inkCanvas.width = page.canvas.width;
      page.inkCanvas.height = page.canvas.height;
    }
    return page.inkCanvas;
  }

  /* The strokes on the page's canvas of ink, the ones being erased left out. */
  paintInk(index) {
    const page = this.pages[index];
    if (!page.canvas || !page.viewport) return;
    const strokes = this.ink.on(index);
    if (!strokes.length && !page.inkCanvas) return;
    const canvas = this.inkLayer(page);
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    const erasing = this.active && this.active.index === index ? this.active.erased : [];
    paint(context, strokes.filter((s) => !erasing.includes(s)), matrixOf(page.viewport, page.ratio));
  }

  /* A stroke just added, on top of the page's ink — the whole page painted again only when its canvas is not
   * the drawing's size (new, or a zoom drawn since). */
  paintAdded(index, stroke) {
    const page = this.pages[index];
    if (!page.canvas || !page.viewport) return;
    const canvas = page.inkCanvas;
    if (!canvas || canvas.width !== page.canvas.width || canvas.height !== page.canvas.height) {
      this.paintInk(index);
      return;
    }
    paint(canvas.getContext("2d"), [stroke], matrixOf(page.viewport, page.ratio));
  }

  /* The live layer over the part of ``page`` in view (``rect``: the page's box on screen), at its drawing's
   * pixels per CSS pixel, on top of the page's layers: made again only when that changed, emptied when it
   * moved. False when none of the page is in view. */
  liveLayer(page, rect) {
    const view = this.element.getBoundingClientRect();
    const left = Math.max(0, Math.floor(view.left - rect.left));
    const top = Math.max(0, Math.floor(view.top - rect.top));
    const width = Math.min(rect.width, Math.ceil(view.right - rect.left)) - left;
    const height = Math.min(rect.height, Math.ceil(view.bottom - rect.top)) - top;
    if (!(width > 0 && height > 0)) return false;
    if (!this.live) this.live = { canvas: this.el("canvas", "pdf-live"), page: null, painted: null };
    const live = this.live;
    const pixels = [Math.ceil(width * page.ratio), Math.ceil(height * page.ratio)];
    if (live.canvas.width !== pixels[0] || live.canvas.height !== pixels[1]) {
      [live.canvas.width, live.canvas.height] = pixels; // emptied
      live.painted = null;
    }
    if (live.page !== page || live.left !== left || live.top !== top || live.width !== width || live.height !== height) {
      this.clearLive();
      Object.assign(live.canvas.style, { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` });
      Object.assign(live, { page, left, top, width, height });
    }
    live.ratio = page.ratio;
    if (page.box.lastChild !== live.canvas) page.box.append(live.canvas); // above an ink layer made since (C1)
    return true;
  }

  /* The live layer emptied where it was drawn on. */
  clearLive() {
    const live = this.live;
    if (!live || !live.painted) return;
    const [x0, y0, x1, y1] = live.painted;
    live.canvas.getContext("2d").clearRect(x0, y0, x1 - x0, y1 - y0);
    live.painted = null;
  }

  /* The live layer gone, its memory given back at once (its page dropped, or the view). */
  dropLive() {
    this.live.canvas.width = 0;
    this.live.canvas.height = 0;
    this.live.canvas.remove();
    this.live = null;
  }

  /* The stroke being drawn, with the pen's predicted points ahead of it (never kept): the live layer put
   * over what is in view now, where it was painted last emptied, and the stroke painted again. */
  paintLive(active) {
    const page = this.pages[active.index];
    if (!page.canvas || !this.liveLayer(page, active.rect)) return;
    const live = this.live;
    const { stroke, predicted } = active;
    this.clearLive();
    const shown = predicted.length ? { ...stroke, points: [...stroke.points, ...predicted], cached: null } : stroke;
    live.painted = paint(live.canvas.getContext("2d"), [shown],
                         matrixOf(active.viewport, live.ratio, live.left, live.top), live.canvas);
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
    if (!this.tool || !this.writable || this.frozen || this.pinch || this.active) return;
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
                    rect: page.box.getBoundingClientRect(), predicted: [], frame: null };
    if (this.tool.tool === "eraser") {
      this.eraseAt(event);
      return;
    }
    this.active.stroke = newStroke(index, this.tool, this.active.pen);
    if (this.lab) this.lab.begin(event);
    this.extend([event], []);
  }

  pointerMove(event) {
    const active = this.active;
    if (!active || event.pointerId !== active.id) return;
    // Where the page is now: a word shown above the pages while drawing moves them (PR #104 review P2).
    active.rect = this.pages[active.index].box.getBoundingClientRect();
    const events = (event.getCoalescedEvents && event.getCoalescedEvents()) || [];
    if (!active.stroke) {
      for (const each of events.length ? events : [event]) this.eraseAt(each);
      return;
    }
    const predicted = (event.getPredictedEvents && event.getPredictedEvents()) || [];
    if (this.lab) this.lab.input(events.length ? events : [event], predicted);
    this.extend(events.length ? events : [event], predicted);
  }

  /* The points taken; the stroke painted at the next frame (once, however many moves come before it). */
  extend(events, predicted) {
    const active = this.active;
    const stroke = active.stroke;
    for (const event of events) stroke.points.push(this.pointOf(event));
    if (stroke.points.length >= MAX_POINTS) {
      this.finish(false);
      return;
    }
    active.predicted = predicted.map((event) => this.pointOf(event));
    if (active.frame === null) active.frame = requestAnimationFrame(() => this.paintFrame(active));
  }

  /* A frame's painting of the stroke under way (timed for the lab). */
  paintFrame(active) {
    active.frame = null;
    if (this.active !== active) return; // the pen lifted first
    const start = this.lab ? performance.now() : 0;
    this.paintLive(active);
    if (this.lab) {
      const live = this.live;
      this.lab.frame(start, performance.now() - start, live ? `${live.canvas.width}×${live.canvas.height}` : "none");
    }
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
    const start = this.lab ? performance.now() : 0;
    this.clearLive();
    if (active.stroke) {
      const kept = active.stroke.points.length >= (cut ? 2 : 1);
      if (kept) {
        this.ink.add(active.stroke);
        this.paintAdded(active.index, active.stroke);
      }
      if (this.lab) this.lab.end(performance.now() - start, kept ? active.stroke.points.length : 0);
    } else {
      this.ink.erase(active.index, active.erased);
      this.paintInk(active.index);
    }
    this.onInk();
  }

  undo() {
    if (this.active || this.frozen) return;
    const page = this.ink.undo();
    if (page !== null) {
      this.paintInk(page);
      this.onInk();
    }
  }

  redo() {
    if (this.active || this.frozen) return;
    const page = this.ink.redo();
    if (page !== null) {
      this.paintInk(page);
      this.onInk();
    }
  }

  /* --------------------------------------------------------------- saving (§5) */

  /* The next save (V3-P82 §3.4): {bytes: what to add after the file's own, checked as the server checks it,
   * changes: what they hold, added: each added stroke's object} — the changes as they are now, or the first
   * pages of them while the bytes would be over MAX_INCREMENT_BYTES (the rest go in the next save); null when
   * there is nothing to save. A page with strokes not yet read (carried here) is read first, so a stroke it
   * already holds is not written again. */
  async increment() {
    let changes = this.ink.changes();
    if (!changes.length) return null;
    for (const { page } of changes) if (!this.ink.loaded(page)) await this.readInk(page);
    changes = this.ink.changes();
    if (!changes.length) return null;
    for (;;) {
      const pages = [];
      for (const { page, add, restore, drop } of changes) {
        const pdfPage = this.pages[page].pdfPage || await this.doc.getPage(page + 1);
        pages.push({ ref: new Ref(pdfPage.ref.num, pdfPage.ref.gen), drop: drop.map((d) => d.ref),
                     restore: restore.map((r) => r.ref), add: add.map(specOf) });
      }
      let made;
      try {
        made = await appendIncrement(this.file, pages);
      } catch (ignored) {
        throw new PdfError("unsaved", "Not saved: the PDF could not be updated in place.");
      }
      if (!checkIncrement(this.file.size, made.bytes)) {
        throw new PdfError("unsaved", "Not saved: the PDF could not be updated in place.");
      }
      if (made.bytes.length > MAX_INCREMENT_BYTES && changes.length > 1) {
        changes = changes.slice(0, Math.ceil(changes.length / 2));
        continue;
      }
      if (made.bytes.length > MAX_INCREMENT_BYTES) {
        throw new PdfError("too_large", "Too much ink on one page to save at once (over 8 MiB).");
      }
      if (this.file.size + made.bytes.length > MAX_PDF_BYTES) {
        throw new PdfError("too_large", "Too large to save here: the PDF would be over 64 MiB.");
      }
      return { bytes: made.bytes, changes, added: made.added };
    }
  }

  /* After the server took a save (``increment()``'s): the file is its bytes and the save's (never read again
   * from the server), in a buffer with room for the next saves (no copy of the whole each time, V3-P82 §3.4);
   * what the save held is saved where it is now. Nothing is read back; undo goes on. */
  async saved({ bytes: increment, changes, added }) {
    const size = this.file.size;
    const total = size + increment.length;
    if (!this.buffer || this.buffer.byteLength < total) {
      const room = Math.min(MAX_PDF_BYTES + MAX_INCREMENT_BYTES, Math.max(total, Math.ceil(size * 1.25) + (1 << 20)));
      const buffer = new ArrayBuffer(room);
      new Uint8Array(buffer).set(this.file.bytes.subarray(0, size));
      this.buffer = buffer;
    }
    new Uint8Array(this.buffer).set(increment, size);
    this.bytes = new Uint8Array(this.buffer, 0, total);
    this.file = await openPdf(this.bytes);
    this.ink.committed(changes, added);
    this.onInk();
  }

  /* §5.5: what is unsaved, to put on another version (or keep in the browser): the ink's carry, with each
   * page's object number where it is known (``refs``: page index → {num, gen}; a page with ink has been
   * drawn), by which the other version finds that page (V3-P82 §4, review C4). At once: no await. */
  carry() {
    const carried = this.ink.carry();
    const refs = new Map();
    for (const index of new Set([...carried.add.map((s) => s.page), ...carried.erasePages])) {
      const pdfPage = this.pages[index] && this.pages[index].pdfPage;
      if (pdfPage && pdfPage.ref) refs.set(index, { num: pdfPage.ref.num, gen: pdfPage.ref.gen });
    }
    return { ...carried, refs };
  }

  /* V3-P82 §3.7: the whole file with its unsaved ink, past any limit — to download when it cannot be saved. */
  async withInk() {
    const changes = this.ink.changes();
    const pages = [];
    for (const { page, add, restore, drop } of changes) {
      const pdfPage = this.pages[page].pdfPage || await this.doc.getPage(page + 1);
      pages.push({ ref: new Ref(pdfPage.ref.num, pdfPage.ref.gen), drop: drop.map((d) => d.ref),
                   restore: restore.map((r) => r.ref), add: add.map(specOf) });
    }
    const { bytes } = await appendIncrement(this.file, pages);
    const whole = new Uint8Array(this.file.size + bytes.length);
    whole.set(this.file.bytes.subarray(0, this.file.size));
    whole.set(bytes, this.file.size);
    return whole;
  }

  /* §5.5: unsaved ink from another version put on this one, each page found by its object number (else by
   * its number); the strokes left (on pages it lacks) counted. */
  async carryIn(carried) {
    const where = new Map();
    for (const [index, ref] of carried.refs || []) {
      try {
        where.set(index, await this.doc.getPageIndex(ref));
      } catch (ignored) { /* not a page of this version: by its number */ }
    }
    const moved = (index) => (where.has(index) ? where.get(index) : index);
    const placed = { ...carried, add: carried.add.map((s) => ({ ...s, page: moved(s.page) })),
                     erasePages: carried.erasePages.map(moved) };
    const left = this.ink.put(placed, this.pages.length);
    for (const index of new Set(placed.erasePages)) if (index < this.pages.length) await this.readInk(index);
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
    if (this.live) this.dropLive();
    if (this.task) this.task.destroy(); // the document and its worker
  }
}

/* From a page's PDF space to a canvas's pixels: ``viewport`` (to CSS pixels on the page), times ``ratio``
 * pixels per CSS pixel, for a canvas whose corner is at (left, top) on the page. */
function matrixOf(viewport, ratio, left = 0, top = 0) {
  const [a, b, c, d, e, f] = viewport.transform;
  return [a * ratio, b * ratio, c * ratio, d * ratio, (e - left) * ratio, (f - top) * ratio];
}

/* The strokes' outlines filled, their points carried to the canvas by ``matrix`` (the context's transform,
 * not each point's). With ``canvas``, the pixels drawn on, [x0, y0, x1, y1] within it (null for none). */
function paint(context, strokes, matrix, canvas = null) {
  const [a, b, c, d, e, f] = matrix;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  context.setTransform(a, b, c, d, e, f);
  for (const stroke of strokes) {
    const polygon = outline(stroke);
    if (!polygon.length) continue;
    context.globalAlpha = stroke.tool === "highlighter" ? HIGHLIGHT_ALPHA : 1;
    context.fillStyle = `rgb(${stroke.rgb.map((v) => Math.round(v * 255)).join(", ")})`;
    context.beginPath();
    for (let i = 0; i < polygon.length; i += 1) {
      const [x, y] = polygon[i];
      if (i === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
      if (canvas) {
        const px = a * x + c * y + e;
        const py = b * x + d * y + f;
        x0 = Math.min(x0, px); y0 = Math.min(y0, py); x1 = Math.max(x1, px); y1 = Math.max(y1, py);
      }
    }
    context.closePath();
    context.fill();
  }
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.globalAlpha = 1;
  if (!canvas || x0 > x1) return null;
  // Antialiasing reaches a pixel or two past the outline.
  return [Math.max(0, Math.floor(x0) - 2), Math.max(0, Math.floor(y0) - 2),
          Math.min(canvas.width, Math.ceil(x1) + 2), Math.min(canvas.height, Math.ceil(y1) + 2)];
}
