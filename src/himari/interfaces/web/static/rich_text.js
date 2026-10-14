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
 *  The only attribute a model value can reach is a link `href`, and only
 *  after `new URL()` -- the browser's own parser, not a regex -- has proven
 *  the scheme is in SAFE_LINK_PROTOCOLS. Relative, protocol-relative,
 *  `javascript:`, `data:` and `vbscript:` targets stay plain text. No image
 *  is ever created (so rendering never fetches anything), and bare URLs
 *  are not auto-linked.
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
const RICH_ESCAPABLE = "\\`*_{}[]()#+-.!|~<>\"'";

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

  /** Advance past an escape or a whole code span starting at `index`, so
   *  closer searches never see delimiters that are really literal code. */
  function skipLiteral(source, index, memo) {
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

  function splitRow(line) {
    let body = line.trim();
    if (body.startsWith("|")) body = body.slice(1);
    if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
    const cells = [];
    let current = "";
    for (let index = 0; index < body.length; index += 1) {
      if (body[index] === "\\" && body[index + 1] === "|") {
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
      while (index < lines.length && !isBlank(lines[index]) && !startsBlock(lines[index], lines[index + 1])) {
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
  return container;
}
