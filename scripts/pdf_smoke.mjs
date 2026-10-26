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
assert.equal(pane.pageAt([0, 100, 200], 150), 1);
assert.equal(pane.pageAt([0, 100, 200], 0), 0);
assert.equal(pane.pageAt([0, 100, 200], 999), 2);

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

console.log("pdf smoke ok");
