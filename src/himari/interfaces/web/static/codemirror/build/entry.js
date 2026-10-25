/* HIMARI's CodeMirror 6 bundle (V3-P75 §7.6): a text editor and a two-way
 * diff, themed by the page's own CSS variables (no colours of its own).
 *
 * Each is placed in a shadow root of its parent: CodeMirror's styles then go
 * in as constructed style sheets (adoptedStyleSheets), which the page's CSP
 * (no inline styles) does not block; in the document itself they would be a
 * <style> element it does. CSS variables still reach inside. */
import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
         highlightSpecialChars } from "@codemirror/view";
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
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text)" },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--surface-selected)" },
  ".cm-cursor": { borderLeftColor: "var(--text)" },
  ".cm-panels": { backgroundColor: "var(--surface)", color: "var(--text)", borderColor: "var(--border-subtle)" },
  ".cm-searchMatch": { backgroundColor: "var(--warn-soft)", outline: "1px solid var(--warn-edge)" },
  ".cm-matchingBracket": { backgroundColor: "var(--accent-soft)" },
});

function basics(name) {
  return [lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(), drawSelection(),
          indentOnInput(), bracketMatching(), highlightActiveLine(), highlightSelectionMatches(),
          syntaxHighlighting(highlight), theme, EditorView.lineWrapping, languageFor(name)];
}

/* An editor in ``parent``: text, the file's name (its language), and
 * onChange(text) / onSave() callbacks; Mod-s saves. */
export function createEditor(parent, { text, name, readOnly = false, onChange = null, onSave = null }) {
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
                   ...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
        editable.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
        EditorView.updateListener.of((update) => { if (update.docChanged && onChange) onChange(); }),
      ],
    }),
  });
  return {
    view,
    text: () => view.state.doc.toString(),
    setText(next) { view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } }); },
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
