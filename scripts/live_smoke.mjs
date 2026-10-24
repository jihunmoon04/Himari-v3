/* Checks for the answer being written (live.js, V3-P73 §7). No browser,
 * packages, or live service: a small fake DOM and a fake EventSource.
 * Usage: node scripts/live_smoke.mjs */
import assert from "node:assert/strict";

const live = await import("../src/himari/interfaces/web/static/live.js");
const { LiveAnswer, reduce, thinkingStyle, thinkingWord, sunGlyph, elapsedText, THINKING_WORDS, STOPPED_NOTE } = live;
let passed = 0;
const check = async (name, body) => { await body(); passed += 1; };
const later = (ms = live.PAINT_MS + 30) => new Promise((resolve) => setTimeout(resolve, ms));

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n.parentNode) n.parentNode.children = n.parentNode.children.filter((c) => c !== n);
      n.parentNode = this;
      this.children.push(n);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); this.parentNode = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  text() { return this.textContent + this.children.map((c) => c.text()).join(""); }
  find(className) {
    if (this.className.split(" ").includes(className)) return this;
    for (const c of this.children) { const f = c.find(className); if (f) return f; }
    return null;
  }
}

class FakeSource {
  static made = [];
  constructor(url) { this.url = url; this.listeners = {}; this.readyState = 1; this.closed = false; FakeSource.made.push(this); }
  addEventListener(kind, fn) { (this.listeners[kind] ||= []).push(fn); }
  emit(kind, data) { for (const fn of this.listeners[kind] || []) fn({ data: JSON.stringify(data) }); }
  close() { this.closed = true; this.readyState = 2; }
}

function setup(overrides = {}) {
  const timeline = new FakeNode("div");
  const clock = { now: 1000 };
  const options = {
    document: { createElement: (tag) => new FakeNode(tag) },
    timeline: () => timeline,
    header: () => { const h = new FakeNode("div"); h.className = "item-kind"; h.textContent = "HIMARI"; return h; },
    render: (text) => { const n = new FakeNode("div"); n.className = "rich-text"; n.textContent = text; return n; },
    style: () => "sun",
    reducedMotion: () => false,
    now: () => clock.now,
    random: () => 0.0025, // word index 2
    frame: (fn) => fn(),
    EventSource: FakeSource,
    ...overrides,
  };
  return { answer: new LiveAnswer(options), timeline, clock, options };
}

const snapshot = (runId, text = "", extra = {}) => ({ run_id: runId, text, tools: [], thinking: false,
                                                      truncated: false, ended: false, ...extra });

await check("reduce", () => {
  let state = reduce(null, "snapshot", snapshot("r1", "Hel"));
  state = reduce(state, "delta", { run_id: "r1", text: "lo", tools: ["echo"], thinking: true });
  assert.deepEqual(state, { runId: "r1", text: "Hello", tools: ["echo"], thinking: true, truncated: false, ended: false });
  assert.equal(reduce(state, "delta", { run_id: "r2", text: "x", tools: [] }), state); // another Run's: resynced by a snapshot
  assert.equal(reduce(state, "end", { run_id: "r1" }).ended, true);
  assert.equal(reduce(null, "delta", { run_id: "r1", text: "x" }), null);
  assert.equal(reduce(state, "idle", {}), state);
});

await check("words, glyphs and seconds", () => {
  assert.equal(thinkingStyle(" hidamari "), "hidamari");
  assert.equal(thinkingStyle(""), "sun");
  assert.equal(thinkingStyle("neon"), "sun");
  assert.equal(thinkingWord("sun", 2), THINKING_WORDS.sun[2]);
  assert.equal(thinkingWord("hidamari", 2), THINKING_WORDS.hidamari[2]); // the same place in the other list
  assert.equal(thinkingWord("sun", 1002), THINKING_WORDS.sun[1002 % THINKING_WORDS.sun.length]);
  assert.deepEqual([0, 1, 5, 6, 9, 10].map(sunGlyph), ["·", "✢", "❀", "✾", "✢", "·"]); // opens, then closes
  assert.deepEqual([999, 12000, 59999, 60000, 65000, 3600000].map(elapsedText),
                   ["0s", "12s", "59s", "1m 00s", "1m 05s", "60m 00s"]);
});

await check("a stream shows the thinking line, then the text, and goes when its Event comes", async () => {
  const { answer, timeline, clock } = setup();
  answer.open("/api/conversations/c1/live");
  const source = FakeSource.made.at(-1);
  assert.equal(source.url, "/api/conversations/c1/live");
  source.emit("snapshot", snapshot("r1"));
  await later();
  const item = timeline.children[0];
  assert.match(item.className, /is-live/);
  const line = item.find("thinking");
  assert.equal(line.className, "thinking is-sun");
  assert.equal(line.attributes.role, "status");
  assert.equal(item.find("visually-hidden").textContent, "HIMARI is thinking");
  assert.equal(item.find("thinking-word").textContent, `${THINKING_WORDS.sun[2]}…`);
  clock.now += 12500;
  await later(live.GLYPH_MS + 20);
  assert.equal(item.find("thinking-time").textContent, "· 12s");
  assert.equal(item.find("thinking-glyph").attributes["aria-hidden"], "true");
  source.emit("delta", { run_id: "r1", text: "Hello", tools: [] });
  source.emit("delta", { run_id: "r1", text: ", Seoul", tools: ["inside/write_file"] });
  await later();
  assert.equal(item.find("thinking"), null);
  assert.equal(item.find("rich-text").textContent, "Hello, Seoul");
  assert.equal(item.find("live-tool").textContent, "Preparing inside/write_file…");
  const done = new FakeNode("div"); // an Event added after it: the item moves back to the end
  timeline.append(done);
  answer.keepLast();
  assert.equal(timeline.children.at(-1), item);
  answer.shown("r1");
  assert.equal(timeline.children.includes(item), false);
  source.emit("snapshot", snapshot("r1", "Hello, Seoul", { ended: true })); // a reconnect after it was shown
  await later();
  assert.equal(timeline.children.length, 1);
  source.emit("done", {});
  assert.equal(source.closed, true);
  answer.reset();
});

await check("a Run that ended without its Event stays dimmed with its note", async () => {
  const { answer, timeline } = setup();
  answer.open("/live");
  const source = FakeSource.made.at(-1);
  source.emit("snapshot", snapshot("r1", "Half an ans"));
  source.emit("end", { run_id: "r1" });
  await later();
  answer.settle(true); // still running: nothing yet
  assert.doesNotMatch(timeline.children[0].className, /is-stopped/);
  answer.settle(false);
  const item = timeline.children[0];
  assert.match(item.className, /is-stopped/);
  assert.equal(item.find("live-note").textContent, STOPPED_NOTE);
  assert.equal(item.find("rich-text").textContent, "Half an ans");
  const later_message = new FakeNode("div"); // what comes after stays after it
  timeline.append(later_message);
  answer.keepLast();
  assert.equal(timeline.children.at(-1), later_message);
  source.emit("snapshot", snapshot("r2", "Next")); // the next Run: an item of its own
  await later();
  assert.equal(timeline.children.length, 3);
  assert.match(timeline.children[0].className, /is-stopped/);
  assert.equal(timeline.children[2].find("rich-text").textContent, "Next");
  answer.reset();
  assert.equal(timeline.children.length, 2); // the stopped one stays until the page reloads
});

await check("a Run stopped before it wrote anything leaves nothing", async () => {
  const { answer, timeline } = setup();
  answer.open("/live");
  FakeSource.made.at(-1).emit("snapshot", snapshot("r1", "", { thinking: true }));
  await later();
  assert.equal(timeline.children.length, 1);
  answer.settle(false);
  assert.equal(timeline.children.length, 0);
  answer.reset();
});

await check("the thinking line follows the theme at once, at the same word", async () => {
  let style = "sun";
  const { answer, timeline } = setup({ style: () => style });
  answer.open("/live");
  FakeSource.made.at(-1).emit("snapshot", snapshot("r1"));
  await later();
  style = "hidamari";
  answer.restyle();
  const item = timeline.children[0];
  assert.equal(item.find("thinking").className, "thinking is-hidamari");
  assert.equal(item.find("thinking-word").textContent, `${THINKING_WORDS.hidamari[2]}…`);
  assert.equal(item.find("thinking-glyph").textContent, live.HIDAMARI_GLYPH);
  answer.reset();
});

await check("reduced motion keeps one glyph still", async () => {
  const { answer, timeline } = setup({ reducedMotion: () => true });
  answer.open("/live");
  FakeSource.made.at(-1).emit("snapshot", snapshot("r1"));
  await later(live.PAINT_MS + 3 * live.GLYPH_MS);
  assert.equal(timeline.children[0].find("thinking-glyph").textContent, sunGlyph(4));
  answer.reset();
});

await check("a refused stream is not retried at once; polling still shows the answer", async () => {
  const { answer, clock } = setup();
  answer.open("/live");
  const first = FakeSource.made.at(-1);
  first.readyState = 2; // the browser closed it (404, 429, a lost session)
  for (const fn of first.listeners.error) fn({});
  const count = FakeSource.made.length;
  answer.open("/live");
  assert.equal(FakeSource.made.length, count);
  clock.now += live.RETRY_AFTER_MS;
  answer.open("/live");
  assert.equal(FakeSource.made.length, count + 1);
  answer.open("/live"); // one open stream at a time
  assert.equal(FakeSource.made.length, count + 1);
  answer.reset();
});

console.log(`live answer checks: ${passed} passed`);
