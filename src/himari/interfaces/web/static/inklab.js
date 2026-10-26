/* The ink lab (V3-P77 §4.6): what the pen gave and what drawing it cost, for
 * the last stroke, and perfect-freehand's settings to try — on the iPad, with
 * the owner, until the defaults are chosen. Off unless asked for: open the
 * app once with ?ink-lab (and ?ink-lab=0 to turn it off); it stays on in that
 * browser. While it is on, the settings tried there are kept there too and
 * used for every stroke drawn and saved in it.
 *
 * Nothing is shown while a stroke is drawn (that would cost a layout): the
 * numbers are the last stroke's, written when the pen lifts.
 *
 * No imports: app.js loads this at its start, for ?ink-lab; the settings'
 * functions (pdfink.js's) are handed over by the PDF view.
 */

const LAB_KEY = "himari.ink.lab";
const TUNING_KEY = "himari.ink.tuning";
// Input shown later than this after it came is late: a frame or two at 60 Hz (16.7 ms each), and a little.
const LATE_MS = 40;

/* ``search``'s ?ink-lab turns the lab on in this browser, ?ink-lab=0 off; read before the address is
 * rewritten (app.js, at its start). */
export function rememberLab(search) {
  try {
    const value = new URLSearchParams(search).get("ink-lab");
    if (value === null) return;
    if (value === "0" || value === "off") {
      localStorage.removeItem(LAB_KEY);
      localStorage.removeItem(TUNING_KEY);
    } else {
      localStorage.setItem(LAB_KEY, "1");
    }
  } catch (ignored) { /* no storage: no lab */ }
}

export function labOn() {
  try {
    return localStorage.getItem(LAB_KEY) === "1";
  } catch (ignored) {
    return false;
  }
}

/* The settings kept in this browser, put to use with ``tuning.set`` (as a view with the lab opens). */
function keptTuning(tuning) {
  try {
    const kept = JSON.parse(localStorage.getItem(TUNING_KEY) || "null");
    if (kept && typeof kept === "object") return tuning.set(kept);
  } catch (ignored) { /* the defaults */ }
  return tuning.current();
}

const SLIDERS = [
  ["thinning", "Thinning", "how much pressure changes the width", 0, 0.95],
  ["smoothing", "Smoothing", "how much the outline's edges are softened", 0, 1],
  ["streamline", "Streamline", "how far the line trails the pen to even it out", 0, 1],
];

export class InkLab {
  /* el(tag, class, text) as the view's; ``tuning``: {defaults, current(), set(values)}, pdfink.js's;
   * retuned() when a setting changed (the view paints its ink again). */
  constructor({ el, tuning, retuned }) {
    this.el = el;
    this.tuning = tuning;
    this.retuned = retuned;
    this.stroke = null;
    this.element = el("details", "ink-lab");
    this.element.open = true;
    const summary = el("summary", "ink-lab-title", "Ink lab");
    this.numbers = el("pre", "ink-lab-numbers", "Draw a stroke to see its numbers.");
    this.sliders = {};
    const settings = el("div", "ink-lab-settings");
    for (const [name, label, title, min, max] of SLIDERS) {
      const row = el("label", "ink-lab-row");
      row.title = title;
      const input = el("input", "ink-lab-slider");
      input.type = "range";
      input.min = String(min);
      input.max = String(max);
      input.step = "0.05";
      const value = el("span", "ink-lab-value", "");
      input.addEventListener("input", () => this.tune({ [name]: Number(input.value) }));
      row.append(el("span", "ink-lab-label", label), input, value);
      settings.append(row);
      this.sliders[name] = { input, value };
    }
    const reset = el("button", "link-button ink-lab-reset", "Defaults");
    reset.type = "button";
    reset.addEventListener("click", () => this.tune({ ...tuning.defaults }));
    settings.append(reset);
    this.element.append(summary, this.numbers, settings);
    this.show(keptTuning(tuning));
  }

  tune(change) {
    const now = this.tuning.set({ ...this.tuning.current(), ...change });
    try {
      localStorage.setItem(TUNING_KEY, JSON.stringify(now));
    } catch (ignored) { /* for this page only */ }
    this.show(now);
    this.retuned();
  }

  show(tuning) {
    for (const [name, { input, value }] of Object.entries(this.sliders)) {
      input.value = String(tuning[name]);
      value.textContent = tuning[name].toFixed(2);
    }
  }

  /* --------------------------------------------------------- one stroke */

  /* A stroke begun with the pointerdown ``event`` (its point the stroke's first). */
  begin(event) {
    this.stroke = { pointerType: event.pointerType, live: "none", moves: 0, coalesced: 0, predicted: 0,
                    times: [event.timeStamp], pressures: [event.pressure], waiting: event.timeStamp, waits: [],
                    paints: [] };
  }

  /* A pointermove: its coalesced events (or itself) and the predicted ones. */
  input(events, predicted) {
    const s = this.stroke;
    if (!s) return;
    s.moves += 1;
    s.coalesced += events.length;
    s.predicted += predicted.length;
    for (const event of events) {
      s.times.push(event.timeStamp);
      s.pressures.push(event.pressure);
    }
    if (s.waiting === null && events.length) s.waiting = events[0].timeStamp;
  }

  /* A frame's painting, begun at ``start`` (ms, as the events' timeStamps) and taking ``ms``, on a live layer
   * of ``live`` pixels: how long the oldest input it shows waited for it — a pause in the pen is no wait. */
  frame(start, ms, live) {
    const s = this.stroke;
    if (!s) return;
    if (s.waiting !== null) s.waits.push(start - s.waiting);
    s.waiting = null;
    s.paints.push(ms);
    s.live = live;
  }

  /* The pen lifted: putting the stroke on the page took ``ms``; ``points`` were kept. */
  end(ms, points) {
    const s = this.stroke;
    this.stroke = null;
    if (!s) return;
    const span = (list) => (list.length > 1 ? list.at(-1) - list[0] : 0);
    const rate = (count, list) => (span(list) > 0 ? Math.round((count * 1000) / span(list)) : 0);
    const inputGaps = s.times.slice(1).map((t, i) => t - s.times[i]);
    const max = (list) => (list.length ? Math.max(...list) : 0);
    const average = (list) => (list.length ? list.reduce((a, b) => a + b, 0) / list.length : 0);
    const pressures = s.pressures.filter((p) => typeof p === "number");
    const fixed = (n) => n.toFixed(1);
    this.numbers.textContent = [
      `${s.pointerType} · ${rate(s.moves, s.times)} moves/s · ${rate(s.times.length - 1, s.times)} points/s`,
      `per move: ${s.moves ? (s.coalesced / s.moves).toFixed(1) : 0} coalesced, ` +
        `${s.moves ? (s.predicted / s.moves).toFixed(1) : 0} predicted`,
      `input gap: max ${fixed(max(inputGaps))} ms`,
      `input to paint: ${fixed(average(s.waits))} ms avg · ${fixed(max(s.waits))} max · ` +
        `${s.waits.filter((w) => w > LATE_MS).length} of ${s.waits.length} frames over ${LATE_MS} ms`,
      `paint: ${fixed(average(s.paints))} ms avg · ${fixed(max(s.paints))} max · lift ${fixed(ms)} ms`,
      `pressure: ${pressures.length ? `${Math.min(...pressures).toFixed(2)}–${Math.max(...pressures).toFixed(2)}` : "none"}` +
        ` · ${points} points · live ${s.live}`,
    ].join("\n");
  }
}
