/* Formula checks for the assistant text renderer (rich_text.js). No browser,
 * packages, or live service: a small fake DOM, a stand-in KaTeX that writes
 * what it was given, and the vendored KaTeX itself for its options.
 * Usage: node scripts/rich_text_smoke.mjs */
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const { renderAssistantText, assistantTextLine } = await import("../src/himari/interfaces/web/static/rich_text.js");

class FakeNode {
  constructor(tag, text = null) {
    this.tagName = tag;
    this.text = text;
    this.children = [];
    this.parent = null;
    this.className = "";
    this.attributes = {};
    const node = this;
    this.classList = {
      add: (name) => { if (!node.classes().includes(name)) node.className = [...node.classes(), name].join(" "); },
      remove: (name) => { node.className = node.classes().filter((c) => c !== name).join(" "); },
      contains: (name) => node.classes().includes(name),
    };
  }
  classes() { return this.className.split(" ").filter(Boolean); }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
  appendChild(n) { this.append(n); return n; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  replaceWith(n) {
    const at = this.parent.children.indexOf(this);
    n.parent = this.parent;
    this.parent.children[at] = n;
  }
  get textContent() { return this.text ?? this.children.map((c) => c.textContent).join(""); }
  set textContent(value) { this.children = []; this.text = null; if (value) this.append(new FakeNode("#text", value)); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(selector) { return this.all().filter((n) => n.classList.contains(selector.slice(1))); }
}

const painted = [];
const fakeKatex = {
  render(tex, node, options) {
    painted.push({ tex, options });
    if (tex.includes("\\refuse")) throw new Error("ParseError");
    node.textContent = "";
    node.append(new FakeNode("#text", `<${options.displayMode ? "D" : "I"}:${tex}>`));
  },
};
const doc = {
  defaultView: { katex: fakeKatex },
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
};

const render = (text) => renderAssistantText(doc, text);
const shown = (text) => render(text).textContent;
const formulas = (text) => render(text).querySelectorAll(".rich-math");

// Inline and displayed delimiters; TeX reaches KaTeX untouched by Markdown.
assert.equal(shown("Area $\\pi r^2$ here"), "Area <I:\\pi r^2> here");
assert.equal(shown("so \\(a_1 + b_1\\) and \\(x*y*z\\)"), "so <I:a_1 + b_1> and <I:x*y*z>");
assert.equal(shown("$$\\frac{a}{b}$$"), "<D:\\frac{a}{b}>");
assert.equal(shown("where \\[\\{x \\mid x > 0\\}\\] holds"), "where <D:\\{x \\mid x > 0\\}> holds");
assert.equal(shown("$a \\\\ b$"), "<I:a \\\\ b>");
assert.equal(shown("$\\$5$"), "<I:\\$5>"); // an escaped $ does not close

// Displayed blocks on their own lines, with lines that look like Markdown blocks.
{
  const node = render("The sum:\n$$\n- a\n| b |\n# c\n$$\nwhere a > 0.");
  assert.deepEqual(node.children.map((c) => c.tagName), ["p", "div", "p"]);
  assert.equal(node.children[1].textContent, "<D:\n- a\n| b |\n# c\n>");
  assert.equal(node.children[2].textContent, "where a > 0.");
}
assert.equal(shown("\\[\n\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}\n\\]"),
  "<D:\n\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}\n>");
assert.equal(shown("- item $x_1$\n- next"), "item <I:x_1>next");
// A blank line ends the search: an unclosed $$ does not swallow what follows.
assert.equal(formulas("$$\na\n\nb $$ c").length, 0);

// Prices, code and escapes stay text.
assert.equal(shown("costs $5 and $10"), "costs $5 and $10");
assert.equal(shown("from $5-$10 a day"), "from $5-$10 a day");
assert.equal(shown("pay $5, or $x$"), "pay $5, or <I:x>");
assert.equal(shown("use `$x$` and `\\(y\\)`"), "use $x$ and \\(y\\)");
assert.equal(shown("```\n$$a$$\n```"), "$$a$$");
assert.equal(shown("\\$x\\$"), "$x$");
assert.equal(shown("$ x$ and $y $"), "$ x$ and $y $");
assert.equal(shown("$$ $$"), "$$ $$");

// Emphasis and links do not close inside a formula.
assert.equal(shown("*see $a*b$ now*"), "see <I:a*b> now");
assert.equal(render("*see $a*b$ now*").children[0].children[0].tagName, "em");
assert.equal(shown("[$x_1$](https://example.com/)"), "<I:x_1>");

// A refused or too long formula shows as written.
{
  const [math] = formulas("$\\refuse x$");
  assert.equal(math.textContent, "$\\refuse x$");
  assert.ok(math.classList.contains("is-refused"));
  const count = painted.length;
  const [long] = formulas(`$${"x".repeat(4001)}$`);
  assert.ok(long.classList.contains("is-refused") && painted.length === count);
}

// The options every formula is given.
{
  painted.length = 0;
  render("$x$");
  const { options } = painted[0];
  assert.equal(options.trust, false);
  assert.equal(options.throwOnError, true);
  assert.equal(options.displayMode, false);
}

// The summary line shows formulas as written.
assert.equal(assistantTextLine(doc, "**Plan:** solve $x^2 = 4$"), "Plan: solve $x^2 = 4$");

// No model value reaches an attribute; the TeX stays off the DOM.
for (const math of formulas("$x$ \\(y\\) $$z$$")) assert.deepEqual(math.attributes, {});

// Adversarial input stays fast (linear searches).
{
  const started = Date.now();
  render("$a ".repeat(20000));
  render("\\(".repeat(20000));
  render("\\[\n".repeat(5000));
  render("*$a*".repeat(10000));
  assert.ok(Date.now() - started < 2000, `slow: ${Date.now() - started} ms`);
}

// Before KaTeX is there: the formula's own text, marked pending, then painted.
{
  let finish;
  const head = new FakeNode("head");
  const view = {};
  const later = { defaultView: view, head, createElement: doc.createElement, createTextNode: doc.createTextNode };
  const [math] = renderAssistantText(later, "$y$").querySelectorAll(".rich-math");
  assert.equal(math.textContent, "$y$");
  assert.ok(math.classList.contains("is-pending"));
  const [style, script] = head.children;
  assert.equal(style.href, "/assets/katex/katex.min.css");
  assert.equal(script.src, "/assets/katex/katex.min.js");
  view.katex = fakeKatex;
  script.onload();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(math.textContent, "<I:y>");
  assert.ok(!math.classList.contains("is-pending"));
}

// The vendored KaTeX with these options: no links or HTML from TeX, Korean
// text accepted, a bad formula refused.
{
  const katex = createRequire(import.meta.url)("../src/himari/interfaces/web/static/katex/katex.min.js");
  const options = { throwOnError: true, trust: false, strict: "ignore", maxSize: 20, maxExpand: 500 };
  const html = katex.renderToString("\\href{javascript:alert(1)}{x} \\url{https://a.example}", options);
  assert.ok(!html.includes("<a ") && !html.includes("href=")); // shown as unsupported commands, in red
  assert.ok(!katex.renderToString("\\htmlId{x}{y}", { ...options, throwOnError: false }).includes('id="x"'));
  assert.ok(katex.renderToString("\\text{넓이} = \\pi r^2", options).includes("넓이"));
  assert.throws(() => katex.renderToString("\\frac{a", options));
  assert.throws(() => katex.renderToString("\\def\\a{\\a\\a}\\a", options));
}

console.log("rich_text formulas: ok");
