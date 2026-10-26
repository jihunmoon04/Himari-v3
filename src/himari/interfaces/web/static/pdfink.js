/* The ink on a PDF (V3-P77 §4, §5.1): strokes, the tools, undo and redo,
 * and what a save writes. No DOM: the view (pdfpane.js) draws and takes the
 * input; this holds what was drawn.
 *
 * A stroke's points are in the page's PDF user space (points, with the pen's
 * pressure), so zoom, rotation and the CropBox never move it. Its outline is
 * perfect-freehand's, the same on screen and in the annotation's appearance
 * stream. A saved stroke is one of HIMARI's ink annotations, read back from
 * its /HimariInk; erased, it leaves its page's /Annots at the next save.
 *
 * V3-P82 §3.1, §3.3: a stroke is named when it is drawn (its /NM), so the
 * same stroke is never written twice — carried to another version, or kept
 * in the browser, a stroke the file already holds is left out. A page's
 * changes are three: strokes added (no object in this file), dropped (in its
 * /Annots, not on the page now) and put back (an object of this file, out of
 * its /Annots: an erase undone after a save). A save marks what it wrote as
 * saved where it is (``committed``): nothing is read again, and undo goes on
 * across saves.
 */

import { getStroke } from "./freehand/perfect-freehand.mjs";

export const HIGHLIGHT_ALPHA = 0.35; // as pdfdoc.js writes the highlighter's /ca
export const MAX_POINTS = 5000; // a stroke ends here (a pen held down without end)
const MAX_SAVED_POINTS = 20000; // read back from a file: more is not ours
const ERASE_SLOP = 3; // PDF points around a stroke that the eraser still takes

// §4.2: pen, three colours and two widths; the highlighter, one of each. Widths in PDF points.
export const TOOLS = {
  pen: { colors: { black: [0.1, 0.1, 0.12], blue: [0.1, 0.32, 0.85], red: [0.84, 0.16, 0.16] },
         sizes: { fine: 1.6, bold: 3.2 } },
  highlighter: { colors: { yellow: [1, 0.86, 0.1] }, sizes: { wide: 12 } },
};

/* perfect-freehand's settings: a pen with pressure thins and thickens; without (a mouse), and the
 * highlighter, keep their width. To be tuned on the iPad with the owner (§4.6): the ink lab (inklab.js)
 * changes them in one browser while it is on; ``tuned`` counts the changes, so outlines are made again. */
export const DEFAULT_TUNING = Object.freeze({ thinning: 0.6, smoothing: 0.5, streamline: 0.35 });
let tuning = { ...DEFAULT_TUNING };
let tuned = 0;

export function currentTuning() {
  return { ...tuning };
}

/* The settings from now on: each a number from 0 to 1 (thinning to 0.95), else the default's. */
export function setTuning(values = {}) {
  const fit = (value, fallback, max) => (isNumber(value) ? Math.min(max, Math.max(0, value)) : fallback);
  tuning = { thinning: fit(values.thinning, DEFAULT_TUNING.thinning, 0.95),
             smoothing: fit(values.smoothing, DEFAULT_TUNING.smoothing, 1),
             streamline: fit(values.streamline, DEFAULT_TUNING.streamline, 1) };
  tuned += 1;
  return currentTuning();
}

function freehand(stroke) {
  const varying = stroke.tool === "pen" && stroke.pressure;
  return { size: stroke.size, thinning: varying ? tuning.thinning : 0, smoothing: tuning.smoothing,
           streamline: tuning.streamline, simulatePressure: false, last: true };
}

let made = 0;
const NAME_PREFIX = "himari-ink-"; // as pdfdoc.js's HIMARI_INK_PREFIX: the /NM of HIMARI's ink

/* A new stroke's name (its /NM): random, so two tabs or devices never make the same one. */
export function strokeName() {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return NAME_PREFIX + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/* A new stroke: ``tool`` with its ``color`` and ``size`` named as in TOOLS (an unknown name: the tool's
 * first), ``pressure``: the points carry the pen's (a mouse's do not). */
export function newStroke(page, { tool, color: colorName, size: sizeName }, pressure) {
  const { colors, sizes } = TOOLS[tool];
  const color = colors[colorName] ? colorName : Object.keys(colors)[0];
  const rgb = colors[color];
  const size = sizes[sizeName] || Object.values(sizes)[0];
  made += 1;
  return { id: `new-${made}`, name: strokeName(), page, tool, color, rgb, size, pressure, points: [], saved: null,
           cached: null };
}

/* The stroke's outline, a polygon in PDF space (kept until its points or the settings change). */
export function outline(stroke) {
  if (!stroke.cached || stroke.cached.count !== stroke.points.length || stroke.cached.tuned !== tuned) {
    stroke.cached = { count: stroke.points.length, tuned, polygon: getStroke(stroke.points, freehand(stroke)) };
  }
  return stroke.cached.polygon;
}

const round = (n, places) => Math.round(n * 10 ** places) / 10 ** places;
const fixed = (n) => String(round(n, 2));

function bounds(polygon, points, pad) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of [...polygon, ...points]) {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
  }
  return [round(x0 - pad, 2), round(y0 - pad, 2), round(x1 + pad, 2), round(y1 + pad, 2)];
}

/* §5.1: what pdfdoc.js's appendIncrement writes for the stroke — /Rect, /InkList, /C, /BS /W, the
 * appearance (its outline filled; the highlighter's alpha its own ExtGState) and /HimariInk. */
export function specOf(stroke) {
  const polygon = outline(stroke);
  const [r, g, b] = stroke.rgb;
  const path = polygon.map(([x, y], i) => `${fixed(x)} ${fixed(y)} ${i === 0 ? "m" : "l"}`).join(" ");
  const appearance = `${fixed(r)} ${fixed(g)} ${fixed(b)} rg${stroke.tool === "highlighter" ? " /GS0 gs" : ""}\n` +
    (polygon.length ? `${path} h f` : "");
  return {
    name: stroke.name,
    rect: bounds(polygon, stroke.points, 1),
    inkList: [stroke.points.flatMap(([x, y]) => [round(x, 2), round(y, 2)])],
    color: stroke.rgb.map((v) => round(v, 3)),
    width: stroke.size,
    highlighter: stroke.tool === "highlighter",
    appearance,
    data: { v: 1, tool: stroke.tool, color: stroke.color, rgb: stroke.rgb.map((v) => round(v, 3)), size: stroke.size,
            pressure: stroke.pressure, points: stroke.points.map(([x, y, p]) => [round(x, 2), round(y, 2), round(p, 3)]) },
  };
}

const isNumber = (v) => typeof v === "number" && Number.isFinite(v);

/* A saved stroke from what pdfdoc.js read (``PdfFile.ink``): its /HimariInk as HIMARI wrote it, checked
 * (the file is untrusted); without one (another app dropped it), its /InkList at its /BS width. */
export function strokeFromSaved(page, entry) {
  const data = entry.data;
  const saved = { ref: entry.ref, name: entry.name };
  const valid = data && data.v === 1 && TOOLS[data.tool] && Array.isArray(data.points)
    && data.points.length > 0 && data.points.length <= MAX_SAVED_POINTS
    && data.points.every((p) => Array.isArray(p) && p.length === 3 && p.every(isNumber))
    && isNumber(data.size) && data.size > 0 && data.size <= 100
    && Array.isArray(data.rgb) && data.rgb.length === 3 && data.rgb.every((v) => isNumber(v) && v >= 0 && v <= 1);
  if (valid) {
    return { id: entry.name, name: entry.name, page, tool: data.tool, color: String(data.color), rgb: data.rgb,
             size: data.size, pressure: data.pressure === true, points: data.points.map((p) => [p[0], p[1], p[2]]),
             saved, cached: null };
  }
  const flat = (Array.isArray(entry.inkList[0]) ? entry.inkList[0] : []).filter(isNumber).slice(0, 2 * MAX_SAVED_POINTS);
  const points = [];
  for (let i = 0; i + 1 < flat.length; i += 2) points.push([flat[i], flat[i + 1], 0.5]);
  const rgb = Array.isArray(entry.color) && entry.color.length === 3 && entry.color.every(isNumber)
    ? entry.color.map((v) => Math.min(1, Math.max(0, v))) : [0, 0, 0];
  const width = isNumber(entry.width) && entry.width > 0 ? Math.min(entry.width, 100) : 1;
  const highlighter = isNumber(entry.opacity) && entry.opacity < 1;
  return { id: entry.name, name: entry.name, page, tool: highlighter ? "highlighter" : "pen", color: null, rgb,
           size: width, pressure: false, points, saved, cached: null };
}

/* Whether the eraser at (x, y), ``radius`` points wide, touches the stroke (§4.2: a whole stroke). */
export function hits(stroke, x, y, radius) {
  const reach = radius + stroke.size / 2 + ERASE_SLOP;
  const points = stroke.points;
  if (points.length === 1) return Math.hypot(points[0][0] - x, points[0][1] - y) <= reach;
  for (let i = 1; i < points.length; i += 1) {
    const [ax, ay] = points[i - 1];
    const [bx, by] = points[i];
    const dx = bx - ax, dy = by - ay;
    const length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length)) : 0;
    if (Math.hypot(ax + t * dx - x, ay + t * dy - y) <= reach) return true;
  }
  return false;
}

/* The ink of one open PDF: each page's strokes now, the names of HIMARI's strokes its /Annots holds in the file
 * (``inFile``; a page in it has been read), the objects this file has for named strokes, and the undo and redo
 * of drawing and erasing — while it is open, across saves (V3-P82 §3.5). */
export class Ink {
  constructor() {
    this.pages = new Map(); // page index → its strokes now, drawn in this order
    this.inFile = new Map(); // page index → names of the HIMARI strokes in its /Annots
    this.objects = new Map(); // a stroke's name → its annotation's Ref in this file
    this.done = [];
    this.undone = [];
    this.carried = null; // after a conflict: {erase: names} to erase again as pages are read (§5.5)
  }

  loaded(page) {
    return this.inFile.has(page);
  }

  /* A page's saved strokes, read when it is first drawn. Strokes carried here that it already holds (by name:
   * a save that landed though its answer was lost) are left out — the file's are the same strokes. */
  load(page, strokes) {
    const names = new Set(strokes.map((s) => s.saved.name));
    this.inFile.set(page, names);
    for (const s of strokes) this.objects.set(s.saved.name, s.saved.ref);
    const now = (this.pages.get(page) || []).filter((s) => !names.has(s.name));
    this.pages.set(page, [...strokes, ...now]);
    if (this.carried) {
      const gone = strokes.filter((s) => this.carried.erase.has(s.saved.name));
      if (gone.length) this.remove(page, gone);
    }
  }

  on(page) {
    return this.pages.get(page) || [];
  }

  add(stroke) {
    this.pages.set(stroke.page, [...this.on(stroke.page), stroke]);
    this.record({ add: stroke });
  }

  /* Strokes of one page erased together (one undo). */
  erase(page, strokes) {
    if (!strokes.length) return;
    const at = strokes.map((s) => this.on(page).indexOf(s));
    this.remove(page, strokes);
    this.record({ erase: { page, strokes, at } });
  }

  remove(page, strokes) {
    this.pages.set(page, this.on(page).filter((s) => !strokes.includes(s)));
  }

  record(op) {
    this.done.push(op);
    this.undone = [];
  }

  /* The page the undo or redo changed, or null when there is none. */
  undo() {
    const op = this.done.pop();
    if (!op) return null;
    this.undone.push(op);
    if (op.add) {
      this.remove(op.add.page, [op.add]);
      return op.add.page;
    }
    const strokes = [...this.on(op.erase.page)];
    op.erase.strokes.forEach((s, i) => strokes.splice(Math.min(op.erase.at[i], strokes.length), 0, s));
    this.pages.set(op.erase.page, strokes);
    return op.erase.page;
  }

  redo() {
    const op = this.undone.pop();
    if (!op) return null;
    this.done.push(op);
    if (op.add) {
      this.pages.set(op.add.page, [...this.on(op.add.page), op.add]);
      return op.add.page;
    }
    this.remove(op.erase.page, op.erase.strokes);
    return op.erase.page;
  }

  /* §3.3: per page changed, the strokes to write (``add``), the objects to put back in its /Annots
   * (``restore``: {name, ref}) and the ones to take out (``drop``: {name, ref}). */
  changes() {
    const found = [];
    for (const [page, strokes] of this.pages) {
      const inFile = this.inFile.get(page) || new Set();
      const add = strokes.filter((s) => !s.saved);
      const restore = strokes.filter((s) => s.saved && !inFile.has(s.saved.name))
        .map((s) => ({ name: s.saved.name, ref: s.saved.ref }));
      const kept = new Set(strokes.filter((s) => s.saved).map((s) => s.saved.name));
      const drop = [...inFile].filter((name) => !kept.has(name) && this.objects.has(name))
        .map((name) => ({ name, ref: this.objects.get(name) }));
      if (add.length || restore.length || drop.length) found.push({ page, add, restore, drop });
    }
    return found.sort((a, b) => a.page - b.page);
  }

  get dirty() {
    return this.changes().length > 0;
  }

  /* §3.4: ``changes`` written into the file — ``added`` names the object each added stroke got. Each is saved
   * where it is now (drawn, or erased since: then dropped at the next save), and the pages' /Annots are as
   * written; what changed meanwhile stays a change. */
  committed(changes, added) {
    for (const { page, add, restore, drop } of changes) {
      const inFile = new Set(this.inFile.get(page) || []);
      for (const stroke of add) {
        const ref = added.get(stroke.name);
        if (!ref) continue;
        stroke.saved = { ref, name: stroke.name };
        this.objects.set(stroke.name, ref);
        inFile.add(stroke.name);
      }
      for (const { name } of restore) inFile.add(name);
      for (const { name } of drop) inFile.delete(name);
      this.inFile.set(page, inFile);
    }
  }

  /* §5.5: what is unsaved, to put on another version — every stroke on a page its file does not hold, whole
   * (an object of this file may be missing there), and the names of the file's strokes erased. */
  carry() {
    const add = [];
    const erase = new Set();
    const erasePages = [];
    for (const [page, strokes] of this.pages) {
      const inFile = this.inFile.get(page) || new Set();
      for (const s of strokes) if (!s.saved || !inFile.has(s.saved.name)) add.push({ ...s, page, saved: null });
    }
    for (const [page, inFile] of this.inFile) {
      const now = new Set(this.on(page).filter((s) => s.saved).map((s) => s.saved.name));
      for (const name of inFile) {
        if (now.has(name)) continue;
        erase.add(name);
        erasePages.push(page);
      }
    }
    return { add, erase, erasePages };
  }

  /* A carry put on this ink: strokes on pages it lacks are left, and counted. On a page read already (kept
   * ink put back once the PDF shows, PR #119 review B2), a stroke it holds by name — in its file, or on it
   * now — is not added again, and the carried erasures are made now; a page read later does both in load. */
  put(carried, pages) {
    this.carried = { erase: carried.erase };
    let left = 0;
    for (const stroke of carried.add) {
      if (stroke.page >= pages) {
        left += 1;
        continue;
      }
      const inFile = this.inFile.get(stroke.page);
      if ((inFile && inFile.has(stroke.name)) || this.on(stroke.page).some((s) => s.name === stroke.name)) continue;
      this.pages.set(stroke.page, [...this.on(stroke.page), { ...stroke, cached: null }]);
    }
    for (const page of this.inFile.keys()) {
      const gone = this.on(page).filter((s) => s.saved && carried.erase.has(s.saved.name));
      if (gone.length) this.remove(page, gone);
    }
    return left;
  }
}
