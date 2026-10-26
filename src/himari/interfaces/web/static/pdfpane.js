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
 * stays under MAX_CANVAS_PIXELS (iOS Safari's limit), giving up sharpness
 * first. Each page fits the pane's width, a turned one too, so nothing needs
 * scrolling sideways until zoomed; Ctrl+wheel and two fingers zoom the pages,
 * never the whole screen.
 */

export const MAX_PDF_BYTES = 64 * 1024 * 1024; // §3.3, Q2: viewed here up to this; larger ones are downloaded
export const MAX_CANVAS_PIXELS = 16_000_000;
export const ZOOM_MIN = 0.5; // of the fitted width
export const ZOOM_MAX = 5;
const PAGE_GAP = 12; // px between pages, and around them
const AROUND = "100% 0px"; // pages drawn beyond the view: one screen up and down
const REDRAW_MS = 150; // after a zoom, before the pages are drawn again at the new scale

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

/* The canvas's pixels per CSS pixel: the screen's, unless the canvas would pass MAX_CANVAS_PIXELS. */
export function outputScale(width, height, ratio) {
  const most = Math.sqrt(MAX_CANVAS_PIXELS / Math.max(1, width * height));
  return Math.max(0.1, Math.min(ratio || 1, most));
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
    this.code = code; // "password" | "unreadable"
  }
}

export class PdfView {
  /* el(tag, class, text) as the pane's; onPage(page, pages) when the page in view changes. */
  constructor({ pdfjs, el, bytes, onPage = () => {} }) {
    this.pdfjs = pdfjs;
    this.el = el;
    this.bytes = bytes;
    this.onPage = onPage;
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
    this.task = this.pdfjs.getDocument({ data: this.bytes, ...PDFJS_OPTIONS });
    try {
      this.doc = await this.task.promise;
    } catch (error) {
      if (error && error.name === "PasswordException") {
        throw new PdfError("password", "This PDF is password-protected; download it to open.");
      }
      throw new PdfError("unreadable", "This PDF could not be opened here.");
    }
    if (this.destroyed) return;
    const first = await this.doc.getPage(1);
    const base = first.getViewport({ scale: 1 });
    for (let i = 0; i < this.doc.numPages; i += 1) {
      const box = this.el("div", "pdf-page");
      box.dataset.page = String(i + 1);
      box.setAttribute("aria-label", `Page ${i + 1}`);
      this.pagesBox.append(box);
      // Until a page is read, it is as large as the first (PDF.js's own viewer guesses alike).
      this.pages.push({ box, size: i === 0 ? { width: base.width, height: base.height } : null, canvas: null,
                        task: null, scale: 0 });
    }
    this.guess = { width: base.width, height: base.height };
    this.layout();
    this.watch();
    this.reportPage(true);
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
    this.element.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 2) return;
      event.preventDefault();
      const [x, y] = middle(event.touches);
      const rect = this.pagesBox.getBoundingClientRect();
      this.pinch = { from: distance(event.touches), ratio: 1, x, y };
      this.pagesBox.style.transformOrigin = `${x - rect.left}px ${y - rect.top}px`;
    }, { passive: false });
    this.element.addEventListener("touchmove", (event) => {
      if (!this.pinch || event.touches.length !== 2) return;
      event.preventDefault();
      const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, this.zoom * distance(event.touches) / this.pinch.from));
      this.pinch.ratio = zoom / this.zoom;
      this.pagesBox.style.transform = `scale(${this.pinch.ratio})`; // drawn again at that scale once let go
    }, { passive: false });
    const endPinch = (event) => {
      if (!this.pinch || event.touches.length >= 2) return;
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
  }

  reportPage(always = false) {
    if (!this.pages.length) return;
    const tops = this.pages.map((page) => page.box.offsetTop);
    const index = pageAt(tops, this.element.scrollTop + this.element.clientHeight / 2);
    if (index === this.current && !always) return;
    this.current = index;
    this.onPage(index + 1, this.pages.length);
  }

  goTo(number) {
    const page = this.pages[Math.min(this.pages.length, Math.max(1, number)) - 1];
    if (!page) return;
    this.element.scrollTop = page.box.offsetTop - PAGE_GAP;
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
    const viewport = pdfPage.getViewport({ scale });
    const ratio = outputScale(viewport.width, viewport.height, globalThis.devicePixelRatio);
    const canvas = this.el("canvas", "pdf-canvas");
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    const task = pdfPage.render({
      canvasContext: canvas.getContext("2d"), viewport,
      transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
      annotationMode: this.pdfjs.AnnotationMode.ENABLE, // annotations as pictures only (§3.4)
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
    page.canvas = canvas;
    page.scale = scale;
    page.box.append(canvas);
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
