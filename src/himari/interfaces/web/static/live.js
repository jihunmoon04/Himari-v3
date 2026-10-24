/* The answer HIMARI is writing (V3-P73 §7): one item at the end of the
 * timeline, fed by GET …/live (Server-Sent Events) while a job runs.
 *
 * - Display only. The item is replaced by the Run's own Event when polling
 *   brings it; a Run that ended without one (stopped, cut off, refused) stays
 *   dimmed where it was, with a note that it was not kept, until the page
 *   reloads; the next Run gets an item of its own.
 * - Before the first text: the thinking line (§7.4). A glyph moves, light
 *   passes over one word chosen when the line starts, and the seconds count.
 *   The words and motion come from the theme: its --thinking-style is "sun"
 *   (light themes) or "hidamari" (dark ones); unknown or unset is "sun".
 *   Reduced motion keeps the word and the seconds, still.
 * - Text goes through the same renderer as finished answers, at most once a
 *   frame and every PAINT_MS; the view follows it only when it was at the
 *   bottom.
 */

export const THINKING_WORDS = {
  sun: ["Turning to the sun", "Blooming", "Basking", "Unfurling", "Gathering light", "Following the sun",
        "Soaking up sunlight", "Opening petals"],
  hidamari: ["Sitting in the sun", "Mulling it over", "Brewing tea", "Warming up", "Curled up thinking",
             "Resting in the warm spot"],
};
export const SUN_GLYPHS = ["·", "✢", "✣", "❋", "✾", "❀"];
export const HIDAMARI_GLYPH = "●";
export const GLYPH_MS = 120;
export const PAINT_MS = 100;
export const RETRY_AFTER_MS = 10000; // after a refused or failed stream: polling still shows the answer
export const STOPPED_NOTE = "Stopped — this part was not kept, and HIMARI will not see it.";

export function thinkingStyle(value) {
  const style = String(value || "").trim();
  return style === "hidamari" ? "hidamari" : "sun";
}

/* The word for a thinking line: one index for the line, so a theme change
 * keeps its place in the other list instead of jumping around. */
export function thinkingWord(style, index) {
  const words = THINKING_WORDS[thinkingStyle(style)];
  return words[index % words.length];
}

/* The sun glyph at a frame: opening, then closing (· ✢ ✣ ❋ ✾ ❀ ✾ ❋ ✣ ✢). */
export function sunGlyph(frame) {
  const span = SUN_GLYPHS.length - 1;
  const at = frame % (2 * span);
  return SUN_GLYPHS[at <= span ? at : 2 * span - at];
}

export function elapsedText(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

/* The live state after one stream event; null for none. A delta or end of
 * another Run changes nothing: the stream resynchronizes with a snapshot. */
export function reduce(state, kind, data) {
  if (kind === "snapshot") {
    return { runId: data.run_id, text: data.text || "", tools: [...(data.tools || [])], thinking: !!data.thinking,
             truncated: !!data.truncated, ended: !!data.ended };
  }
  if (!state || !data || data.run_id !== state.runId) return state;
  if (kind === "delta") {
    return { ...state, text: state.text + (data.text || ""), tools: [...state.tools, ...(data.tools || [])],
             thinking: state.thinking || !!data.thinking, truncated: state.truncated || !!data.truncated };
  }
  if (kind === "end") return { ...state, ended: true };
  return state;
}

export class LiveAnswer {
  /* options: document, timeline() → its element, header() → the item's
   * header node, render(text) → rendered answer text, style() → the theme's
   * --thinking-style, reducedMotion() → bool, now() → ms, random() → [0, 1),
   * EventSource (the constructor). */
  constructor(options) {
    this.o = options;
    this.source = null;
    this.url = null;
    this.failedAt = null;
    this.state = null;
    this.node = null;
    this.answered = new Set(); // Runs whose own Event is shown
    this.since = new Map(); // Run → when its thinking line started
    this.word = 0;
    this.frame = 0;
    this.ticker = null;
    this.paintTimer = null;
    this.lastPaint = 0;
    this.stopped = false;
  }

  /* While a job runs: listen, unless a stream is open or one failed lately. */
  open(url) {
    if (this.source && this.url === url) return;
    if (this.failedAt !== null && this.o.now() - this.failedAt < RETRY_AFTER_MS) return;
    this.close();
    this.url = url;
    const source = new this.o.EventSource(url);
    this.source = source;
    for (const kind of ["snapshot", "delta", "end", "idle"]) {
      source.addEventListener(kind, (event) => {
        if (this.source !== source) return;
        let data = null;
        try { data = JSON.parse(event.data); } catch (ignored) { return; }
        this.take(kind, data);
      });
    }
    source.addEventListener("done", () => { if (this.source === source) this.close(); });
    source.addEventListener("error", () => {
      // A refusal (404, 429, a lost session) closes it for good; a stream the
      // server ended at its time limit reconnects by itself.
      if (this.source === source && source.readyState === 2) {
        this.source = null;
        this.failedAt = this.o.now();
      }
    });
  }

  close() {
    if (this.source) this.source.close();
    this.source = null;
  }

  /* Another conversation, or none: nothing of this one stays. */
  reset() {
    this.close();
    this.url = null;
    this.failedAt = null;
    this.state = null;
    this.answered.clear();
    this.since.clear();
    this.stopped = false;
    this.remove();
  }

  take(kind, data) {
    const before = this.state;
    const next = reduce(before, kind, data);
    if (next === before) return;
    if (next && this.answered.has(next.runId)) { // its Event is already shown
      this.state = null;
      this.remove();
      return;
    }
    if (!before || !next || before.runId !== next.runId) {
      if (this.stopped) this.leave();
      this.stopped = false;
      this.word = Math.floor(this.o.random() * 1000);
    }
    this.state = next;
    this.schedule();
  }

  /* The Run's own Event is in the timeline now. */
  shown(runId) {
    if (!runId) return;
    this.answered.add(runId);
    if (this.state && this.state.runId === runId) {
      this.state = null;
      this.remove();
    }
  }

  /* The job ended and its Events are in: an unanswered Run was stopped. */
  settle(running) {
    if (running || !this.state || this.stopped) return;
    if (!this.state.text && !this.state.tools.length) { // nothing was written: nothing to keep
      this.state = null;
      this.remove();
      return;
    }
    this.stopped = true;
    this.paint();
  }

  /* After Events were added: the item being written stays last (a stopped
   * one stays where it was). */
  keepLast() {
    if (this.node && this.node.parentNode && !this.stopped) this.o.timeline().append(this.node);
  }

  /* A stopped item stays in the timeline, no longer this one's. */
  leave() {
    this.stopTicker();
    this.node = null;
  }

  /* The theme changed: the thinking line follows at once. */
  restyle() {
    if (this.node && this.thinking()) this.paint();
  }

  thinking() {
    return this.state && !this.stopped && !this.state.text && !this.state.tools.length;
  }

  remove() {
    this.stopTicker();
    if (this.paintTimer !== null) clearTimeout(this.paintTimer);
    this.paintTimer = null;
    if (this.node) this.node.remove();
    this.node = null;
  }

  schedule() {
    if (this.paintTimer !== null) return;
    const wait = Math.max(0, PAINT_MS - (this.o.now() - this.lastPaint));
    this.paintTimer = setTimeout(() => {
      this.paintTimer = null;
      (this.o.frame || ((fn) => fn()))(() => this.paint());
    }, wait);
  }

  paint() {
    const state = this.state;
    if (!state) return;
    this.lastPaint = this.o.now();
    const doc = this.o.document;
    const timeline = this.o.timeline();
    const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    if (!this.node) {
      this.node = doc.createElement("div");
      timeline.append(this.node);
    }
    const node = this.node;
    node.className = `item item-assistant is-live${this.stopped ? " is-stopped" : ""}`;
    node.dataset.run = state.runId;
    const parts = [this.o.header()];
    if (this.thinking()) {
      parts.push(this.thinkingLine(state.runId));
    } else {
      this.stopTicker();
      if (state.text) parts.push(this.o.render(state.text));
      const tool = state.tools[state.tools.length - 1];
      if (tool && !this.stopped && !state.ended) parts.push(this.line("live-tool", `Preparing ${tool}…`));
      if (state.truncated) parts.push(this.line("live-note", "The rest is shown when the answer is done."));
      if (this.stopped) parts.push(this.line("live-note", STOPPED_NOTE));
    }
    node.replaceChildren(...parts);
    if (atBottom) timeline.scrollTop = timeline.scrollHeight;
  }

  line(className, text) {
    const node = this.o.document.createElement("div");
    node.className = className;
    node.textContent = text;
    return node;
  }

  thinkingLine(runId) {
    const doc = this.o.document;
    if (!this.since.has(runId)) this.since.set(runId, this.o.now());
    const style = thinkingStyle(this.o.style());
    const line = doc.createElement("div");
    line.className = `thinking is-${style}`;
    line.setAttribute("role", "status");
    const said = doc.createElement("span");
    said.className = "visually-hidden";
    said.textContent = "HIMARI is thinking";
    const glyph = doc.createElement("span");
    glyph.className = "thinking-glyph";
    const word = doc.createElement("span");
    word.className = "thinking-word";
    word.textContent = `${thinkingWord(style, this.word)}…`;
    const time = doc.createElement("span");
    time.className = "thinking-time";
    for (const part of [glyph, word, time]) part.setAttribute("aria-hidden", "true");
    line.append(said, glyph, word, time);
    const tick = () => {
      glyph.textContent = style === "sun" ? sunGlyph(this.o.reducedMotion() ? 4 : this.frame) : HIDAMARI_GLYPH;
      time.textContent = `· ${elapsedText(this.o.now() - this.since.get(runId))}`;
      this.frame += 1;
    };
    tick();
    this.stopTicker();
    this.ticker = setInterval(tick, GLYPH_MS);
    return line;
  }

  stopTicker() {
    if (this.ticker !== null) clearInterval(this.ticker);
    this.ticker = null;
  }
}
