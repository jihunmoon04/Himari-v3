/* The PDF view's settings and HIMARI's own PDF writing (V3-P77 §3.4, §5, the stage-2 entry
 * conditions of §11), against the bundled PDF.js and tests/fixtures/pdf: no browser, packages
 * or live service. Usage: node scripts/pdf_smoke.mjs */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const STATIC = new URL("../src/himari/interfaces/web/static/", import.meta.url);
const FIXTURES = new URL("../tests/fixtures/pdf/", import.meta.url);
const fixture = (name) => new Uint8Array(readFileSync(new URL(name, FIXTURES)));

const pane = await import(new URL("pdfpane.js", STATIC));
const { openPdf, extendPdf, appendIncrement, checkIncrement, PdfReadError, PdfString, Ref } =
  await import(new URL("pdfdoc.js", STATIC));
const appendBytes = async (...args) => (await appendIncrement(...args)).bytes; // V3-P82: {bytes, added}
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
    prepend(node) { node.parent = this; this.children.unshift(node); }
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
  const increment = await appendBytes(file, refs.map((ref, i) => ({ ref, add: strokes[i] })));
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
  const second = await appendBytes(again, [{ ref: refs[0], drop: [firstInk[0].ref] }]);
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
  const increment = await appendBytes(await openPdf(original), []);
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

// --- saving in place (V3-P82 §3.3, §3.6) -------------------------------------------------------------------
const latin1 = (bytes) => new TextDecoder("latin1").decode(bytes);
const firstPage = async (bytes) => {
  const doc = await open(bytes);
  const { num, gen } = (await doc.getPage(1)).ref;
  await doc.loadingTask.destroy();
  return new Ref(num, gen);
};

// A named stroke's object, told back; /HimariInk a Flate stream (v2); an erase undone after a save puts the
// same object back in /Annots — never twice, nothing written again. PDF.js reads each step.
for (const name of ["table.pdf", "stream.pdf"]) {
  const original = fixture(name);
  const ref = await firstPage(original);
  const pen = { ...stroke(110, 110), name: "himari-ink-0123456789abcdef01234567" };
  const made = await appendIncrement(await openPdf(original), [{ ref, add: [pen] }]);
  const annot = made.added.get(pen.name);
  assert.ok(annot instanceof Ref && made.added.size === 1, `${name}: the stroke's object told`);
  assert.match(latin1(made.bytes), /\/HimariInk \d+ 0 R/, `${name}: v2, a stream`);
  const one = join(original, made.bytes);
  assert.deepEqual((await (await openPdf(one)).ink(ref)).map((e) => [e.name, e.data, e.ref.key]),
                   [[pen.name, pen.data, annot.key]], `${name}: read back as written, by its name`);
  const two = join(one, await appendBytes(await openPdf(one), [{ ref, drop: [annot] }]));
  assert.deepEqual(await (await openPdf(two)).ink(ref), []);
  const back = await appendIncrement(await openPdf(two), [{ ref, restore: [annot, annot] }]);
  assert.equal(back.added.size, 0, "nothing written again");
  assert.ok(checkIncrement(two.length, back.bytes));
  const three = join(two, back.bytes);
  assert.deepEqual((await (await openPdf(three)).ink(ref)).map((e) => e.ref.key), [annot.key], `${name}: put back once`);
  const held = join(three, await appendBytes(await openPdf(three), [{ ref, restore: [annot] }]));
  assert.equal((await (await openPdf(held)).ink(ref)).length, 1, `${name}: in /Annots already: not twice`);
  const doc = await open(held);
  assert.deepEqual((await (await doc.getPage(1)).getAnnotations()).map((a) => a.subtype), ["Square", "Link", "Ink"]);
  await doc.loadingTask.destroy();
}

// /HimariInk: a string (v1, V3-P77) read too; a stream inflating past 4 MiB is not read (the stroke drawn from
// its /InkList), nor a stream of another kind.
{
  const file = await openPdf(fixture("table.pdf"));
  const data = { v: 1, tool: "pen", points: [[1, 2, 0.5]] };
  assert.equal(await file.inkData(PdfString.utf8(JSON.stringify(data))), JSON.stringify(data));
  await assert.rejects(file.inkData(null), PdfReadError);
  const original = fixture("table.pdf");
  const ref = await firstPage(original);
  const huge = { ...stroke(50, 50), name: "himari-ink-huge", data: { ...stroke(50, 50).data, pad: "x".repeat(5 << 20) } };
  const saved = join(original, await appendBytes(await openPdf(original), [{ ref, add: [huge] }]));
  const [entry] = await (await openPdf(saved)).ink(ref);
  assert.equal(entry.data, null, "past 4 MiB inflated: not read");
  assert.equal(entry.inkList[0].length, 6, "drawn from its /InkList");
}

// §3.6 (review r3 B2): a save cut short at the end — a piece under 1 KiB or over it — is read past to the last
// save that reads, ``cut`` saying where to cut back to; the size stays the file's. Bytes another producer left
// after %%EOF are never cut. Cut back, it reads as before.
for (const name of ["table.pdf", "stream.pdf"]) {
  const original = fixture(name);
  const ref = await firstPage(original);
  const one = join(original, await appendBytes(await openPdf(original), [{ ref, add: [stroke(110, 110)] }]));
  assert.equal((await openPdf(one)).cut, null, `${name}: a whole file has nothing to cut`);
  const many = Array.from({ length: 40 }, (_, i) => ({ ...stroke(20 + i, 30 + i), name: `himari-ink-cut${i}` }));
  const next = await appendBytes(await openPdf(one), [{ ref, add: many }]);
  assert.ok(next.length > 4096);
  for (const piece of [300, next.length - 20, next.length - 10]) { // the last: cut within its startxref N
    const cut = join(one, next.subarray(0, piece));
    const file = await openPdf(cut);
    assert.equal(file.cut, one.length, `${name}: cut back to the save before (${piece} bytes left)`);
    assert.equal(file.size, cut.length, "its size is the file's");
    assert.equal((await file.ink(ref)).length, 1, "the saves before read");
    const mended = await openPdf(cut.subarray(0, file.cut));
    assert.equal(mended.cut, null);
    assert.equal((await mended.ink(ref)).length, 1);
  }
  for (const tail of ["\n\n\n", "%% another producer's comment\n", "\0\0\0\0"]) {
    const file = await openPdf(join(one, new TextEncoder().encode(tail)));
    assert.equal(file.cut, null, `${name}: ${JSON.stringify(tail)} not cut`);
  }
}

// PR #119 review B1: an end that does not read is never passed over for an older one — another app's update
// (here its startxref a few bytes off, as PDF.js forgives) is viewed only, never cut. A file whose startxref
// has no %%EOF after it reads as before V3-P82.
for (const name of ["table.pdf", "stream.pdf"]) {
  const original = fixture(name);
  const ref = await firstPage(original);
  const update = await appendBytes(await openPdf(original), [{ ref, add: [stroke(110, 110)] }]);
  const tail = latin1(update.subarray(update.length - 40));
  const offset = Number(/startxref\s+(\d+)\s+%%EOF\s*$/.exec(tail)[1]);
  const at = update.length - 40 + tail.lastIndexOf("startxref");
  const theirs = join(original, join(update.subarray(0, at), new TextEncoder().encode(`startxref\n${offset + 3}\n%%EOF\n`)));
  await assert.rejects(openPdf(theirs), PdfReadError, `${name}: viewed only, not cut back`);
  const noEof = join(original, update.subarray(0, update.length - 6)); // "%%EOF\n" gone
  assert.equal((await (await openPdf(noEof)).ink(ref)).length, 1, `${name}: read from its last startxref`);
}

// V3-P82 §3.4: a save read on top of the file it was made on — only its own section read — is the file read
// whole; a section that is not after it is refused.
for (const name of ["table.pdf", "stream.pdf"]) {
  const original = fixture(name);
  const ref = await firstPage(original);
  const file = await openPdf(original);
  const one = join(original, await appendBytes(file, [{ ref, add: [stroke(110, 110), stroke(70, 500, "highlighter")] }]));
  const extended = await extendPdf(file, one);
  const whole = await openPdf(one);
  assert.deepEqual([...extended.entries].sort((a, b) => a[0] - b[0]), [...whole.entries].sort((a, b) => a[0] - b[0]),
                   `${name}: the same objects`);
  assert.deepEqual((await extended.ink(ref)).map((e) => e.data), (await whole.ink(ref)).map((e) => e.data));
  assert.equal(extended.startxref, whole.startxref);
  assert.equal(extended.nextNumber, whole.nextNumber);
  const two = join(one, await appendBytes(extended, [{ ref, add: [stroke(200, 300)] }]));
  assert.equal((await (await extendPdf(extended, two)).ink(ref)).length, 3, `${name}: and the next on it`);
  await assert.rejects(extendPdf(file, two), PdfReadError, `${name}: not after this file`);
  await assert.rejects(extendPdf(file, original), PdfReadError, `${name}: nothing added`);
}

// §3.6 (review r3 B1): a PDF saved in place more than 1,000 times (V3-P77's limit) is read and saved on.
{
  const original = fixture("table.pdf");
  const text = latin1(original);
  const trailer = /trailer\s*<<([\s\S]*?)>>\s*startxref\s+(\d+)/.exec(text);
  let prev = Number(trailer[2]);
  const parts = [original];
  let size = original.length;
  for (let i = 0; i < 1200; i += 1) {
    const section = `xref\n0 1\n0000000000 65535 f\r\ntrailer\n<<${trailer[1]} /Prev ${prev}>>\nstartxref\n${size}\n%%EOF\n`;
    const bytes = new TextEncoder().encode(section);
    prev = size;
    parts.push(bytes);
    size += bytes.length;
  }
  const saves = new Uint8Array(size);
  let at = 0;
  for (const part of parts) {
    saves.set(part, at);
    at += part.length;
  }
  const started = Date.now();
  const file = await openPdf(saves);
  const ref = await firstPage(saves);
  const saved = join(saves, await appendBytes(file, [{ ref, add: [stroke(110, 110)] }]));
  assert.equal((await (await openPdf(saved)).ink(ref)).length, 1);
  assert.ok(Date.now() - started < 10_000, "read in good time");
  const doc = await open(saved);
  assert.equal((await (await doc.getPage(1)).getAnnotations()).filter((a) => a.subtype === "Ink").length, 1,
               "PDF.js reads it too");
  await doc.loadingTask.destroy();
}

// --- the ink (V3-P77 §4): the model -------------------------------------------------------------------------
const inkModule = await import(new URL("pdfink.js", STATIC));
const { Ink, newStroke, outline, specOf, strokeFromSaved, hits, TOOLS, DEFAULT_TUNING, currentTuning, setTuning } = inkModule;
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
  // §4.6: the settings tried in the ink lab — outlines made again, the defaults giving the same as before;
  // out of range, the nearest; not a number, the default.
  const tunedBefore = outline(pen).map((p) => [...p]);
  setTuning({ thinning: 0 });
  assert.notDeepEqual(outline(pen), tunedBefore);
  setTuning(DEFAULT_TUNING);
  assert.deepEqual(outline(pen), tunedBefore);
  assert.deepEqual(setTuning({ thinning: 5, smoothing: "x", streamline: -1 }),
                   { thinning: 0.95, smoothing: DEFAULT_TUNING.smoothing, streamline: 0 });
  setTuning(DEFAULT_TUNING);
  assert.deepEqual(currentTuning(), { ...DEFAULT_TUNING });
  // Hits: on it, near it, off it.
  assert.ok(hits(pen, 140, 130, 1) && hits(pen, 120, 118, 1) && !hits(pen, 300, 300, 5));

  // Round trip through the file: what is drawn is read back as it was (§11 entry 4, now with real strokes).
  const original = fixture("table.pdf");
  const file = await openPdf(original);
  const doc = await open(original);
  const ref = new Ref((await doc.getPage(1)).ref.num, 0);
  const saved = join(original, await appendBytes(file, [{ ref, add: [specOf(pen), specOf(marker)] }]));
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
  assert.deepEqual(ink.changes().map((c) => [c.page, c.add, c.restore, c.drop.map((d) => [d.name, d.ref.num])]),
                   [[0, [pen], [], [["himari-ink-a", 40]]]]);
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

  // V3-P82 §3.1, §3.3–§3.5: strokes are named when drawn; a save marks what it wrote as saved where it is,
  // and what changed meanwhile stays a change; undo goes on across saves — a saved stroke's add undone is
  // dropped, its erase undone is put back (its object, not written again); carried, a stroke the other
  // version holds already (by name) is left out.
  const book = new Ink();
  book.load(0, old.map((s) => ({ ...s, cached: null })));
  const one = newStroke(0, { tool: "pen", color: "black", size: "fine" }, true);
  one.points.push([1, 1, 0.5], [2, 2, 0.5]);
  assert.match(one.name, /^himari-ink-[0-9a-f]{24}$/);
  assert.notEqual(one.name, newStroke(0, { tool: "pen" }, true).name);
  assert.equal(specOf(one).name, one.name);
  book.add(one);
  const snapshot = book.changes();
  const two = newStroke(0, { tool: "pen" }, true);
  two.points.push([3, 3, 0.5]);
  book.add(two); // drawn while the save is on its way
  book.committed(snapshot, new Map([[one.name, new Ref(90)]]));
  assert.deepEqual(one.saved, { ref: new Ref(90), name: one.name });
  assert.deepEqual(book.changes().map((c) => [c.add, c.restore, c.drop]), [[[two], [], []]], "only the newer");
  book.committed(book.changes(), new Map([[two.name, new Ref(93)]]));
  assert.ok(!book.dirty);
  book.undo(); // two's add, after its save: dropped next
  assert.deepEqual(book.changes()[0].drop.map((d) => d.ref.num), [93]);
  book.redo();
  assert.ok(!book.dirty);
  book.erase(0, [one]);
  book.committed(book.changes(), new Map());
  assert.ok(!book.dirty && !book.inFile.get(0).has(one.name));
  book.undo(); // the erase, after its save: put back, the same object
  assert.deepEqual(book.changes().map((c) => [c.add, c.restore.map((r) => r.ref.num), c.drop]), [[[], [90], []]]);
  const away = book.carry();
  assert.deepEqual(away.add.map((s) => [s.name, s.saved]), [[one.name, null]], "carried whole: put back elsewhere");
  // Saved, though its answer was lost: the version read again holds it; carried there, it is not added twice.
  const lost = new Ink();
  lost.put({ add: [{ ...two, saved: null }], erase: new Set(), erasePages: [] }, 3);
  lost.load(0, [strokeFromSaved(0, { ref: new Ref(93), name: two.name, data: specOf(two).data, inkList: [] })]);
  assert.deepEqual(lost.on(0).map((s) => [s.name, Boolean(s.saved)]), [[two.name, true]]);
  assert.ok(!lost.dirty);
  // PR #119 review B2: put on pages read already (kept ink put back once the PDF shows) — a stroke the page
  // holds by name is not added again, and the carried erasures are made now.
  const shown = new Ink();
  const held = strokeFromSaved(0, { ref: new Ref(93), name: two.name, data: specOf(two).data, inkList: [] });
  const other = strokeFromSaved(0, { ref: new Ref(90), name: one.name, data: specOf(one).data, inkList: [] });
  shown.load(0, [held, other]);
  const fresh = newStroke(0, { tool: "pen" }, true);
  fresh.points.push([5, 5, 0.5]);
  shown.put({ add: [{ ...two, saved: null }, { ...fresh }], erase: new Set([one.name]), erasePages: [0] }, 3);
  assert.deepEqual(shown.on(0).map((s) => s.name), [two.name, fresh.name]);
  assert.deepEqual(shown.changes().map((c) => [c.add.map((s) => s.name), c.drop.map((d) => d.name)]),
                   [[[fresh.name], [one.name]]]);
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
    append(...nodes) { for (const n of nodes) { n.remove(); n.parentNode = this; this.children.push(n); } }
    prepend(node) { node.remove(); node.parentNode = this; this.children.unshift(node); }
    after(node) {
      node.remove();
      node.parentNode = this.parentNode;
      this.parentNode.children.splice(this.parentNode.children.indexOf(this) + 1, 0, node);
    }
    get lastChild() { return this.children.at(-1) || null; }
    contains(node) { for (let at = node; at; at = at.parentNode) if (at === this) return true; return false; }
    remove() {
      if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    getBoundingClientRect() { // a page as large as its box; the view 624 × 800 at the top left
      const width = parseFloat(this.style.width) || this.clientWidth;
      const height = parseFloat(this.style.height) || this.clientHeight;
      const top = this.top || 0;
      return { top, left: 0, width, height, right: width, bottom: top + height };
    }
    setPointerCapture(id) { this.captured = id; }
    releasePointerCapture() { this.captured = null; }
    focus() {}
    getContext() { // one per canvas, counting what was done with it
      this.context ||= { fills: 0, clears: [], transforms: [], clearRect(...r) { this.clears.push(r); },
                         setTransform(...m) { this.transforms.push(m); }, beginPath() {}, moveTo() {}, lineTo() {},
                         closePath() {}, fill() { this.fills += 1; } };
      return this.context;
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
  const withInk = join(original, await appendBytes(await openPdf(original), [{ ref, add: [specOf(first)] }]));
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
  assert.ok(view.ink.dirty);
  // The live layer: over the part of the page in view (scrolled 300 px down), painted once a frame however many
  // moves came, emptied where it was drawn on when the pen lifts; the stroke then added on top of the page's
  // ink, the page not painted again.
  const tick = () => new Promise((r) => setTimeout(r, 5));
  const inkContext = view.pages[0].inkCanvas.getContext();
  const [inkClears, inkFills] = [inkContext.clears.length, inkContext.fills];
  box(view).top = -300;
  pointer(view, "pointerdown", { pointerId: 8, clientX: 200, clientY: 200 });
  for (const x of [210, 220, 230]) pointer(view, "pointermove", { pointerId: 8, clientX: x, clientY: 210 });
  await tick();
  const live = view.live.canvas;
  assert.equal(live.parentNode, box(view));
  assert.equal(live.className, "pdf-live");
  assert.deepEqual([live.style.left, live.style.top, live.style.width, live.style.height],
                   ["0px", "300px", box(view).style.width, `${parseFloat(box(view).style.height) - 300}px`]);
  assert.deepEqual([live.width, live.height], [parseFloat(box(view).style.width), parseFloat(box(view).style.height) - 300]);
  const liveContext = live.getContext();
  assert.equal(liveContext.fills, 1, "one frame for the down and three moves");
  assert.deepEqual(box(view).children.map((c) => c.className), ["pdf-canvas", "pdf-ink", "pdf-live"]);
  const transform = view.pages[0].pdfPage.getViewport({ scale: view.scaleOf(view.pages[0]) }).transform;
  assert.deepEqual(liveContext.transforms.at(-2), [...transform.slice(0, 5), transform[5] - 300]);
  pointer(view, "pointermove", { pointerId: 8, clientX: 260, clientY: 240 });
  await tick();
  assert.equal(liveContext.fills, 2);
  assert.equal(liveContext.clears.length, 1, "where the frame before drew, emptied");
  // The view scrolled under the pen (a palm, a wheel): the layer follows what is in view, emptied first.
  box(view).top = -350;
  pointer(view, "pointermove", { pointerId: 8, clientX: 270, clientY: 240 });
  await tick();
  assert.deepEqual([live.style.top, live.height], ["350px", parseFloat(box(view).style.height) - 350]); // emptied
  assert.equal(liveContext.fills, 3);
  assert.deepEqual(liveContext.transforms.at(-2).slice(4), [transform[4], transform[5] - 350]);
  pointer(view, "pointerup", { pointerId: 8 });
  assert.equal(liveContext.clears.length, 2);
  const [, , w, h] = liveContext.clears.at(-1);
  assert.ok(w > 0 && w < live.width && h > 0 && h < live.height, "only where it was drawn on");
  assert.deepEqual([inkContext.clears.length, inkContext.fills], [inkClears, inkFills + 1]);
  await tick();
  assert.equal(liveContext.fills, 3, "no frame after the pen lifted");
  box(view).top = 0;
  view.undo();
  // A zoom drawn again: the ink's canvas is made the drawing's size again (it kept the old one).
  view.zoomBy(2, 0, 0);
  for (let i = 0; i < 100 && view.pages[0].canvas.width < 1000; i++) await new Promise((r) => setTimeout(r, 10));
  assert.ok(view.pages[0].canvas.width >= 1000, "drawn again at twice the size");
  assert.deepEqual([view.pages[0].inkCanvas.width, view.pages[0].inkCanvas.height],
                   [view.pages[0].canvas.width, view.pages[0].canvas.height]);
  // ... and the new drawing goes under the ink and the live layer, never over them (review B1).
  assert.deepEqual(box(view).children.map((c) => c.className), ["pdf-canvas", "pdf-ink", "pdf-live"]);
  view.zoomBy(0.5, 0, 0);
  for (let i = 0; i < 100 && view.pages[0].canvas.width >= 1000; i++) await new Promise((r) => setTimeout(r, 10));
  // A page out of view dropped: its live layer goes with it; back in view, drawn again.
  observed([{ target: box(view), isIntersecting: false }]);
  assert.ok(view.live === null && !view.pages[0].inkCanvas && box(view).children.length === 0);
  observed([{ target: box(view), isIntersecting: true }]);
  for (let i = 0; i < 50 && !view.pages[0].inkCanvas; i++) await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(box(view).children.map((c) => c.className), ["pdf-canvas", "pdf-ink"]);
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
  assert.deepEqual(view.ink.changes()[0].drop.map((d) => d.ref.key), [savedRef.key]);
  view.undo();
  assert.ok(view.ink.on(0).some((s) => s.saved));
  view.redo();
  // V3-P82 §3.4: while another version is read in its place (frozen), nothing is drawn; a save never stops it.
  view.setTool({ tool: "pen", color: "black", size: "fine" });
  view.frozen = true;
  const counted = view.ink.on(0).length;
  assert.ok(!pointer(view, "pointerdown", { pointerId: 6, clientX: 50, clientY: 700 }));
  assert.equal(view.active, null);
  view.frozen = false;
  // PR #104 review P2: the page moved under the pen (a word shown above the pages): its points follow the page.
  pointer(view, "pointerdown", { pointerId: 7, clientX: 50, clientY: 700 });
  box(view).top = 40;
  pointer(view, "pointermove", { pointerId: 7, clientX: 50, clientY: 740 });
  pointer(view, "pointerup", { pointerId: 7 });
  box(view).top = 0;
  const moved = view.ink.on(0).at(-1).points;
  assert.equal(view.ink.on(0).length, counted + 1);
  assert.deepEqual(moved[1].slice(0, 2), moved[0].slice(0, 2), "the same spot on the page");
  view.undo();
  view.setTool({ tool: "eraser" });
  // Saved: an increment the server would take — the new strokes saved where they are (named, never read back),
  // the erased one gone; a stroke drawn while it was on its way stays unsaved; undo goes on (V3-P82 §3.4–§3.5).
  const made = await view.increment();
  assert.ok(checkIncrement(withInk.length, made.bytes));
  view.setTool({ tool: "pen", color: "black", size: "fine" });
  const doneBefore = view.ink.done.length + 1;
  pointer(view, "pointerdown", { pointerId: 10, clientX: 60, clientY: 60 });
  pointer(view, "pointerup", { pointerId: 10 });
  await view.saved(made);
  assert.equal(view.file.size, withInk.length + made.bytes.length);
  assert.deepEqual(Array.from(view.bytes.subarray(0, withInk.length)), Array.from(withInk), "the file's bytes kept");
  const reread = await openPdf(view.bytes);
  assert.deepEqual((await reread.ink(ref)).map((e) => e.name).sort(),
                   view.ink.on(0).filter((s) => s.saved).map((s) => s.name).sort(), "what it holds, named so");
  assert.deepEqual(view.ink.changes().map((c) => c.add.length), [1], "the one drawn meanwhile waits");
  assert.equal(view.ink.done.length, doneBefore, "undo goes on after a save");
  view.undo(); // that one
  assert.equal(await view.increment(), null);
  // A second save is added after the first, in the same buffer (no copy of the whole each time).
  pointer(view, "pointerdown", { pointerId: 11, clientX: 70, clientY: 70 });
  pointer(view, "pointermove", { pointerId: 11, clientX: 90, clientY: 80 });
  pointer(view, "pointerup", { pointerId: 11 });
  const buffer = view.buffer;
  const next = await view.increment();
  await view.saved(next);
  assert.equal(view.buffer, buffer);
  assert.ok(!view.ink.dirty);
  view.destroy();

  // §4.6, the ink lab: off unless asked for; ?ink-lab turns it on in this browser, its numbers the last
  // stroke's, a setting tried kept there and the ink painted again with it; ?ink-lab=0 off, the settings gone.
  const lab = await import(new URL("inklab.js", STATIC));
  const stored = new Map();
  globalThis.localStorage = { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, String(v)),
                              removeItem: (k) => stored.delete(k) };
  assert.ok(!lab.labOn());
  lab.rememberLab("?x=1");
  assert.ok(!lab.labOn());
  lab.rememberLab("?ink-lab");
  assert.ok(lab.labOn());
  const tried = (await viewOf(fixture("table.pdf"))).view;
  assert.ok(tried.lab && tried.element.children.includes(tried.lab.element));
  tried.setTool({ tool: "pen", color: "black", size: "fine" });
  pointer(tried, "pointerdown", { pointerId: 9, clientX: 100, clientY: 100, timeStamp: 0 });
  pointer(tried, "pointermove", { pointerId: 9, clientX: 120, clientY: 100,
                                  getCoalescedEvents: () => [{ clientX: 110, clientY: 100, pressure: 0.3, timeStamp: 8 },
                                                             { clientX: 120, clientY: 100, pressure: 0.7, timeStamp: 16 }] });
  await tick();
  pointer(tried, "pointerup", { pointerId: 9 });
  const numbers = tried.lab.numbers.textContent;
  assert.match(numbers, /^pen · 63 moves\/s · 125 points\/s\n/); // the down's point at 0 ms counted too
  assert.match(numbers, /\ninput to paint: [\d.]+ ms avg · [\d.]+ max · \d of 1 frames over 40 ms\n/);
  assert.match(numbers, /per move: 2\.0 coalesced, 0\.0 predicted/);
  assert.match(numbers, /pressure: 0\.30–0\.70 · 3 points · live \d+×\d+$/);
  // A pencil's touch on the lab's controls is the controls', never kept from them.
  const onLab = { clientX: 1, clientY: 1, touchType: "stylus" };
  assert.ok(!fire(tried.element, "touchstart", { changedTouches: [onLab], touches: [onLab], target: tried.lab.numbers }));
  assert.ok(fire(tried.element, "touchstart", { changedTouches: [onLab], touches: [onLab], target: box(tried) }));
  const fillsBefore = tried.pages[0].inkCanvas.getContext().fills;
  tried.lab.sliders.thinning.input.value = "0.2";
  fire(tried.lab.sliders.thinning.input, "input", {});
  assert.equal(currentTuning().thinning, 0.2);
  assert.equal(JSON.parse(stored.get("himari.ink.tuning")).thinning, 0.2);
  assert.ok(tried.pages[0].inkCanvas.getContext().fills > fillsBefore, "painted again");
  tried.destroy();
  lab.rememberLab("?ink-lab=0");
  assert.ok(!lab.labOn() && !stored.has("himari.ink.tuning"));
  setTuning(DEFAULT_TUNING);
  delete globalThis.localStorage;

  // Encrypted: viewed only, its annotations drawn by PDF.js, no pen.
  const locked = await viewOf(fixture("encrypted.pdf"));
  assert.ok(!locked.view.writable && /encrypted/.test(locked.view.readOnly));
  assert.equal(renders.at(-1).annotationMode, pdfjs.AnnotationMode.ENABLE);
  locked.view.setTool({ tool: "pen", color: "black", size: "fine" });
  assert.equal(locked.view.tool, null);
  locked.view.destroy();
}

console.log("pdf smoke ok");
