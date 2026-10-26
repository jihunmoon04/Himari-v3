/* HIMARI's own reading and writing of a PDF's structure (V3-P77 §4.4, §5).
 *
 * pdf.js draws the pages; it cannot write the ink this packet saves (§5.1: a /NM of
 * ours, the /HimariInk data, an appearance filled with the stroke's outline), nor
 * tell its annotations' /NM. So this module reads what saving needs and writes
 * the incremental update itself (implementation notes: docs/pdf-ink-v0.1.md §14):
 *
 * - openPdf(bytes): the cross-reference sections (tables, streams, both in one
 *   file), the trailer, and objects by number, those in object streams included.
 *   No repair: a file whose cross-reference cannot be read is not written to.
 * - PdfFile.ink(pageRef): the page's HIMARI ink — the annotations whose /NM starts
 *   with "himari-ink-", with their /HimariInk data.
 * - appendIncrement(file, pages): the bytes to add after the file's own — new
 *   objects, each changed page with its new /Annots, a cross-reference section of
 *   the file's own form (table or stream) whose /Prev is the file's, and %%EOF.
 *   The file's bytes are never changed.
 * - checkIncrement(size, increment): the server's light check (§5.3 step 4).
 *
 * V3-P82 §3.6: a file saved in place many times has as many sections (up to
 * MAX_SECTIONS); a save cut short at its end (a power cut) is read past — the
 * last ``startxref … %%EOF`` that reads is the file's end, and ``cut`` tells
 * where to cut it back to. A stroke's /HimariInk is a Flate stream (v2; a
 * string, v1, is read too), and an increment can put a stroke's object back
 * in a page's /Annots (an erase undone after a save).
 *
 * No DOM: it runs in the browser and in Node alike (scripts/pdf_smoke.mjs).
 */

export const HIMARI_INK_PREFIX = "himari-ink-";
// /Prev links followed. Loops are caught by the offsets seen; this bounds the work: a save in place adds one
// section of a few hundred bytes at least, so 64 MiB holds well under this (V3-P82 §3.6, review r3 B1).
const MAX_SECTIONS = 50_000;
const TAIL_BYTES = 8 * 1024 * 1024 + 1024; // where the end of a save cut short is looked for: one increment's
const MAX_INK_DATA = 4 * 1024 * 1024; // a stroke's /HimariInk, inflated (more: drawn from its /InkList)

export class PdfReadError extends Error {}

export class Ref {
  constructor(num, gen = 0) {
    this.num = num;
    this.gen = gen;
  }

  get key() {
    return this.gen ? `${this.num}R${this.gen}` : `${this.num}R`; // pdf.js's annotation id
  }
}

export class Name {
  constructor(name) {
    this.name = name;
  }
}

export class PdfString {
  constructor(bytes) {
    this.bytes = bytes; // as in the file, unescaped
  }

  get text() {
    const b = this.bytes;
    if (b.length >= 2 && b[0] === 0xfe && b[1] === 0xff) return new TextDecoder("utf-16be").decode(b.subarray(2));
    if (b.length >= 3 && b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) return new TextDecoder().decode(b.subarray(3));
    return String.fromCharCode(...b); // PDFDocEncoding: its ASCII part is all HIMARI writes
  }

  static of(text) { // ASCII
    return new PdfString(Uint8Array.from(text, (c) => c.charCodeAt(0) & 0x7f));
  }

  static utf8(text) { // a PDF 2.0 text string: UTF-8 after its byte order mark
    const bytes = new TextEncoder().encode(text);
    return new PdfString(/^[\x20-\x7e]*$/.test(text) ? bytes : Uint8Array.from([0xef, 0xbb, 0xbf, ...bytes]));
  }
}

class Stream {
  constructor(dict, start) {
    this.dict = dict;
    this.start = start; // where its data starts in the file
  }
}

const KEYWORD = Symbol("keyword");
const isSpace = (c) => c === 0 || c === 9 || c === 10 || c === 12 || c === 13 || c === 32;
const isDelimiter = (c) => c === 40 || c === 41 || c === 60 || c === 62 || c === 91 || c === 93 || c === 123 ||
  c === 125 || c === 47 || c === 37;

/* --------------------------------------------------------------- parsing */

class Lexer {
  constructor(bytes, pos = 0) {
    this.bytes = bytes;
    this.pos = pos;
  }

  skip() {
    const b = this.bytes;
    while (this.pos < b.length) {
      const c = b[this.pos];
      if (isSpace(c)) this.pos += 1;
      else if (c === 37) { // a comment, to the line's end
        while (this.pos < b.length && b[this.pos] !== 10 && b[this.pos] !== 13) this.pos += 1;
      } else break;
    }
  }

  /* The next token: a number, {KEYWORD: word}, a Name, a PdfString, or "[", "]", "<<", ">>". */
  token() {
    this.skip();
    const b = this.bytes;
    if (this.pos >= b.length) throw new PdfReadError("unexpected end of file");
    const c = b[this.pos];
    if (c === 91 || c === 93) { this.pos += 1; return c === 91 ? "[" : "]"; }
    if (c === 60 && b[this.pos + 1] === 60) { this.pos += 2; return "<<"; }
    if (c === 62 && b[this.pos + 1] === 62) { this.pos += 2; return ">>"; }
    if (c === 60) return this.hexString();
    if (c === 40) return this.literalString();
    if (c === 47) return this.name();
    const start = this.pos;
    while (this.pos < b.length && !isSpace(b[this.pos]) && !isDelimiter(b[this.pos])) this.pos += 1;
    if (this.pos === start) throw new PdfReadError(`unexpected byte ${c} at ${start}`);
    const word = String.fromCharCode(...b.subarray(start, this.pos));
    if (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(word)) return Number(word);
    return { [KEYWORD]: word };
  }

  name() {
    const b = this.bytes;
    this.pos += 1;
    let name = "";
    while (this.pos < b.length && !isSpace(b[this.pos]) && !isDelimiter(b[this.pos])) {
      if (b[this.pos] === 35 && this.pos + 2 < b.length) { // #xx
        name += String.fromCharCode(parseInt(String.fromCharCode(b[this.pos + 1], b[this.pos + 2]), 16));
        this.pos += 3;
      } else {
        name += String.fromCharCode(b[this.pos]);
        this.pos += 1;
      }
    }
    return new Name(name);
  }

  hexString() {
    const b = this.bytes;
    this.pos += 1;
    const digits = [];
    while (this.pos < b.length && b[this.pos] !== 62) {
      if (!isSpace(b[this.pos])) digits.push(b[this.pos]);
      this.pos += 1;
    }
    this.pos += 1;
    if (digits.length % 2) digits.push(48);
    const out = new Uint8Array(digits.length / 2);
    for (let i = 0; i < out.length; i += 1) out[i] = parseInt(String.fromCharCode(digits[2 * i], digits[2 * i + 1]), 16);
    return new PdfString(out);
  }

  literalString() {
    const b = this.bytes;
    this.pos += 1;
    const out = [];
    let depth = 1;
    while (this.pos < b.length) {
      let c = b[this.pos++];
      if (c === 40) depth += 1;
      else if (c === 41 && --depth === 0) return new PdfString(Uint8Array.from(out));
      else if (c === 92) {
        c = b[this.pos++];
        const escaped = { 110: 10, 114: 13, 116: 9, 98: 8, 102: 12 }[c];
        if (escaped !== undefined) out.push(escaped);
        else if (c >= 48 && c <= 55) {
          let code = c - 48;
          for (let i = 0; i < 2 && b[this.pos] >= 48 && b[this.pos] <= 55; i += 1) code = code * 8 + b[this.pos++] - 48;
          out.push(code & 0xff);
        } else if (c === 13) { if (b[this.pos] === 10) this.pos += 1; } // a line continued
        else if (c !== 10) out.push(c);
        continue;
      }
      out.push(c);
    }
    throw new PdfReadError("unterminated string");
  }

  /* One value; "N G R" read as a Ref. */
  value(token = this.token()) {
    if (token === "[") {
      const items = [];
      for (let next = this.token(); next !== "]"; next = this.token()) items.push(this.value(next));
      return items;
    }
    if (token === "<<") {
      const dict = new Map();
      for (let key = this.token(); key !== ">>"; key = this.token()) {
        if (!(key instanceof Name)) throw new PdfReadError("a dictionary key is not a name");
        const item = this.value();
        if (item !== null) dict.set(key.name, item); // a null entry is no entry
      }
      return dict;
    }
    if (typeof token === "number" && Number.isInteger(token)) {
      const save = this.pos;
      try {
        const gen = this.token();
        if (typeof gen === "number" && Number.isInteger(gen)) {
          const r = this.token();
          if (r && r[KEYWORD] === "R") return new Ref(token, gen);
        }
      } catch (ignored) { /* the end: a plain number */ }
      this.pos = save;
      return token;
    }
    if (token && token[KEYWORD] !== undefined) {
      const word = token[KEYWORD];
      if (word === "true") return true;
      if (word === "false") return false;
      if (word === "null") return null;
      return token;
    }
    return token;
  }

  keyword(word) {
    const token = this.token();
    if (!token || token[KEYWORD] !== word) throw new PdfReadError(`expected ${word}`);
  }
}

function find(bytes, text, from, to = bytes.length) {
  const codes = Array.from(text, (c) => c.charCodeAt(0));
  outer: for (let i = from; i <= to - codes.length; i += 1) {
    for (let j = 0; j < codes.length; j += 1) if (bytes[i + j] !== codes[j]) continue outer;
    return i;
  }
  return -1;
}

/* The last ``text`` that starts at ``from`` or after and ends before ``before``. */
function findLastBefore(bytes, text, from, before) {
  const codes = Array.from(text, (c) => c.charCodeAt(0));
  outer: for (let i = before - codes.length; i >= from; i -= 1) {
    for (let j = 0; j < codes.length; j += 1) if (bytes[i + j] !== codes[j]) continue outer;
    return i;
  }
  return -1;
}

function findLast(bytes, text, from) {
  const codes = Array.from(text, (c) => c.charCodeAt(0));
  outer: for (let i = bytes.length - codes.length; i >= from; i -= 1) {
    for (let j = 0; j < codes.length; j += 1) if (bytes[i + j] !== codes[j]) continue outer;
    return i;
  }
  return -1;
}

async function inflate(data) {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/* Inflated, but no more than ``limit`` bytes of it (null past that): a small stream cannot fill the memory. */
async function inflateAtMost(data, limit) {
  const reader = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate")).getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

async function deflate(data) {
  const stream = new Blob([data]).stream().pipeThrough(new CompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/* The PNG predictors of a cross-reference stream (/DecodeParms /Predictor 10–15). */
function unpredict(data, columns) {
  const row = columns + 1;
  if (data.length % row) throw new PdfReadError("a predicted stream of uneven rows");
  const out = new Uint8Array((data.length / row) * columns);
  for (let r = 0; r < data.length / row; r += 1) {
    const type = data[r * row];
    for (let i = 0; i < columns; i += 1) {
      const x = data[r * row + 1 + i];
      const left = i > 0 ? out[r * columns + i - 1] : 0;
      const up = r > 0 ? out[(r - 1) * columns + i] : 0;
      const corner = r > 0 && i > 0 ? out[(r - 1) * columns + i - 1] : 0;
      let v;
      if (type === 0) v = x;
      else if (type === 1) v = x + left;
      else if (type === 2) v = x + up;
      else if (type === 3) v = x + ((left + up) >> 1);
      else if (type === 4) {
        const p = left + up - corner;
        const pa = Math.abs(p - left), pb = Math.abs(p - up), pc = Math.abs(p - corner);
        v = x + (pa <= pb && pa <= pc ? left : pb <= pc ? up : corner);
      } else throw new PdfReadError(`unknown PNG predictor ${type}`);
      out[r * columns + i] = v & 0xff;
    }
  }
  return out;
}

/* --------------------------------------------------------------- the file */

export class PdfFile {
  constructor(bytes) {
    this.bytes = bytes;
    this.size = bytes.length;
    this.entries = new Map(); // object number → {type: 1, offset, gen} | {type: 2, stream, index} | {type: 0}
    this.trailer = null; // the newest section's
    this.form = null; // "table" | "stream": the newest section's, followed by an increment
    this.startxref = 0;
    this.objectStreams = new Map();
  }

  get encrypted() {
    return this.trailer.has("Encrypt");
  }

  /* The next free object number: the newest trailer's /Size. */
  get nextNumber() {
    return this.trailer.get("Size");
  }

  /* The file's end: its last ``startxref N %%EOF`` (a line break after it allowed) — or, past bytes a save
   * cut short left after it, the last one that reads (V3-P82 §3.6). ``cut``: where to cut such bytes off,
   * when they begin as a save does (an object, or a cross-reference table); null when the file ends there,
   * or ends in what another producer left (never cut). */
  async read() {
    if (find(this.bytes, "%PDF-", 0, Math.min(this.size, 1024)) < 0) throw new PdfReadError("not a PDF");
    this.cut = null;
    let failure = new PdfReadError("no startxref");
    for (const { offset, end } of this.ends()) {
      try {
        await this.readFrom(offset);
      } catch (error) {
        if (!(error instanceof PdfReadError)) throw error;
        failure = error;
        this.entries = new Map();
        this.objectStreams = new Map();
        this.trailer = null;
        this.form = null;
        continue;
      }
      const rest = this.bytes.subarray(end);
      const left = Array.from(rest.subarray(0, 64), (b) => String.fromCharCode(b)).join("").trimStart();
      if (/^(\d+\s+\d+\s+obj\b|xref\b)/.test(left)) this.cut = end;
      return this;
    }
    throw failure;
  }

  /* Each ``startxref N %%EOF`` near the end, the last first: {offset: N, end: just after its %%EOF and one
   * line break}. The newest is in the last 1 KiB; one a cut save left behind is at most TAIL_BYTES back. */
  *ends() {
    const from = Math.max(0, this.size - TAIL_BYTES);
    let before = this.size;
    while (before > from) {
      const at = findLastBefore(this.bytes, "%%EOF", from, before);
      if (at < 0) return;
      before = at;
      const head = Array.from(this.bytes.subarray(Math.max(0, at - 40), at), (b) => String.fromCharCode(b)).join("");
      const found = /startxref\s+(\d+)\s*$/.exec(head);
      if (!found) continue;
      let end = at + 5;
      if (this.bytes[end] === 13) end += 1;
      if (this.bytes[end] === 10) end += 1;
      yield { offset: Number(found[1]), end };
    }
  }

  async readFrom(startxref) {
    this.startxref = startxref;
    if (!Number.isInteger(this.startxref) || this.startxref <= 0 || this.startxref >= this.size) {
      throw new PdfReadError("startxref out of the file");
    }
    const seen = new Set();
    let offset = this.startxref;
    while (offset !== undefined) {
      if (seen.has(offset) || seen.size >= MAX_SECTIONS) throw new PdfReadError("a loop of cross-reference sections");
      seen.add(offset);
      const { trailer, form } = await this.section(offset);
      if (this.trailer === null) {
        this.trailer = trailer;
        this.form = form;
      }
      offset = trailer.get("Prev");
      if (offset !== undefined && (!Number.isInteger(offset) || offset < 0 || offset >= this.size)) {
        throw new PdfReadError("/Prev out of the file");
      }
    }
    if (!(this.trailer.get("Root") instanceof Ref) || !Number.isInteger(this.trailer.get("Size"))) {
      throw new PdfReadError("a trailer without /Root or /Size");
    }
    return this;
  }

  /* One section: older entries never replace newer ones. */
  async section(offset) {
    const lexer = new Lexer(this.bytes, offset);
    lexer.skip();
    if (find(this.bytes, "xref", lexer.pos, lexer.pos + 4) === lexer.pos) {
      lexer.pos += 4;
      const free = []; // in a hybrid file, objects of its streams are free in its table
      for (;;) {
        const first = lexer.token();
        if (first && first[KEYWORD] === "trailer") break;
        const count = lexer.token();
        if (!Number.isInteger(first) || !Number.isInteger(count)) throw new PdfReadError("a broken xref table");
        for (let i = 0; i < count; i += 1) {
          const at = lexer.token();
          const gen = lexer.token();
          const kind = lexer.token();
          if (this.entries.has(first + i)) continue;
          if (kind[KEYWORD] === "n") this.entries.set(first + i, { type: 1, offset: at, gen });
          else free.push(first + i);
        }
      }
      const trailer = lexer.value();
      if (!(trailer instanceof Map)) throw new PdfReadError("no trailer dictionary");
      const hybrid = trailer.get("XRefStm"); // its entries stand below this table's (ISO 32000 §7.5.8.4)
      if (Number.isInteger(hybrid)) await this.streamSection(hybrid);
      for (const num of free) if (!this.entries.has(num)) this.entries.set(num, { type: 0 });
      return { trailer, form: "table" };
    }
    return { trailer: await this.streamSection(offset), form: "stream" };
  }

  async streamSection(offset) {
    const stream = this.objectAt(offset).value;
    if (!(stream instanceof Stream) || stream.dict.get("Type")?.name !== "XRef") {
      throw new PdfReadError("no cross-reference at startxref");
    }
    const { dict } = stream;
    const data = await this.decode(stream);
    const widths = dict.get("W");
    const size = dict.get("Size");
    const index = dict.get("Index") || [0, size];
    const row = widths[0] + widths[1] + widths[2];
    let at = 0;
    const field = (width, otherwise) => {
      if (width === 0) return otherwise;
      let v = 0;
      for (let i = 0; i < width; i += 1) v = v * 256 + data[at++];
      return v;
    };
    for (let s = 0; s < index.length; s += 2) {
      for (let i = 0; i < index[s + 1]; i += 1) {
        if (at + row > data.length) throw new PdfReadError("a short cross-reference stream");
        const type = field(widths[0], 1);
        const a = field(widths[1], 0);
        const b = field(widths[2], 0);
        const num = index[s] + i;
        if (this.entries.has(num)) continue;
        if (type === 1) this.entries.set(num, { type: 1, offset: a, gen: b });
        else if (type === 2) this.entries.set(num, { type: 2, stream: a, index: b });
        else this.entries.set(num, { type: 0 });
      }
    }
    return dict;
  }

  /* "N G obj value [stream]" at offset. */
  objectAt(offset) {
    const lexer = new Lexer(this.bytes, offset);
    const num = lexer.token();
    const gen = lexer.token();
    lexer.keyword("obj");
    const value = lexer.value();
    lexer.skip();
    if (value instanceof Map && find(this.bytes, "stream", lexer.pos, lexer.pos + 6) === lexer.pos) {
      let start = lexer.pos + 6;
      if (this.bytes[start] === 13) start += 1;
      if (this.bytes[start] === 10) start += 1;
      return { num, gen, value: new Stream(value, start) };
    }
    return { num, gen, value };
  }

  async streamData(stream) {
    let length = stream.dict.get("Length");
    if (length instanceof Ref) length = await this.get(length);
    if (!Number.isInteger(length) || length < 0 || stream.start + length > this.size) {
      throw new PdfReadError("a stream of no readable /Length");
    }
    return this.bytes.subarray(stream.start, stream.start + length);
  }

  /* A stream's data with its filters undone: none, or /FlateDecode with or without a PNG predictor. */
  async decode(stream) {
    let data = await this.streamData(stream);
    let filters = stream.dict.get("Filter");
    let parms = await this.resolve(stream.dict.get("DecodeParms"));
    filters = filters === undefined ? [] : Array.isArray(filters) ? filters : [filters];
    parms = Array.isArray(parms) ? parms : [parms];
    for (let i = 0; i < filters.length; i += 1) {
      if (filters[i].name !== "FlateDecode") throw new PdfReadError(`unsupported filter ${filters[i].name}`);
      data = await inflate(data);
      const p = parms[i] instanceof Map ? parms[i] : null;
      const predictor = p ? p.get("Predictor") || 1 : 1;
      if (predictor >= 10) data = unpredict(data, (p.get("Columns") || 1) * (p.get("Colors") || 1));
      else if (predictor !== 1) throw new PdfReadError(`unsupported predictor ${predictor}`);
    }
    return data;
  }

  async objectStream(num) {
    if (!this.objectStreams.has(num)) {
      this.objectStreams.set(num, (async () => {
        const stream = await this.get(num, true);
        if (!(stream instanceof Stream)) throw new PdfReadError("not an object stream");
        const data = await this.decode(stream);
        const lexer = new Lexer(data);
        const offsets = [];
        for (let i = 0; i < stream.dict.get("N"); i += 1) offsets.push([lexer.token(), lexer.token()]);
        return { data, first: stream.dict.get("First"), offsets };
      })());
    }
    return this.objectStreams.get(num);
  }

  /* An object by number (or Ref); a stream as a Stream only when ``streams``. */
  async get(ref, streams = false) {
    const num = ref instanceof Ref ? ref.num : ref;
    const entry = this.entries.get(num);
    if (!entry || entry.type === 0) return null;
    let value;
    if (entry.type === 1) {
      const found = this.objectAt(entry.offset);
      if (found.num !== num) throw new PdfReadError(`object ${num} is not at its offset`);
      value = found.value;
    } else {
      const { data, first, offsets } = await this.objectStream(entry.stream);
      const [inStream, at] = offsets[entry.index] || [];
      if (inStream !== num) throw new PdfReadError(`object ${num} is not in its object stream`);
      value = new Lexer(data, first + at).value();
    }
    if (value instanceof Stream && !streams) throw new PdfReadError(`object ${num} is a stream`);
    return value;
  }

  async resolve(value) {
    return value instanceof Ref ? this.get(value) : value;
  }

  /* Whether the PDF carries signatures (/AcroForm /SigFlags bit 1): ink is added after them (§5.4). */
  async signed() {
    const root = await this.get(this.trailer.get("Root"));
    const form = root instanceof Map ? await this.resolve(root.get("AcroForm")) : null;
    const flags = form instanceof Map ? await this.resolve(form.get("SigFlags")) : 0;
    return Number.isInteger(flags) && (flags & 1) === 1;
  }

  /* The page's /Annots as it is: refs, and the odd dictionary written in place. */
  async annots(pageRef) {
    const page = await this.get(pageRef);
    if (!(page instanceof Map)) throw new PdfReadError("not a page");
    const annots = await this.resolve(page.get("Annots"));
    return Array.isArray(annots) ? annots : [];
  }

  /* §4.4: the page's HIMARI ink — {ref, name, data, inkList, width, color, opacity}; ``data`` is the
   * /HimariInk JSON, or null where another app dropped it (the stroke is then drawn from /InkList). */
  async ink(pageRef) {
    const found = [];
    for (const item of await this.annots(pageRef)) {
      if (!(item instanceof Ref)) continue; // HIMARI writes its annotations as objects of their own
      const dict = await this.get(item);
      if (!(dict instanceof Map) || dict.get("Subtype")?.name !== "Ink") continue;
      const name = dict.get("NM");
      if (!(name instanceof PdfString) || !name.text.startsWith(HIMARI_INK_PREFIX)) continue;
      let data = null;
      try {
        data = JSON.parse(await this.inkData(dict.get("HimariInk")));
      } catch (ignored) { /* drawn from /InkList */ }
      const bs = await this.resolve(dict.get("BS"));
      found.push({ ref: item, name: name.text, data, inkList: dict.get("InkList") || [],
                   width: bs instanceof Map ? bs.get("W") ?? 1 : 1, color: dict.get("C") || [0, 0, 0],
                   opacity: dict.get("CA") ?? 1 });
    }
    return found;
  }
}

/* A stroke's /HimariInk as text: a string (v1), or a Flate stream of at most MAX_INK_DATA inflated (v2,
 * V3-P82 §3.6). Throws when it is neither, or too large. */
PdfFile.prototype.inkData = async function inkData(raw) {
  if (raw instanceof PdfString) return raw.text;
  if (!(raw instanceof Ref)) throw new PdfReadError("no /HimariInk");
  const stream = await this.get(raw, true);
  if (!(stream instanceof Stream) || stream.dict.get("Filter")?.name !== "FlateDecode") {
    throw new PdfReadError("a /HimariInk stream of another kind");
  }
  const data = await inflateAtMost(await this.streamData(stream), MAX_INK_DATA);
  if (data === null) throw new PdfReadError("a /HimariInk too large");
  return new TextDecoder().decode(data);
};

export async function openPdf(bytes) {
  return new PdfFile(bytes).read();
}

/* --------------------------------------------------------------- writing */

function number(n) {
  if (Number.isInteger(n)) return String(n);
  const text = n.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  return text === "-0" ? "0" : text;
}

function nameText(name) {
  let out = "/";
  for (const c of name) {
    const code = c.charCodeAt(0);
    out += code > 32 && code < 127 && !isDelimiter(code) && code !== 35 ? c : `#${code.toString(16).padStart(2, "0")}`;
  }
  return out;
}

/* A value as PDF syntax; strings as hex, so no byte needs escaping. */
export function serialize(value) {
  if (value === null || value === undefined) return "null";
  if (value === true || value === false) return String(value);
  if (typeof value === "number") return number(value);
  if (value instanceof Ref) return `${value.num} ${value.gen} R`;
  if (value instanceof Name) return nameText(value.name);
  if (value instanceof PdfString) return `<${Array.from(value.bytes, (b) => b.toString(16).padStart(2, "0")).join("")}>`;
  if (Array.isArray(value)) return `[${value.map(serialize).join(" ")}]`;
  if (value instanceof Map) return `<<${[...value].map(([k, v]) => `${nameText(k)} ${serialize(v)}`).join(" ")}>>`;
  if (value && value[KEYWORD] !== undefined) throw new Error(`a bare keyword ${value[KEYWORD]} cannot be written`);
  throw new Error(`cannot write ${typeof value}`);
}

function pdfDate(date) {
  const two = (n) => String(n).padStart(2, "0");
  return `D:${date.getUTCFullYear()}${two(date.getUTCMonth() + 1)}${two(date.getUTCDate())}${two(date.getUTCHours())}` +
    `${two(date.getUTCMinutes())}${two(date.getUTCSeconds())}Z`;
}

function randomName() {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return HIMARI_INK_PREFIX + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

const ascii = (text) => Uint8Array.from(text, (c) => c.charCodeAt(0));

function concat(parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/* One stroke (§5.1). {rect: [x0, y0, x1, y1], inkList: [[x, y, …], …], color: [r, g, b], width,
 * highlighter, appearance: the /AP /N content (PDF operators, in page space), data: /HimariInk}. */
function inkDict(stroke, name, pageRef, apRef, dataRef, date) {
  const dict = new Map([
    ["Type", new Name("Annot")], ["Subtype", new Name("Ink")], ["Rect", stroke.rect],
    ["InkList", stroke.inkList], ["C", stroke.color], ["BS", new Map([["W", stroke.width]])],
    ["NM", PdfString.of(name)], ["M", PdfString.of(pdfDate(date))], ["F", 4], ["P", pageRef],
    ["AP", new Map([["N", apRef]])], ["HimariInk", dataRef],
  ]);
  if (stroke.highlighter) dict.set("CA", 0.35);
  return dict;
}

function appearanceDict(stroke) {
  const dict = new Map([["Type", new Name("XObject")], ["Subtype", new Name("Form")], ["BBox", stroke.rect]]);
  if (stroke.highlighter) { // §5.1, review P2-4: the alpha is the appearance's own
    const gs = new Map([["ca", 0.35], ["CA", 0.35], ["BM", new Name("Multiply")]]);
    dict.set("Resources", new Map([["ExtGState", new Map([["GS0", gs]])]]));
  }
  return dict;
}

/* §5.2, V3-P82 §3.3: the incremental update. ``pages``: [{ref: the page's Ref, drop: Refs of its HIMARI ink
 * erased, restore: Refs of HIMARI ink of this file put back (an erase undone after a save), add: strokes as
 * specOf makes them (named)}]. Returns {bytes: what to add after the file's own, added: a stroke's name → the
 * Ref of its annotation}. */
export async function appendIncrement(file, pages, { date = new Date() } = {}) {
  if (file.encrypted) throw new PdfReadError("an encrypted PDF is not written to");
  let next = file.nextNumber;
  const objects = []; // [num, gen, bytes]
  const added = new Map();
  const body = (num, gen, text) => objects.push([num, gen, ascii(`${num} ${gen} obj\n${text}\nendobj\n`)]);
  const stream = (num, dict, content) => {
    dict.set("Filter", new Name("FlateDecode"));
    dict.set("Length", content.length);
    objects.push([num, 0, concat([ascii(`${num} 0 obj\n${serialize(dict)}\nstream\n`), content,
                                  ascii("\nendstream\nendobj\n")])]);
  };
  for (const { ref, drop = [], restore = [], add = [] } of pages) {
    const page = await file.get(ref);
    if (!(page instanceof Map)) throw new PdfReadError("not a page");
    const dropped = new Set(drop.map((r) => r.key));
    const annots = (await file.annots(ref)).filter((a) => !(a instanceof Ref && dropped.has(a.key)));
    const held = new Set(annots.filter((a) => a instanceof Ref).map((a) => a.key));
    for (const back of restore) {
      if (held.has(back.key)) continue;
      held.add(back.key);
      annots.push(back);
    }
    for (const stroke of add) {
      const name = stroke.name || randomName();
      const apRef = new Ref(next++);
      const dataRef = new Ref(next++);
      const annotRef = new Ref(next++);
      stream(apRef.num, appearanceDict(stroke), await deflate(ascii(stroke.appearance)));
      stream(dataRef.num, new Map(), await deflate(new TextEncoder().encode(JSON.stringify(stroke.data))));
      body(annotRef.num, 0, serialize(inkDict(stroke, name, ref, apRef, dataRef, date)));
      annots.push(annotRef);
      added.set(name, annotRef);
    }
    const changed = new Map(page);
    if (annots.length) changed.set("Annots", annots);
    else changed.delete("Annots");
    body(ref.num, ref.gen, serialize(changed));
  }
  const parts = [ascii("\n")]; // the file may end right after %%EOF
  let at = file.size + 1;
  const offsets = [];
  for (const [num, gen, bytes] of objects) {
    offsets.push([num, gen, at]);
    parts.push(bytes);
    at += bytes.length;
  }
  const trailer = new Map([["Size", next], ["Root", file.trailer.get("Root")]]);
  for (const key of ["Info", "ID"]) if (file.trailer.has(key)) trailer.set(key, file.trailer.get(key));
  trailer.set("Prev", file.startxref);
  if (file.form === "stream") {
    const xrefNum = next;
    trailer.set("Size", next + 1);
    offsets.push([xrefNum, 0, at]);
    offsets.sort((a, b) => a[0] - b[0]);
    const rows = new Uint8Array(offsets.length * 7); // /W [1 4 2]
    offsets.forEach(([, gen, offset], i) => {
      rows[i * 7] = 1;
      new DataView(rows.buffer).setUint32(i * 7 + 1, offset);
      new DataView(rows.buffer).setUint16(i * 7 + 5, gen);
    });
    const dict = new Map([["Type", new Name("XRef")], ...trailer, ["W", [1, 4, 2]],
                          ["Index", subsections(offsets).flatMap(([first, count]) => [first, count])],
                          ["Length", rows.length]]);
    parts.push(ascii(`${xrefNum} 0 obj\n${serialize(dict)}\nstream\n`), rows, ascii("\nendstream\nendobj\n"),
               ascii(`startxref\n${at}\n%%EOF\n`));
    return { bytes: concat(parts), added };
  }
  offsets.sort((a, b) => a[0] - b[0]);
  let table = "xref\n";
  for (const [first, count, rows] of subsections(offsets)) {
    table += `${first} ${count}\n`;
    for (const [, gen, offset] of rows) table += `${String(offset).padStart(10, "0")} ${String(gen).padStart(5, "0")} n\r\n`;
  }
  parts.push(ascii(`${table}trailer\n${serialize(trailer)}\nstartxref\n${at}\n%%EOF\n`));
  return { bytes: concat(parts), added };
}

/* Runs of consecutive object numbers: [first, count, rows]. */
function subsections(offsets) {
  const runs = [];
  for (const row of offsets) {
    const last = runs[runs.length - 1];
    if (last && last[0] + last[1] === row[0]) {
      last[1] += 1;
      last[2].push(row);
    } else runs.push([row[0], 1, [row]]);
  }
  return runs;
}

/* §5.3 step 4, as the server checks it: the increment ends with %%EOF (a line break after it allowed) and
 * its last startxref points inside it. */
export function checkIncrement(size, increment) {
  let end = increment.length;
  while (end > 0 && (increment[end - 1] === 10 || increment[end - 1] === 13)) end -= 1;
  if (end < 5 || find(increment, "%%EOF", end - 5, end) !== end - 5) return false;
  const at = findLast(increment.subarray(0, end), "startxref", 0);
  if (at < 0) return false;
  let offset;
  try {
    offset = new Lexer(increment, at + 9).token();
  } catch (ignored) {
    return false;
  }
  return Number.isInteger(offset) && offset > size && offset < size + increment.length;
}
