/* HIMARI's CodeMirror 6 bundle (V3-P75 §7.6): a text editor and a two-way
 * diff, themed by the page's own CSS variables (no colours of its own).
 *
 * Each is placed in a shadow root of its parent: CodeMirror's styles then go
 * in as constructed style sheets (adoptedStyleSheets), which the page's CSP
 * (no inline styles) does not block; in the document itself they would be a
 * <style> element it does. CSS variables still reach inside. */
import { EditorState, Compartment, StateField } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
         highlightSpecialChars, showTooltip } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { syntaxHighlighting, HighlightStyle, indentOnInput, bracketMatching } from "@codemirror/language";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { MergeView } from "@codemirror/merge";
import { tags } from "@lezer/highlight";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { json } from "@codemirror/lang-json";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { cpp } from "@codemirror/lang-cpp";
import { java } from "@codemirror/lang-java";
import { rust } from "@codemirror/lang-rust";
import { sql } from "@codemirror/lang-sql";
import { xml } from "@codemirror/lang-xml";
import { yaml } from "@codemirror/lang-yaml";

const BY_EXTENSION = {
  js: () => javascript(), mjs: () => javascript(), cjs: () => javascript(), jsx: () => javascript({ jsx: true }),
  ts: () => javascript({ typescript: true }), tsx: () => javascript({ typescript: true, jsx: true }),
  py: () => python(), pyi: () => python(), md: () => markdown(), markdown: () => markdown(),
  json: () => json(), html: () => html(), htm: () => html(), css: () => css(),
  c: () => cpp(), h: () => cpp(), cc: () => cpp(), cpp: () => cpp(), cxx: () => cpp(), hpp: () => cpp(),
  java: () => java(), rs: () => rust(), sql: () => sql(), xml: () => xml(), svg: () => xml(),
  yml: () => yaml(), yaml: () => yaml(),
};

function shadow(parent) {
  const root = parent.shadowRoot || parent.attachShadow({ mode: "open" });
  root.replaceChildren();
  return root;
}

export function languageFor(name) {
  const dot = name.lastIndexOf(".");
  const make = dot > 0 ? BY_EXTENSION[name.slice(dot + 1).toLowerCase()] : null;
  return make ? make() : [];
}

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.operatorKeyword, tags.modifier], color: "var(--code-keyword, #7c3aed)" },
  { tag: [tags.string, tags.special(tags.string), tags.regexp], color: "var(--code-string, #15803d)" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: "var(--text-muted)", fontStyle: "italic" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: "var(--code-number, #b45309)" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: "var(--code-function, #1d4ed8)" },
  { tag: [tags.typeName, tags.className, tags.namespace], color: "var(--code-type, #0e7490)" },
  { tag: [tags.heading], fontWeight: "700" },
  { tag: [tags.emphasis], fontStyle: "italic" },
  { tag: [tags.strong], fontWeight: "700" },
  { tag: [tags.link, tags.url], textDecoration: "underline" },
  { tag: [tags.tagName, tags.attributeName], color: "var(--code-type, #0e7490)" },
]);

const theme = EditorView.theme({
  "&": { color: "var(--text)", backgroundColor: "var(--bg)", fontSize: "13px", height: "100%" },
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.6" },
  ".cm-content": { caretColor: "var(--text)", padding: "8px 0" },
  ".cm-gutters": { backgroundColor: "var(--bg)", color: "var(--text-muted)", border: "none", opacity: "0.7" },
  ".cm-activeLine": { backgroundColor: "var(--surface)" },
  // The active line's background is drawn over the selection layer: none while text is selected.
  "&.cm-has-selection .cm-activeLine": { backgroundColor: "transparent" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text)" },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in srgb, var(--accent) 20%, transparent)" },
  ".cm-cursor": { borderLeftColor: "var(--text)" },
  ".cm-panels": { backgroundColor: "var(--surface)", color: "var(--text)", borderColor: "var(--border-subtle)" },
  ".cm-searchMatch": { backgroundColor: "var(--warn-soft)", outline: "1px solid var(--warn-edge)" },
  ".cm-matchingBracket": { backgroundColor: "var(--accent-soft)" },
  ".cm-selectionMatch": { backgroundColor: "color-mix(in srgb, var(--accent) 8%, transparent)" },
  ".cm-tooltip.cm-ask": { border: "1px solid var(--border-strong)", borderRadius: "999px",
                          backgroundColor: "var(--surface-raised)", boxShadow: "0 2px 8px rgba(0, 0, 0, 0.12)" },
  ".cm-ask button": { font: "inherit", fontFamily: "var(--sans, inherit)", fontSize: "12px", color: "var(--accent)",
                      background: "transparent", border: "none", padding: "4px 12px", minHeight: "28px",
                      cursor: "pointer" },
});

function basics(name) {
  return [lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(), drawSelection(),
          indentOnInput(), bracketMatching(), highlightActiveLine(), highlightSelectionMatches(),
          syntaxHighlighting(highlight), theme, EditorView.lineWrapping, languageFor(name),
          EditorView.editorAttributes.compute(["selection"],
                                              (state) => (state.selection.main.empty ? {} : { class: "cm-has-selection" }))];
}

/* V3-P76 §3.1: "Ask HIMARI" at the end of a selection, a tooltip that calls onAsk(). */
function askTooltip(onAsk) {
  const tooltip = (state) => {
    const range = state.selection.main;
    if (range.empty || state.selection.ranges.length > 1) return null;
    return {
      pos: range.head, above: range.head < range.anchor, arrow: false,
      create() {
        const dom = document.createElement("div");
        dom.className = "cm-ask";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Ask HIMARI";
        button.title = "Point HIMARI at this selection (Ctrl/Cmd+Shift+Enter)";
        button.addEventListener("mousedown", (event) => event.preventDefault()); // the selection stays
        button.addEventListener("click", () => onAsk());
        dom.append(button);
        return { dom };
      },
    };
  };
  return StateField.define({
    create: tooltip,
    update(value, tr) { return tr.docChanged || tr.selection ? tooltip(tr.state) : value; },
    provide: (field) => showTooltip.from(field),
  });
}

/* An editor in ``parent``: text, the file's name (its language), and
 * onChange(text) / onSave() / onAsk() callbacks; Mod-s saves, Mod-Shift-Enter
 * asks (V3-P76: with onAsk, also a tooltip on a selection). Positions are
 * CodeMirror's: UTF-16 code units, every line break (\r\n, \r, \n) one "\n". */
export function createEditor(parent, { text, name, readOnly = false, onChange = null, onSave = null, onAsk = null }) {
  const editable = new Compartment();
  const root = shadow(parent);
  const view = new EditorView({
    parent: root,
    root,
    state: EditorState.create({
      doc: text,
      extensions: [
        basics(name),
        keymap.of([{ key: "Mod-s", preventDefault: true, run: () => { if (onSave) onSave(); return true; } },
                   ...(onAsk ? [{ key: "Mod-Shift-Enter", preventDefault: true, run: () => { onAsk(); return true; } }] : []),
                   ...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
        onAsk ? askTooltip(onAsk) : [],
        editable.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
        EditorView.updateListener.of((update) => { if (update.docChanged && onChange) onChange(); }),
      ],
    }),
  });
  return {
    view,
    text: () => view.state.doc.toString(),
    setText(next) { view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } }); },
    /* The main selection: its positions, its text (lines joined by "\n"), and where it starts and ends
     * as 1-based lines and columns. */
    selection() {
      const { from, to } = view.state.selection.main;
      const doc = view.state.doc;
      const first = doc.lineAt(from);
      const last = doc.lineAt(to);
      return { from, to, text: doc.sliceString(from, to), fromLine: first.number, fromColumn: from - first.from + 1,
               toLine: last.number, toColumn: to - last.from + 1 };
    },
    /* Selects ``from``–``to`` (cut to the text) and shows it. */
    select(from, to) {
      const length = view.state.doc.length;
      const anchor = Math.max(0, Math.min(from, length));
      view.dispatch({ selection: { anchor, head: Math.max(anchor, Math.min(to, length)) }, scrollIntoView: true });
      view.focus();
    },
    setReadOnly(on) {
      view.dispatch({ effects: editable.reconfigure([EditorState.readOnly.of(on), EditorView.editable.of(!on)]) });
    },
    focus() { view.focus(); },
    destroy() { view.destroy(); },
  };
}

/* A read-only side-by-side diff: ``left`` (e.g. on disk) and ``right`` (e.g. yours). */
export function createDiff(parent, { left, right, name }) {
  const side = (doc) => ({ doc, extensions: [basics(name), EditorState.readOnly.of(true), EditorView.editable.of(false)] });
  const root = shadow(parent);
  const merge = new MergeView({ a: side(left), b: side(right), parent: root, root,
                                collapseUnchanged: { margin: 3, minSize: 6 } });
  return { destroy() { merge.destroy(); } };
}
