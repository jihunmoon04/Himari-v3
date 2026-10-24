/* Hostile-content-safe assistant text renderer, taken unchanged from
 * HIMARI-v2 interfaces/web/static/app.js (UX-CORRECTION-01). It builds a
 * closed set of elements with createElement/createTextNode, never parses
 * HTML, and lets only an http/https/mailto URL become a link. */

/** UX-CORRECTION-01: assistant prose is formatted, and it is still hostile.
 *
 *  `renderAssistantText` is the ONE place model text becomes structure. It
 *  recognises a closed set of Markdown block/inline forms and builds each
 *  one explicitly with `createElement` and `createTextNode`. Nothing the
 *  model writes is ever parsed as HTML: `<script>` or `<img onerror=...>`
 *  in the text is just characters inside a text node, and there is still
 *  no `innerHTML`-style sink anywhere in this file.
 *
 *  The only attribute this file sets from a model value is a link `href`,
 *  and only after `new URL()` -- the browser's own parser, not a regex --
 *  has proven the scheme is in SAFE_LINK_PROTOCOLS. Relative,
 *  protocol-relative, `javascript:`, `data:` and `vbscript:` targets stay
 *  plain text. No image is ever created (so rendering fetches nothing but
 *  HIMARI's own KaTeX files), and bare URLs are not auto-linked.
 *
 *  Formulas -- `$...$` and `\(...\)` inline, `$$...$$` and `\[...\]`
 *  displayed -- are cut out before any other inline form, so `_`, `*` and
 *  the backslashes in TeX reach KaTeX as written. A formula KaTeX refuses
 *  shows as the text it was. KaTeX builds the formula with DOM calls too
 *  (never HTML) and with `trust: false`, so `\href`, `\url`,
 *  `\includegraphics` and `\html...` make no link, image, id or data
 *  attribute. What it does build from the TeX: text nodes (glyphs, and
 *  the TeX itself in the MathML annotation), and `class`/`style` values
 *  it computes, with sizes and colours it has parsed and checked
 *  (scripts/rich_text_smoke.mjs lists what it may emit). The TeX is kept
 *  in `mathSources` rather than a data attribute.
 *
 *  Used for ASSISTANT_MESSAGE / ASSISTANT_COMMENTARY / ASSISTANT_QUESTION
 *  timeline items only. User text, tool output, approval arguments, diffs
 *  and server errors keep their plain-text rendering. */
export const SAFE_LINK_PROTOCOLS = Object.freeze(["http:", "https:", "mailto:"]);

/** Nesting bound for quotes/lists and for inline spans. Past it the
 *  remaining text is shown literally rather than recursed into. */
const RICH_TEXT_MAX_DEPTH = 12;
const RICH_LINK_LABEL_MAX = 1000;
const RICH_LINK_DEST_MAX = 2048;
const RICH_ESCAPABLE = "\\`*_{}[]()#+-.!|~<>\"'$";

/* -- formulas (KaTeX) ------------------------------------------------- */

const KATEX_SCRIPT = "/assets/katex/katex.min.js";
const KATEX_STYLE = "/assets/katex/katex.min.css";
/** A longer formula is shown as written, never handed to KaTeX. */
const MATH_MAX_CHARS = 4000;
const KATEX_OPTIONS = Object.freeze({
  throwOnError: true, // a refused formula falls back to its own text
  trust: false,
  strict: "ignore", // Korean or other text in math mode is not an error
  maxSize: 20,
  maxExpand: 500,
  output: "htmlAndMathml",
});

/** Each formula node -> { tex, display, source }. A WeakMap rather than a
 *  data attribute, so no attribute this file sets carries model text
 *  except a link href. */
const mathSources = new WeakMap();
let katexLoad = null;
let mathWidths = null;

/** window.katex, loading the script and stylesheet with the first formula
 *  (most answers have none). Resolves null if they cannot load; the
 *  formulas then stay as written until the page is opened again. */
function loadKatex(doc) {
  const view = doc.defaultView;
  if (view?.katex) return Promise.resolve(view.katex);
  if (katexLoad === null) {
    katexLoad = new Promise((resolve) => {
      if (!doc.head) {
        resolve(null);
        return;
      }
      const style = doc.createElement("link");
      style.rel = "stylesheet";
      style.href = KATEX_STYLE;
      const script = doc.createElement("script");
      script.src = KATEX_SCRIPT;
      script.onload = () => resolve(view?.katex ?? null);
      script.onerror = () => resolve(null);
      doc.head.append(style, script);
    });
  }
  return katexLoad;
}

function paintMath(node, katex) {
  const math = mathSources.get(node);
  node.classList.remove("is-pending");
  try {
    katex.render(math.tex, node, { ...KATEX_OPTIONS, displayMode: math.display });
  } catch (refused) {
    node.textContent = math.source;
    node.classList.add("is-refused");
  }
}

/** An inline formula with a part wider than its line -- a group KaTeX
 *  cannot break, such as a long `\left( ... \right)` -- is marked
 *  `is-wide`: it then takes a line of its own and scrolls sideways, as a
 *  displayed one does. Breakable formulas still wrap with the text. */
function fitMath(node) {
  const view = node.ownerDocument.defaultView;
  const parts = node.querySelectorAll(".katex-html > .katex-base");
  let line = node.parentElement;
  while (line && view.getComputedStyle(line).display === "inline") line = line.parentElement;
  if (!parts.length || !line) return;
  const style = view.getComputedStyle(line);
  const room = line.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const widest = Math.max(...Array.from(parts, (part) => part.getBoundingClientRect().width));
  node.classList.toggle("is-wide", widest > room + 0.5);
}

/** Fit an answer's inline formulas when it is first laid out and whenever
 *  its width changes (window, sidebar). An answer taken off the page is
 *  no longer watched. */
function watchMathWidth(doc, container) {
  const view = doc.defaultView;
  if (!view?.ResizeObserver) return;
  if (mathWidths === null) {
    mathWidths = new view.ResizeObserver((entries) => {
      for (const { target } of entries) {
        if (!target.isConnected) {
          mathWidths.unobserve(target);
          continue;
        }
        for (const node of target.querySelectorAll(".rich-math:not(.rich-math-display)")) fitMath(node);
      }
    });
  }
  mathWidths.observe(container);
}

/** The text of `renderAssistantText(doc, text)` with each formula as it
 *  was written: for a one-line summary, where KaTeX's glyphs and MathML
 *  would read as noise. */
export function assistantTextLine(doc, text) {
  const node = renderAssistantText(doc, text);
  for (const math of node.querySelectorAll(".rich-math")) {
    math.replaceWith(doc.createTextNode(mathSources.get(math).source));
  }
  return node.textContent;
}

const RICH_FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const RICH_FENCE_CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const RICH_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/;
const RICH_RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const RICH_QUOTE = /^ {0,3}> ?(.*)$/;
const RICH_LIST_ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$/;
const RICH_TABLE_DELIMITER = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

/** A link target the browser may act on, or null.
 *
 *  Anything with whitespace/control characters is refused outright (the
 *  URL parser silently strips some of them, which is how `java\tscript:`
 *  style tricks work). Anything that does not parse as an absolute URL --
 *  every relative or protocol-relative form -- is refused as well. */
export function safeLinkHref(raw) {
  if (typeof raw !== "string" || raw.length === 0) return null;
  if (/[\u0000-\u0020\u007f-\u009f]/.test(raw)) return null;
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (ignored) {
    return null;
  }
  if (!SAFE_LINK_PROTOCOLS.includes(parsed.protocol)) return null;
  return parsed.href;
}

export function renderAssistantText(doc, text) {
  const el = (tag, className) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    return node;
  };
  // The single text primitive of this renderer. A text node can only ever
  // hold characters.
  const textNode = (value) => doc.createTextNode(String(value));

  const isBlank = (line) => line.trim().length === 0;
  const indentOf = (line) => line.length - line.replace(/^ +/, "").length;
  const stripIndent = (line, count) => line.slice(Math.min(count, indentOf(line)));

  /* -- inline -------------------------------------------------------- */

  const runLength = (source, index, character) => {
    let end = index;
    while (end < source.length && source[end] === character) end += 1;
    return end - index;
  };

  /** Index of the backtick run of exactly `length` that closes a code span
   *  opened before `from`, or -1. */
  function codeSpanClose(source, from, length, memo) {
    const key = `code${length}`;
    if (memo[key] !== undefined && from >= memo[key]) return -1;
    let index = from;
    while (index < source.length) {
      if (source[index] === "`") {
        const run = runLength(source, index, "`");
        if (run === length) return index;
        index += run;
      } else {
        index += 1;
      }
    }
    memo[key] = from;
    return -1;
  }

  const opensMath = (source, index) =>
    source[index] === "$" || (source[index] === "\\" && (source[index + 1] === "(" || source[index + 1] === "["));

  /** Index of `closer` at or after `from`, stepping over `\x` pairs, or -1. */
  function mathClose(source, from, closer, memo) {
    const key = `math${closer}`;
    if (memo[key] !== undefined && from >= memo[key]) return -1;
    let index = from;
    while (index < source.length) {
      if (source.startsWith(closer, index)) return index;
      index += source[index] === "\\" ? 2 : 1;
    }
    memo[key] = from;
    return -1;
  }

  /** A formula opening at `index` (see `opensMath`), or null.
   *
   *  `$` follows Pandoc: the opening one has a non-space on its right, the
   *  closing one a non-space on its left and no digit after it, so "$5 and
   *  $10" and "$5-$10" stay prices. The first `$` after the opening one has
   *  to be the closing one, so "$5, or $x$" is a price and then x. */
  function matchMath(source, index, memo) {
    const key = `math@${index}`;
    if (memo[key] === undefined) memo[key] = findMath(source, index, memo);
    return memo[key];
  }

  function findMath(source, index, memo) {
    const pairs = [["$$", "$$", true], ["\\[", "\\]", true], ["\\(", "\\)", false]];
    for (const [open, closer, display] of pairs) {
      if (!source.startsWith(open, index)) continue;
      const close = mathClose(source, index + open.length, closer, memo);
      if (close < 0) return null;
      const tex = source.slice(index + open.length, close);
      if (!tex.trim()) return null;
      const end = close + closer.length;
      return { tex, display, source: source.slice(index, end), end };
    }
    if (source[index] !== "$" || isSpace(source[index + 1])) return null;
    let cursor = index + 1;
    while (cursor < source.length && source[cursor] !== "$") cursor += source[cursor] === "\\" ? 2 : 1;
    if (cursor >= source.length || isSpace(source[cursor - 1]) || /[0-9]/.test(source[cursor + 1] ?? "")) {
      return null;
    }
    return { tex: source.slice(index + 1, cursor), display: false, source: source.slice(index, cursor + 1), end: cursor + 1 };
  }

  let inlineMath = false;

  /** The node for one formula: its own text until KaTeX is there. */
  function appendMath(parent, math, tag = "span") {
    const node = el(tag, math.display ? "rich-math rich-math-display" : "rich-math");
    mathSources.set(node, math);
    node.append(textNode(math.source));
    parent.append(node);
    if (math.tex.length > MATH_MAX_CHARS) {
      node.classList.add("is-refused");
      return;
    }
    if (!math.display) inlineMath = true;
    const katex = doc.defaultView?.katex;
    if (katex) {
      paintMath(node, katex);
      return;
    }
    node.classList.add("is-pending");
    loadKatex(doc).then((loaded) => {
      if (loaded) paintMath(node, loaded);
      else node.classList.remove("is-pending");
      if (loaded && !math.display && node.isConnected) fitMath(node);
    });
  }

  /** Advance past an escape, a whole code span or a whole formula starting
   *  at `index`, so closer searches never see delimiters that are really
   *  literal code or TeX. */
  function skipLiteral(source, index, memo) {
    if (opensMath(source, index)) {
      const math = matchMath(source, index, memo);
      if (math) return math.end;
    }
    if (source[index] === "\\") return index + 2;
    if (source[index] === "`") {
      const run = runLength(source, index, "`");
      const close = codeSpanClose(source, index + run, run, memo);
      return close >= 0 ? close + run : index + run;
    }
    return -1;
  }

  const isWordCharacter = (character) => character !== undefined && /[\p{L}\p{N}]/u.test(character);
  const isSpace = (character) => character === undefined || /\s/.test(character);

  /** An emphasis/strong/strikethrough span opening at `index`, or null. */
  function matchEmphasis(source, index, memo) {
    const marker = source[index];
    const run = runLength(source, index, marker);
    if (isSpace(source[index + run])) return null;
    // `snake_case_names` must stay words: `_` only opens at a word edge.
    if (marker === "_" && isWordCharacter(source[index - 1])) return null;
    const widths = marker === "~" ? (run === 2 ? [2] : []) : run >= 2 ? [2, 1] : [1];
    for (const width of widths) {
      const key = `${marker}${width}${run >= 3 ? "+" : ""}`;
      const from = index + width;
      if (memo[key] !== undefined && from >= memo[key]) continue;
      let cursor = from;
      while (cursor < source.length) {
        const skipped = skipLiteral(source, cursor, memo);
        if (skipped >= 0) {
          cursor = skipped;
          continue;
        }
        if (source[cursor] !== marker) {
          cursor += 1;
          continue;
        }
        const closeRun = runLength(source, cursor, marker);
        const fits = closeRun === width || (closeRun >= 3 && run >= 3);
        const flanked =
          !isSpace(source[cursor - 1]) &&
          !(marker === "_" && isWordCharacter(source[cursor + closeRun]));
        const closeStart = cursor + closeRun - width;
        if (fits && flanked && closeStart > from) {
          const tag = marker === "~" ? "del" : width === 2 ? "strong" : "em";
          return { tag, innerStart: from, innerEnd: closeStart, end: cursor + closeRun };
        }
        cursor += closeRun;
      }
      memo[key] = from;
    }
    return null;
  }

  /** `[label](destination "optional title")` opening at `index`, or null. */
  function matchLink(source, index, memo) {
    let depth = 0;
    let cursor = index + 1;
    let close = -1;
    const limit = Math.min(source.length, index + 1 + RICH_LINK_LABEL_MAX);
    while (cursor < limit) {
      const skipped = skipLiteral(source, cursor, memo);
      if (skipped >= 0) {
        cursor = skipped;
        continue;
      }
      if (source[cursor] === "[") depth += 1;
      if (source[cursor] === "]") {
        if (depth === 0) {
          close = cursor;
          break;
        }
        depth -= 1;
      }
      cursor += 1;
    }
    if (close < 0 || source[close + 1] !== "(") return null;

    let position = close + 2;
    while (source[position] === " ") position += 1;
    let destination = "";
    if (source[position] === "<") {
      const end = source.indexOf(">", position + 1);
      if (end < 0 || end - position > RICH_LINK_DEST_MAX) return null;
      destination = source.slice(position + 1, end);
      position = end + 1;
    } else {
      let parens = 0;
      const start = position;
      while (position < source.length && position - start <= RICH_LINK_DEST_MAX) {
        const character = source[position];
        if (/\s/.test(character)) break;
        if (character === "(") parens += 1;
        if (character === ")") {
          if (parens === 0) break;
          parens -= 1;
        }
        position += 1;
      }
      destination = source.slice(start, position);
    }
    while (source[position] === " ") position += 1;
    const quote = source[position];
    if (quote === '"' || quote === "'") {
      const end = source.indexOf(quote, position + 1);
      if (end < 0) return null;
      position = end + 1;
      while (source[position] === " ") position += 1;
    }
    if (source[position] !== ")") return null;
    return { label: source.slice(index + 1, close), destination, end: position + 1 };
  }

  function appendInline(parent, source, depth, insideLink = false) {
    // Failure memo for this span: once a search for some closer failed from
    // position p it fails from every later position too, so it is never
    // repeated. Keeps adversarial input (thousands of unmatched `*` or
    // backticks) linear instead of quadratic.
    const memo = Object.create(null);
    let buffer = "";
    const flush = () => {
      if (!buffer) return;
      // Soft line breaks are kept as line breaks: that is how model prose
      // was always shown, and chat text relies on it.
      const lines = buffer.split("\n");
      lines.forEach((line, lineIndex) => {
        if (lineIndex > 0) parent.append(el("br"));
        if (line) parent.append(textNode(line));
      });
      buffer = "";
    };
    let index = 0;
    while (index < source.length) {
      const character = source[index];
      if (opensMath(source, index)) {
        const math = matchMath(source, index, memo);
        if (math) {
          flush();
          appendMath(parent, math);
          index = math.end;
          continue;
        }
      }
      if (character === "\\" && RICH_ESCAPABLE.includes(source[index + 1] ?? "\n")) {
        buffer += source[index + 1];
        index += 2;
        continue;
      }
      if (character === "`") {
        const run = runLength(source, index, "`");
        const close = codeSpanClose(source, index + run, run, memo);
        if (close < 0) {
          buffer += source.slice(index, index + run);
          index += run;
          continue;
        }
        let body = source.slice(index + run, close).replace(/\n/g, " ");
        if (body.length >= 2 && body.startsWith(" ") && body.endsWith(" ") && body.trim()) {
          body = body.slice(1, -1);
        }
        flush();
        const code = el("code", "rich-code");
        code.append(textNode(body));
        parent.append(code);
        index = close + run;
        continue;
      }
      if (depth < RICH_TEXT_MAX_DEPTH && (character === "*" || character === "_" || character === "~")) {
        const span = matchEmphasis(source, index, memo);
        if (span) {
          flush();
          const node = el(span.tag);
          appendInline(node, source.slice(span.innerStart, span.innerEnd), depth + 1, insideLink);
          parent.append(node);
          index = span.end;
          continue;
        }
        const run = runLength(source, index, character);
        buffer += source.slice(index, index + run);
        index += run;
        continue;
      }
      if (!insideLink && depth < RICH_TEXT_MAX_DEPTH && character === "[") {
        const link = matchLink(source, index, memo);
        if (link) {
          flush();
          const href = safeLinkHref(link.destination);
          if (href === null) {
            // An unsafe or relative target never becomes actionable: only
            // its label is shown, as ordinary text.
            appendInline(parent, link.label, depth + 1, true);
          } else {
            const anchor = el("a", "rich-link");
            anchor.setAttribute("href", href);
            if (href.startsWith("http")) {
              anchor.setAttribute("target", "_blank");
              anchor.setAttribute("rel", "noopener noreferrer");
            }
            appendInline(anchor, link.label, depth + 1, true);
            parent.append(anchor);
          }
          index = link.end;
          continue;
        }
      }
      buffer += character;
      index += 1;
    }
    flush();
  }

  /* -- blocks -------------------------------------------------------- */

  /** A displayed formula on lines of its own -- a line starting with `$$`
   *  or `\\[` up to the line ending with its closer, with no blank line
   *  between -- or null. Its lines never reach the block rules, so TeX
   *  lines that start with `-` or `|` stay TeX. `misses` keeps, per closer,
   *  the lines a search already ran through without finding it. */
  function mathBlock(lines, index, misses) {
    const first = lines[index].trimStart();
    const opener = first.startsWith("$$") ? "$$" : first.startsWith("\\[") ? "\\[" : null;
    if (opener === null) return null;
    const closer = opener === "$$" ? "$$" : "\\]";
    const miss = misses[closer];
    if (miss && index >= miss.from && index < miss.to) return null;
    const body = [];
    let rest = first.slice(opener.length);
    let cursor = index;
    for (; cursor < lines.length; cursor += 1) {
      if (cursor > index) {
        rest = lines[cursor];
        if (isBlank(rest)) break;
      }
      let close = 0;
      while (close < rest.length && !rest.startsWith(closer, close)) close += rest[close] === "\\" ? 2 : 1;
      if (close >= rest.length) {
        body.push(rest);
        continue;
      }
      if (!isBlank(rest.slice(close + closer.length))) return null;
      body.push(rest.slice(0, close));
      const tex = body.join("\n");
      if (!tex.trim()) return null;
      return { tex, display: true, source: `${opener}${tex}${closer}`, end: cursor + 1 };
    }
    misses[closer] = { from: index, to: cursor };
    return null;
  }

  const listKind = (marker) => (/\d/.test(marker[0]) ? `ol${marker.slice(-1)}` : `ul${marker}`);

  function startsBlock(line, next) {
    if (RICH_FENCE.test(line) || RICH_HEADING.test(line) || RICH_RULE.test(line) || RICH_QUOTE.test(line)) {
      return true;
    }
    const item = RICH_LIST_ITEM.exec(line);
    // A list interrupts a paragraph only when it is unambiguous: a bullet,
    // or an ordered list that starts at 1 ("2024. was a year" stays prose).
    if (item && item[1].length <= 3 && item[3] && (!/\d/.test(item[2][0]) || /^1[.)]$/.test(item[2]))) {
      return true;
    }
    return isTableStart(line, next);
  }

  /** A formula is kept whole and as written: `$P(A|B)$` stays one cell, and
   *  the `\|` in `$\|x\|$` is TeX's norm, not an escaped column bar. */
  function splitRow(line) {
    let body = line.trim();
    if (body.startsWith("|")) body = body.slice(1);
    if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
    const cells = [];
    const memo = Object.create(null);
    let current = "";
    for (let index = 0; index < body.length; index += 1) {
      const math = opensMath(body, index) ? matchMath(body, index, memo) : null;
      if (math) {
        current += math.source;
        index = math.end - 1;
      } else if (body[index] === "\\" && body[index + 1] === "|") {
        current += "|";
        index += 1;
      } else if (body[index] === "|") {
        cells.push(current.trim());
        current = "";
      } else {
        current += body[index];
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function isTableStart(line, next) {
    if (next === undefined || !line.includes("|") || !next.includes("|")) return false;
    if (!RICH_TABLE_DELIMITER.test(next)) return false;
    return splitRow(line).length === splitRow(next).length;
  }

  function appendTable(parent, lines, index) {
    const header = splitRow(lines[index]);
    const aligns = splitRow(lines[index + 1]).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      return left ? "left" : null;
    });
    const wrap = el("div", "rich-table-wrap");
    const table = el("table", "rich-table");
    const row = (cells, cellTag) => {
      const tr = el("tr");
      header.forEach((ignored, column) => {
        const cell = el(cellTag, aligns[column] ? `rich-align-${aligns[column]}` : null);
        appendInline(cell, cells[column] ?? "", 0);
        tr.append(cell);
      });
      return tr;
    };
    const head = el("thead");
    head.append(row(header, "th"));
    table.append(head);
    const body = el("tbody");
    let cursor = index + 2;
    while (cursor < lines.length && !isBlank(lines[cursor]) && lines[cursor].includes("|")) {
      body.append(row(splitRow(lines[cursor]), "td"));
      cursor += 1;
    }
    if (body.children.length > 0) table.append(body);
    wrap.append(table);
    parent.append(wrap);
    return cursor;
  }

  function appendList(parent, lines, index, depth) {
    const first = RICH_LIST_ITEM.exec(lines[index]);
    const kind = listKind(first[2]);
    const ordered = kind.startsWith("ol");
    const list = el(ordered ? "ol" : "ul", "rich-list");
    if (ordered) {
      const start = Number.parseInt(first[2], 10);
      if (start !== 1) list.setAttribute("start", String(start));
    }
    let cursor = index;
    while (cursor < lines.length) {
      const item = RICH_LIST_ITEM.exec(lines[cursor]);
      if (!item || listKind(item[2]) !== kind || item[1].length > 3) break;
      const ownIndent = item[1].length;
      const markerEnd = ownIndent + item[2].length;
      const gap = lines[cursor].slice(markerEnd).length - lines[cursor].slice(markerEnd).replace(/^ +/, "").length;
      const contentIndent = markerEnd + (gap >= 1 && gap <= 4 ? gap : 1);
      const itemLines = [item[3] ?? ""];
      cursor += 1;
      let continues = true;
      while (cursor < lines.length) {
        const line = lines[cursor];
        if (isBlank(line)) {
          let look = cursor + 1;
          while (look < lines.length && isBlank(lines[look])) look += 1;
          if (look < lines.length && indentOf(lines[look]) >= contentIndent) {
            for (; cursor < look; cursor += 1) itemLines.push("");
            continue;
          }
          const sibling = look < lines.length ? RICH_LIST_ITEM.exec(lines[look]) : null;
          continues = Boolean(sibling && listKind(sibling[2]) === kind && sibling[1].length <= 3);
          cursor = look;
          break;
        }
        const nested = RICH_LIST_ITEM.exec(line);
        // Anything indented past this item's own marker belongs to it --
        // models routinely indent a sub-list under "1." by two spaces only.
        if (indentOf(line) >= contentIndent || (nested && indentOf(line) > ownIndent)) {
          itemLines.push(stripIndent(line, contentIndent));
          cursor += 1;
          continue;
        }
        if (nested || startsBlock(line, lines[cursor + 1])) break;
        itemLines.push(line.trim());
        cursor += 1;
      }
      const li = el("li");
      appendBlocks(li, itemLines, depth + 1);
      list.append(li);
      if (!continues) break;
    }
    parent.append(list);
    return cursor;
  }

  function appendBlocks(parent, lines, depth) {
    if (depth > RICH_TEXT_MAX_DEPTH) {
      const paragraph = el("p", "rich-p");
      appendInline(paragraph, lines.join("\n"), RICH_TEXT_MAX_DEPTH);
      parent.append(paragraph);
      return;
    }
    const misses = Object.create(null);
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (isBlank(line)) {
        index += 1;
        continue;
      }

      const fence = RICH_FENCE.exec(line);
      if (fence && !(fence[1][0] === "`" && fence[2].includes("`"))) {
        const fenceIndent = indentOf(line);
        const body = [];
        index += 1;
        while (index < lines.length) {
          const closing = RICH_FENCE_CLOSE.exec(lines[index]);
          if (closing && closing[1][0] === fence[1][0] && closing[1].length >= fence[1].length) break;
          body.push(stripIndent(lines[index], fenceIndent));
          index += 1;
        }
        index += 1;
        const pre = el("pre", "rich-pre");
        const code = el("code");
        // Fenced content is literal by construction: it is never handed to
        // the inline parser, so HTML- or Markdown-looking text stays text.
        code.append(textNode(body.join("\n")));
        pre.append(code);
        parent.append(pre);
        continue;
      }

      const formula = mathBlock(lines, index, misses);
      if (formula) {
        appendMath(parent, formula, "div");
        index = formula.end;
        continue;
      }

      const heading = RICH_HEADING.exec(line);
      if (heading) {
        const node = el(`h${heading[1].length}`, "rich-heading");
        appendInline(node, (heading[2] ?? "").replace(/(^|[ \t]+)#+$/, ""), 0);
        parent.append(node);
        index += 1;
        continue;
      }

      if (RICH_RULE.test(line)) {
        parent.append(el("hr", "rich-rule"));
        index += 1;
        continue;
      }

      if (RICH_QUOTE.test(line)) {
        const quoted = [];
        while (index < lines.length && RICH_QUOTE.test(lines[index])) {
          quoted.push(RICH_QUOTE.exec(lines[index])[1]);
          index += 1;
        }
        const quote = el("blockquote", "rich-quote");
        appendBlocks(quote, quoted, depth + 1);
        parent.append(quote);
        continue;
      }

      const item = RICH_LIST_ITEM.exec(line);
      if (item && item[1].length <= 3) {
        index = appendList(parent, lines, index, depth);
        continue;
      }

      if (isTableStart(line, lines[index + 1])) {
        index = appendTable(parent, lines, index);
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !isBlank(lines[index]) && !startsBlock(lines[index], lines[index + 1]) &&
             !mathBlock(lines, index, misses)) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      const node = el("p", "rich-p");
      appendInline(node, paragraph.join("\n"), 0);
      parent.append(node);
    }
  }

  const container = el("div", "item-text rich-text");
  const lines = String(text ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(/^\t+/, (tabs) => "    ".repeat(tabs.length)));
  appendBlocks(container, lines, 0);
  if (inlineMath) watchMathWidth(doc, container);
  return container;
}
