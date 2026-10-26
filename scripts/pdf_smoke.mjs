/* The PDF view's settings and HIMARI's own PDF writing (V3-P77 §3.4, §5, the stage-2 entry
 * conditions of §11), against the bundled PDF.js and tests/fixtures/pdf: no browser, packages
 * or live service. Usage: node scripts/pdf_smoke.mjs */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const STATIC = new URL("../src/himari/interfaces/web/static/", import.meta.url);
const FIXTURES = new URL("../tests/fixtures/pdf/", import.meta.url);
const fixture = (name) => new Uint8Array(readFileSync(new URL(name, FIXTURES)));

const pane = await import(new URL("pdfpane.js", STATIC));
const { openPdf, appendIncrement, checkIncrement, PdfReadError, Ref } = await import(new URL("pdfdoc.js", STATIC));
const pdfjs = await import(new URL("pdfjs/pdf.min.mjs", STATIC));
pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs/pdf.worker.min.mjs", STATIC).href;

// §3.4: the settings, in one place.
assert.equal(pane.PDFJS_OPTIONS.enableXfa, false);
assert.equal(pane.PDFJS_OPTIONS.useWasm, false);
assert.equal(pane.PDFJS_OPTIONS.isEvalSupported, false);
assert.ok(Object.isFrozen(pane.PDFJS_OPTIONS));
assert.equal(pane.MAX_PDF_BYTES, 64 * 1024 * 1024);
// …and the bundle: no WebAssembly shipped, nothing that evaluates code but core-js's globalThis fallback,
// never reached where globalThis exists (and refused by the page's CSP if it were).
const bundled = ["pdfjs/pdf.min.mjs", "pdfjs/pdf.worker.min.mjs", "pdfjs/wasm/openjpeg_nowasm_fallback.js",
                 "pdfjs/wasm/jbig2_nowasm_fallback.js"];
for (const folder of ["pdfjs", "pdfjs/wasm"]) {
  assert.ok(!readdirSync(new URL(folder, STATIC)).some((name) => name.endsWith(".wasm")), `no .wasm in ${folder}`);
}
for (const name of bundled) {
  const code = readFileSync(new URL(name, STATIC), "utf8");
  assert.ok(!/[^\w.$]eval\(/.test(code), `no eval in ${name}`);
  const made = code.match(/Function\("[^"]*"\)/g) || [];
  assert.ok(made.every((call) => call === 'Function("return this")'), `no new Function in ${name}`);
  assert.ok(code.includes("globalThis") || !made.length);
}

// A canvas stays under iOS Safari's limit; small pages keep the screen's sharpness.
assert.equal(pane.outputScale(600, 800, 2), 2);
const big = pane.outputScale(2400, 3200, 3);
assert.ok(Math.floor(2400 * big) * Math.floor(3200 * big) <= pane.MAX_CANVAS_PIXELS && big < 3); // as drawn
// PR #102 review: no floor under the limits — a sliver of a page, very long, stays under both; drawn that
// coarsely it is not drawn at all (below MIN_OUTPUT_SCALE), and the side limit holds for a very wide one.
const drawn = (w, h, s) => [Math.floor(w * s), Math.floor(h * s)];
for (const [w, h] of [[600, 3_000_000], [3_000_000, 600], [20_000, 100], [5000, 5000]]) {
  const s = pane.outputScale(w, h, 2);
  const [cw, ch] = drawn(w, h, s);
  assert.ok(cw * ch <= pane.MAX_CANVAS_PIXELS && cw <= pane.MAX_CANVAS_SIDE && ch <= pane.MAX_CANVAS_SIDE, `${w}×${h}`);
}
assert.ok(pane.outputScale(600, 3_000_000, 2) < pane.MIN_OUTPUT_SCALE);
assert.ok(pane.outputScale(20_000, 100, 2) < 1); // the side, not the area, holds it
assert.equal(pane.pageAt([0, 100, 200], 150), 1);
assert.equal(pane.pageAt([0, 100, 200], 0), 0);
assert.equal(pane.pageAt([0, 100, 200], 999), 2);

// PR #102 review: pages are found in the view's own scrolled coordinates, wherever the view sits on the
// screen — here 258 px down, with each page's offsetTop counting from the document instead.
{
  const VIEW_TOP = 258;
  let view = null;
  class Box {
    constructor(tag, className) {
      Object.assign(this, { tag, className, children: [], parent: null, dataset: {}, style: {}, attributes: {},
                            scrollTop: 0, clientTop: 0, clientWidth: 624, clientHeight: 600 });
    }
    append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener() {}
    remove() {}
    get isPage() { return this.className === "pdf-page"; }
    // Laid out as app.css does: 12 px around and between pages.
    get contentTop() {
      const siblings = this.parent.children;
      let top = 12;
      for (const s of siblings.slice(0, siblings.indexOf(this))) top += parseFloat(s.style.height) + 12;
      return top;
    }
    get offsetTop() { return VIEW_TOP + this.contentTop; } // from the document: what goTo must not use
    getBoundingClientRect() {
      if (!this.isPage) return { top: VIEW_TOP, left: 0 };
      return { top: VIEW_TOP + view.element.clientTop + this.contentTop - view.element.scrollTop, left: 0 };
    }
  }
  globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  const fakePdfjs = (numPages) => ({
    getDocument: () => ({ promise: Promise.resolve({
      numPages, getPage: async () => ({ getViewport: ({ scale }) => ({ width: 600 * scale, height: 800 * scale }) }),
    }), destroy() {} }),
  });
  const shown = [];
  view = new pane.PdfView({ pdfjs: fakePdfjs(3), el: (tag, className) => new Box(tag, className), bytes: new Uint8Array(),
                            onPage: (page, pages) => shown.push([page, pages]) });
  await view.open();
  assert.deepEqual(view.pageTops(), [12, 824, 1636]);
  view.goTo(2);
  assert.equal(view.element.scrollTop, 812, "page 2 at the view's top, less the gap");
  assert.deepEqual(shown.at(-1), [2, 3]);
  view.goTo(3);
  view.element.scrollTop = 900; // scrolled by hand: the middle (1200) is on page 2
  view.reportPage();
  assert.deepEqual(shown.at(-1), [2, 3]);
  view.destroy();

  // A page too long to draw at all: told on the page, no canvas made, nothing rendered; drawn again (and the
  // notice gone) once the zoom lets it.
  let rendered = 0;
  const tall = { AnnotationMode: { ENABLE: 1 }, getDocument: () => ({ promise: Promise.resolve({ numPages: 1, getPage: async () => ({
    getViewport: ({ scale }) => ({ width: 600 * scale, height: (view && view.zoom > 1 ? 3000 : 3_000_000) * scale }),
    render: () => { rendered += 1; return { promise: Promise.resolve(), cancel() {} }; },
  }) }), destroy() {} }) };
  const made = [];
  const el = (tag, className, text) => { const box = new Box(tag, className); box.textContent = text; made.push(box);
                                         if (tag === "canvas") box.getContext = () => ({}); return box; };
  view = new pane.PdfView({ pdfjs: tall, el, bytes: new Uint8Array() });
  await view.open();
  view.near.add(0);
  await view.draw(0);
  const page = view.pages[0];
  assert.equal(rendered, 0);
  assert.ok(!made.some((b) => b.tag === "canvas"));
  assert.match(page.note.textContent, /too large to draw here/);
  assert.ok(page.box.children.includes(page.note));
  view.zoom = 2; // a shape it can draw now (the fake's page shrinks when zoomed)
  page.size = null;
  page.note.remove = () => { page.box.children = page.box.children.filter((c) => c !== page.note); };
  await view.draw(0);
  assert.equal(rendered, 1);
  assert.equal(page.note, null);
  assert.ok(page.box.children.some((c) => c.tag === "canvas"));
  view.destroy();

  // A few bytes can hold tens of thousands of pages: past MAX_PDF_PAGES the view is refused, nothing laid out.
  view = new pane.PdfView({ pdfjs: fakePdfjs(pane.MAX_PDF_PAGES + 1), el: (tag, className) => new Box(tag, className),
                            bytes: new Uint8Array() });
  await assert.rejects(view.open(), (e) => e.code === "too_many" && /2,001 pages/.test(e.message));
  assert.equal(view.pagesBox.children.length, 0);
  view = new pane.PdfView({ pdfjs: fakePdfjs(pane.MAX_PDF_PAGES), el: (tag, className) => new Box(tag, className),
                            bytes: new Uint8Array() });
  await view.open();
  assert.equal(view.numPages, pane.MAX_PDF_PAGES);
  view.destroy();
}

const quiet = { ...pane.PDFJS_OPTIONS, wasmUrl: new URL("pdfjs/wasm/", STATIC).href, cMapUrl: new URL("pdfjs/cmaps/", STATIC).href,
                verbosity: 0 };
const open = (bytes) => pdfjs.getDocument({ ...quiet, data: bytes.slice() }).promise;
const join = (a, b) => { const out = new Uint8Array(a.length + b.length); out.set(a); out.set(b, a.length); return out; };
const annotationOps = async (page, mode) =>
  (await page.getOperatorList({ annotationMode: mode })).fnArray.filter((f) => f === pdfjs.OPS.beginAnnotation).length;

function stroke(x, y, tool = "pen") {
  const data = { v: 1, tool, color: tool === "pen" ? "blue" : "yellow", size: 2,
                 points: [[x, y, 0.5], [x + 40, y + 40, 0.75], [x + 80, y + 10, 0.25]] };
  return { rect: [x - 4, y - 4, x + 84, y + 44], inkList: [[x, y, x + 40, y + 40, x + 80, y + 10]],
           color: tool === "pen" ? [0, 0, 1] : [1, 0.9, 0], width: 2, highlighter: tool === "highlighter",
           appearance: `${tool === "pen" ? "0 0 1" : "1 0.9 0"} rg ${tool === "pen" ? "" : "/GS0 gs "}` +
             `${x} ${y} m ${x + 40} ${y + 40} l ${x + 80} ${y + 10} l ${x + 80} ${y + 14} l ${x + 40} ${y + 44} l h f`,
           data };
}

for (const name of ["table.pdf", "stream.pdf"]) {
  const original = fixture(name);
  const doc = await open(original);
  const file = await openPdf(original);
  assert.equal(file.form, name === "table.pdf" ? "table" : "stream", name);
  assert.ok(!file.encrypted);
  const refs = [];
  for (let i = 1; i <= doc.numPages; i += 1) {
    const { num, gen } = (await doc.getPage(i)).ref;
    refs.push(new Ref(num, gen));
  }
  assert.equal(refs.length, 3);
  assert.equal((await file.annots(refs[0])).length, 2, `${name}: the other app's square and the link`);
  assert.deepEqual(await file.ink(refs[0]), []);

  // §11 entry 1–2: an increment of the file's own form, after its bytes, that passes the server's check.
  const strokes = [[stroke(110, 110), stroke(70, 500, "highlighter")], [stroke(200, 300)], [stroke(100, 100)]];
  const increment = await appendIncrement(file, refs.map((ref, i) => ({ ref, add: strokes[i] })));
  assert.ok(checkIncrement(original.length, increment), `${name}: the increment passes the server's check`);
  assert.equal(/\/Type\s*\/XRef/.test(new TextDecoder("latin1").decode(increment)), name === "stream.pdf",
               `${name}: the cross-reference form kept`);
  const saved = join(original, increment);
  const again = await openPdf(saved);
  assert.equal(again.startxref > original.length, true);

  // §11 entry 4: /HimariInk read back as written, on every page (one turned, one cropped).
  for (const [i, ref] of refs.entries()) {
    const ink = await again.ink(ref);
    assert.deepEqual(ink.map((s) => s.data), strokes[i].map((s) => s.data), `${name} page ${i + 1}`);
    assert.ok(ink.every((s) => s.name.startsWith("himari-ink-") && s.name.length > 20));
  }
  const firstInk = await again.ink(refs[0]);
  assert.equal(firstInk[1].opacity, 0.35, "the highlighter's /CA");

  // PDF.js reads them as Ink annotations, the other app's kept.
  const reopened = await open(saved);
  const page = await reopened.getPage(1);
  const subtypes = (await page.getAnnotations()).map((a) => a.subtype);
  assert.deepEqual(subtypes, ["Square", "Link", "Ink", "Ink"], name);

  // §11 entry 3 (§4.4 B): HIMARI's ink marked deleted in PDF.js's annotation storage is left out of the
  // page's drawing; the other app's square is still drawn.
  assert.equal(await annotationOps(page, pdfjs.AnnotationMode.ENABLE), 3);
  for (const ink of firstInk) {
    reopened.annotationStorage.setValue(`pdfjs_internal_editor_himari_${ink.ref.key}`,
                                        { deleted: true, id: ink.ref.key, pageIndex: 0 });
  }
  assert.equal(await annotationOps(page, pdfjs.AnnotationMode.ENABLE_STORAGE), 1, `${name}: the square only`);
  assert.equal(await annotationOps(await reopened.getPage(2), pdfjs.AnnotationMode.ENABLE_STORAGE), 1);

  // An erase is a second increment after the first: the pen stroke leaves /Annots, the rest stay.
  const second = await appendIncrement(again, [{ ref: refs[0], drop: [firstInk[0].ref] }]);
  assert.ok(checkIncrement(saved.length, second));
  const twice = join(saved, second);
  const left = await (await openPdf(twice)).ink(refs[0]);
  assert.deepEqual(left.map((s) => s.name), [firstInk[1].name]);
  const subtypesLeft = (await (await (await open(twice)).getPage(1)).getAnnotations()).map((a) => a.subtype);
  assert.deepEqual(subtypesLeft, ["Square", "Link", "Ink"]);
  assert.deepEqual(twice.subarray(0, original.length), original, "the original bytes untouched");
  await doc.loadingTask.destroy();
  await reopened.loadingTask.destroy();
}

// The server's light check (§5.3 step 4) refuses what is not an increment after these bytes.
{
  const original = fixture("table.pdf");
  const increment = await appendIncrement(await openPdf(original), []);
  assert.ok(checkIncrement(original.length, increment));
  const ascii = (text) => new TextEncoder().encode(text);
  assert.ok(!checkIncrement(original.length, ascii("1 0 obj\n<<>>\nendobj\n")), "no startxref");
  assert.ok(!checkIncrement(original.length, ascii("startxref\n10\n%%EOF\n")), "a startxref into the original");
  assert.ok(!checkIncrement(original.length, ascii(`startxref\n${original.length + 999}\n%%EOF\n`)), "past the end");
  assert.ok(!checkIncrement(original.length, increment.subarray(0, increment.length - 7)), "no %%EOF");
  assert.ok(checkIncrement(original.length, join(increment, ascii("\r\n"))), "a line break after %%EOF");
}

// Files it does not write to: encrypted ones (viewed only, §3.3), and ones whose structure it cannot read.
{
  const encrypted = await openPdf(fixture("encrypted.pdf"));
  assert.ok(encrypted.encrypted);
  await assert.rejects(appendIncrement(encrypted, []), PdfReadError);
  assert.equal((await open(fixture("encrypted.pdf"))).numPages, 1, "viewed without a password");
  await assert.rejects(open(fixture("locked.pdf")), (error) => error.name === "PasswordException");
  const original = fixture("table.pdf");
  await assert.rejects(openPdf(original.subarray(0, original.length - 40)), PdfReadError, "no startxref");
  await assert.rejects(openPdf(new TextEncoder().encode("hello")), PdfReadError, "not a PDF");
  const text = new TextDecoder("latin1").decode(original); // the trailer's /Prev its own section
  const own = text.match(/startxref\s+(\d+)/)[1];
  const looped = Uint8Array.from(text.replace(/trailer\s*<</, (m) => `${m} /Prev ${own}`), (c) => c.charCodeAt(0));
  await assert.rejects(openPdf(looped), PdfReadError, "a /Prev loop");
}

// --- the ink (V3-P77 §4): the model -------------------------------------------------------------------------
const inkModule = await import(new URL("pdfink.js", STATIC));
const { Ink, newStroke, outline, specOf, strokeFromSaved, hits, TOOLS } = inkModule;
{
  const pen = newStroke(0, { tool: "pen", color: "blue", size: "bold" }, true);
  pen.points.push([100, 100, 0.2], [140, 130, 0.9], [180, 110, 0.5]);
  assert.equal(pen.size, TOOLS.pen.sizes.bold);
  assert.deepEqual(pen.rgb, TOOLS.pen.colors.blue);
  assert.ok(outline(pen).length > 4);
  const spec = specOf(pen);
  const [x0, y0, x1, y1] = spec.rect;
  assert.ok(pen.points.every(([x, y]) => x0 <= x && x <= x1 && y0 <= y && y <= y1));
  assert.match(spec.appearance, /^0\.1 0\.32 0\.85 rg\n[\d. ]+ m /);
  assert.ok(spec.appearance.endsWith(" h f") && !spec.appearance.includes("/GS0"));
  const marker = newStroke(0, { tool: "highlighter", color: "nope", size: "nope" }, false);
  marker.points.push([70, 520, 0.5], [380, 520, 0.5]);
  assert.equal(marker.size, 12);
  assert.ok(specOf(marker).highlighter && specOf(marker).appearance.includes("/GS0 gs"));
  // Pressure thins and thickens a pen's stroke; without it (a mouse) the width holds.
  const span = (stroke) => { const ys = outline(stroke).map(([, y]) => y); return Math.max(...ys) - Math.min(...ys); };
  const flat = (pressure) => { const s = newStroke(0, { tool: "pen", color: "black", size: "bold" }, pressure);
                               s.points.push([0, 0, 0.1], [50, 0, 1], [100, 0, 0.1]); return s; };
  assert.ok(span(flat(true)) !== span(flat(false)));
  // Hits: on it, near it, off it.
  assert.ok(hits(pen, 140, 130, 1) && hits(pen, 120, 118, 1) && !hits(pen, 300, 300, 5));

  // Round trip through the file: what is drawn is read back as it was (§11 entry 4, now with real strokes).
  const original = fixture("table.pdf");
  const file = await openPdf(original);
  const doc = await open(original);
  const ref = new Ref((await doc.getPage(1)).ref.num, 0);
  const saved = join(original, await appendIncrement(file, [{ ref, add: [specOf(pen), specOf(marker)] }]));
  const back = (await (await openPdf(saved)).ink(ref)).map((entry) => strokeFromSaved(0, entry));
  assert.deepEqual(back.map((s) => [s.tool, s.size, s.rgb, s.pressure, s.points]),
                   [pen, marker].map((s) => [s.tool, s.size, s.rgb.map((v) => Math.round(v * 1000) / 1000), s.pressure,
                                             s.points]));
  assert.ok(back.every((s) => s.saved && s.saved.name.startsWith("himari-ink-")));
  // Untrusted data falls back to /InkList: too many points, wrong types, a colour out of range.
  for (const data of [{ v: 1, tool: "pen", size: 2, rgb: [0, 0, 0], points: [[1, 2, "x"]] },
                      { v: 1, tool: "pen", size: 2, rgb: [0, 0, 9], points: [[1, 2, 0.5]] },
                      { v: 1, tool: "brush", size: 2, rgb: [0, 0, 0], points: [[1, 2, 0.5]] }, null]) {
    const fallback = strokeFromSaved(0, { ref, name: "himari-ink-x", data, inkList: [[10, 20, 30, 40]], width: 2,
                                          color: [1, 0, 0], opacity: 1 });
    assert.deepEqual(fallback.points, [[10, 20, 0.5], [30, 40, 0.5]]);
    assert.deepEqual(fallback.rgb, [1, 0, 0]);
  }
  await doc.loadingTask.destroy();

  // Undo and redo; an erase undone goes back where it was; what a save writes and drops.
  const ink = new Ink();
  const old = [strokeFromSaved(0, { ref: new Ref(40), name: "himari-ink-a", data: null, inkList: [[0, 0, 5, 5]], width: 1, color: [0, 0, 0] }),
               strokeFromSaved(0, { ref: new Ref(41), name: "himari-ink-b", data: null, inkList: [[9, 9, 8, 8]], width: 1, color: [0, 0, 0] })];
  ink.load(0, old);
  assert.ok(!ink.dirty);
  ink.add(pen);
  ink.erase(0, [old[0]]);
  assert.deepEqual(ink.on(0), [old[1], pen]);
  assert.deepEqual(ink.changes().map((c) => [c.page, c.add, c.drop.map((r) => r.num)]), [[0, [pen], [40]]]);
  assert.equal(ink.undo(), 0);
  assert.deepEqual(ink.on(0), [old[0], old[1], pen]);
  ink.undo();
  assert.ok(!ink.dirty && ink.undone.length === 2);
  ink.redo();
  assert.deepEqual(ink.on(0), [old[0], old[1], pen]);
  ink.add(marker); // a new stroke ends what could be redone
  assert.equal(ink.undone.length, 0);
  // §5.5: carried to another version — new strokes kept (not past its pages), erased ones erased again.
  ink.erase(0, [old[1]]);
  const carried = ink.carry();
  assert.deepEqual(carried.erasePages, [0]);
  const next = new Ink();
  assert.equal(next.put({ ...carried, add: [...carried.add, { ...pen, page: 7 }] }, 3), 1, "one left: no page 8");
  next.load(0, old.map((s) => ({ ...s })));
  assert.deepEqual(next.on(0).map((s) => s.saved ? s.saved.name : "new"), ["himari-ink-a", "new", "new"]);
}

// --- the ink (§4.1): the view's input, against the bundled PDF.js with a fake DOM ----------------------------
{
  class Node {
    constructor(tag, className) {
      Object.assign(this, { tag, className, children: [], parentNode: null, dataset: {}, style: {}, attributes: {},
                            listeners: {}, scrollTop: 0, scrollLeft: 0, clientTop: 0, clientWidth: 624,
                            clientHeight: 800, width: 0, height: 0, captured: null });
      this.classList = { toggle: (name, on) => { this.attributes[`class:${name}`] = on; } };
    }
    append(...nodes) { for (const n of nodes) { n.parentNode = this; this.children.push(n); } }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    getBoundingClientRect() { return { top: 0, left: 0 }; }
    setPointerCapture(id) { this.captured = id; }
    releasePointerCapture() { this.captured = null; }
    focus() {}
    getContext() {
      return { clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() { this.fills = (this.fills || 0) + 1; } };
    }
  }
  const fire = (node, type, event) => {
    let prevented = false;
    for (const fn of node.listeners[type] || []) fn({ preventDefault: () => { prevented = true; }, ...event });
    return prevented;
  };
  let observed = null;
  globalThis.IntersectionObserver = class { constructor(fn) { observed = fn; } observe() {} disconnect() {} };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  const renders = [];
  const wrapped = {
    AnnotationMode: pdfjs.AnnotationMode,
    getDocument: (options) => {
      const task = pdfjs.getDocument({ ...options, ...quiet, data: options.data });
      return {
        promise: task.promise.then((doc) => ({
          numPages: doc.numPages, annotationStorage: doc.annotationStorage,
          getPage: async (n) => {
            const page = await doc.getPage(n);
            return Object.assign(Object.create(page), { render: (params) => {
              renders.push(params);
              return { promise: Promise.resolve(), cancel() {} };
            } });
          },
        })),
        destroy: () => task.destroy(),
      };
    },
  };
  async function viewOf(bytes) {
    const notices = [];
    const view = new pane.PdfView({ pdfjs: wrapped, el: (tag, className) => new Node(tag, className), bytes,
                                    onNotice: (text) => notices.push(text) });
    await view.open();
    observed([{ target: view.pages[0].box, isIntersecting: true }]);
    for (let i = 0; i < 50 && !view.pages[0].canvas; i++) await new Promise((r) => setTimeout(r, 5));
    assert.ok(view.pages[0].canvas, "page 1 drawn");
    return { view, notices };
  }
  const box = (view) => view.pages[0].box;
  const pointer = (view, type, extra) => fire(view.pagesBox, type, { pointerId: 1, pointerType: "pen", button: 0,
                                                                     target: box(view), pressure: 0.6, ...extra });

  // A PDF with HIMARI ink already: the page drawn without it (its refs marked deleted), the layer holding it.
  const original = fixture("table.pdf");
  const doc = await open(original);
  const ref = new Ref((await doc.getPage(1)).ref.num, 0);
  await doc.loadingTask.destroy();
  const first = newStroke(0, { tool: "pen", color: "black", size: "fine" }, true);
  first.points.push([100, 600, 0.5], [200, 650, 0.5]);
  const withInk = join(original, await appendIncrement(await openPdf(original), [{ ref, add: [specOf(first)] }]));
  const savedRef = (await (await openPdf(withInk)).ink(ref))[0].ref;
  const { view } = await viewOf(withInk);
  assert.ok(view.writable && !view.readOnly);
  assert.equal(renders.at(-1).annotationMode, pdfjs.AnnotationMode.ENABLE_STORAGE);
  assert.ok(view.doc.annotationStorage.has(`pdfjs_internal_editor_himari_${savedRef.key}`));
  assert.equal(view.ink.on(0).length, 1);
  assert.ok(view.pages[0].inkCanvas, "the saved stroke on the ink layer");

  // No tool: a pen does nothing (scrolls). A finger never draws; a mouse's other buttons do not.
  assert.ok(!pointer(view, "pointerdown", { clientX: 50, clientY: 50 }));
  view.setTool({ tool: "pen", color: "red", size: "fine" });
  assert.ok(!pointer(view, "pointerdown", { pointerType: "touch", clientX: 50, clientY: 50 }));
  assert.ok(!pointer(view, "pointerdown", { pointerType: "mouse", button: 2, clientX: 50, clientY: 50 }));
  assert.equal(view.active, null);
  // The pen: down, moved (its coalesced points), up — one stroke in PDF space.
  assert.ok(pointer(view, "pointerdown", { clientX: 100, clientY: 100 }));
  assert.equal(view.pagesBox.captured, 1);
  pointer(view, "pointermove", { clientX: 130, clientY: 120,
                                 getCoalescedEvents: () => [{ clientX: 115, clientY: 110, pressure: 0.4 },
                                                            { clientX: 130, clientY: 120, pressure: 0.8 }],
                                 getPredictedEvents: () => [{ clientX: 140, clientY: 125, pressure: 0.8 }] });
  pointer(view, "pointerup", { clientX: 130, clientY: 120 });
  const drawn = view.ink.on(0).at(-1);
  assert.equal(drawn.points.length, 3, "down and the two coalesced points; never the predicted one");
  const viewport = view.pages[0].pdfPage.getViewport({ scale: view.scaleOf(view.pages[0]) });
  assert.deepEqual(drawn.points[0].slice(0, 2), viewport.convertToPdfPoint(100, 100));
  assert.deepEqual(drawn.points.map((p) => p[2]), [0.6, 0.4, 0.8]);
  assert.equal(drawn.color, "red");
  assert.ok(view.ink.dirty && !view.pages[0].liveCanvas);
  // A mouse draws at an even pressure.
  pointer(view, "pointerdown", { pointerType: "mouse", pointerId: 2, clientX: 300, clientY: 300 });
  pointer(view, "pointermove", { pointerType: "mouse", pointerId: 2, clientX: 320, clientY: 300 });
  pointer(view, "pointerup", { pointerType: "mouse", pointerId: 2 });
  assert.deepEqual(view.ink.on(0).at(-1).points.map((p) => p[2]), [0.5, 0.5]);
  // Cut short by the system: kept from two points, dropped with fewer.
  const before = view.ink.on(0).length;
  pointer(view, "pointerdown", { pointerId: 3, clientX: 400, clientY: 400 });
  pointer(view, "pointermove", { pointerId: 3, clientX: 410, clientY: 410 });
  pointer(view, "pointercancel", { pointerId: 3 });
  pointer(view, "lostpointercapture", { pointerId: 3 });
  assert.equal(view.ink.on(0).length, before + 1);
  pointer(view, "pointerdown", { pointerId: 4, clientX: 400, clientY: 500 });
  pointer(view, "pointercancel", { pointerId: 4 });
  assert.equal(view.ink.on(0).length, before + 1);
  // §4.1 A: a stylus touch is kept from scrolling while a tool is picked; a finger's is not, nor is a pencil
  // and a palm a pinch.
  const touch = (type) => ({ clientX: 10, clientY: 10, touchType: type });
  assert.ok(fire(view.element, "touchstart", { changedTouches: [touch("stylus")], touches: [touch("stylus")] }));
  assert.ok(!fire(view.element, "touchstart", { changedTouches: [touch("direct")], touches: [touch("direct")] }));
  fire(view.element, "touchstart", { changedTouches: [touch("direct")], touches: [touch("stylus"), touch("direct")] });
  assert.equal(view.pinch, null);
  // The eraser takes whole strokes, the saved one too; undone, they come back.
  view.setTool({ tool: "eraser" });
  const [sx, sy] = viewport.convertToViewportPoint(150, 625); // on the saved stroke
  pointer(view, "pointerdown", { pointerId: 5, clientX: sx, clientY: sy });
  pointer(view, "pointerup", { pointerId: 5 });
  assert.ok(!view.ink.on(0).some((s) => s.saved));
  assert.deepEqual(view.ink.changes()[0].drop.map((r) => r.key), [savedRef.key]);
  view.undo();
  assert.ok(view.ink.on(0).some((s) => s.saved));
  view.redo();
  // Saved: an increment the server would take, read back — the new strokes saved, the erased one gone.
  const increment = await view.increment();
  assert.ok(checkIncrement(withInk.length, increment));
  await view.saved(increment);
  assert.ok(!view.ink.dirty && view.ink.on(0).every((s) => s.saved));
  assert.equal(view.ink.on(0).length, 3);
  assert.equal(view.ink.done.length, 0, "undo starts again after a save");
  assert.equal((await view.increment()), null);
  view.destroy();

  // Encrypted: viewed only, its annotations drawn by PDF.js, no pen.
  const locked = await viewOf(fixture("encrypted.pdf"));
  assert.ok(!locked.view.writable && /encrypted/.test(locked.view.readOnly));
  assert.equal(renders.at(-1).annotationMode, pdfjs.AnnotationMode.ENABLE);
  locked.view.setTool({ tool: "pen", color: "black", size: "fine" });
  assert.equal(locked.view.tool, null);
  locked.view.destroy();
}

console.log("pdf smoke ok");
