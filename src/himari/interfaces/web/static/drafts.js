/* The composer's drafts, one per place (V3-P78 §3): Home ("home") and each
 * conversation (its id). The table in memory is the source; the browser
 * store (local_store.js) follows it, so moving between places never waits
 * for a read (§3.1, review B3). No DOM here: app.js says what the composer
 * holds and when it changed.
 *
 * A draft: {text, attachments: [{fileId, name, size, uploadedAt}], references: [...]};
 * kept with {at, writer} and, while a message from it is being sent, {sending} (§3.1, N6). */

export const DRAFT_LIMITS = { places: 50, ageMs: 30 * 24 * 3600 * 1000, chars: 32000 }; // Q3
// The server takes an upload in a message for 23 hours (data/api.py); a chip is dropped an hour before (§3.1, N5).
export const ATTACHMENT_MAX_AGE_MS = 22 * 3600 * 1000;
const SAVE_DELAY_MS = 400; // §3.3

export function emptyDraft() {
  return { text: "", attachments: [], references: [] };
}

export function isEmpty(draft) {
  return !draft || (!draft.text && !draft.attachments.length && !draft.references.length);
}

function referenceKey(r) {
  return JSON.stringify([r.path, r.whole ? null : r.start, r.whole ? null : r.end]);
}

/* ``into`` with ``more`` after it: the text joined by a blank line, chips added once. */
export function mergeDrafts(into, more) {
  const a = into || emptyDraft();
  const b = more || emptyDraft();
  const text = a.text && b.text ? `${a.text}\n\n${b.text}` : a.text || b.text;
  const ids = new Set(a.attachments.map((f) => f.fileId));
  const keys = new Set(a.references.map(referenceKey));
  return { text,
           attachments: [...a.attachments, ...b.attachments.filter((f) => !ids.has(f.fileId))],
           references: [...a.references, ...b.references.filter((r) => !keys.has(referenceKey(r)))] };
}

/* What stays of ``draft`` once ``sent`` was accepted (§3.1, review B2): the text if it was edited since,
 * and the chips not sent. */
export function withoutSent(draft, sent) {
  const ids = new Set(sent.fileIds);
  const keys = new Set(sent.references.map(referenceKey));
  return { text: draft.text === sent.text ? "" : draft.text,
           attachments: draft.attachments.filter((f) => !ids.has(f.fileId)),
           references: draft.references.filter((r) => !keys.has(referenceKey(r))) };
}

/* The chips of ``draft`` an upload too old to send (§3.1, N5) has left: [kept draft, names dropped]. */
export function withoutExpired(draft, now) {
  const kept = draft.attachments.filter((f) => typeof f.uploadedAt === "number" && now - f.uploadedAt <= ATTACHMENT_MAX_AGE_MS);
  const dropped = draft.attachments.filter((f) => !kept.includes(f)).map((f) => f.name);
  return [{ ...draft, attachments: kept }, dropped];
}

function copy(draft) {
  return { text: draft.text, attachments: draft.attachments.map((f) => ({ ...f })),
           references: draft.references.map((r) => ({ ...r })) };
}

function kept(value) {
  const entry = { ...copy(value), at: value.at };
  if (value.sending) entry.sending = value.sending;
  return entry;
}

/* A place kept in the browser: Home or a conversation id, never the conversation being created (§3.3). */
export function storable(place) {
  return typeof place === "string";
}

export class DraftBook {
  /* store: {entries(name), get(name, key), put(name, key, value), remove(name, key), prune(name, max, ageMs)}
   * (local_store.js); writer: this tab's id. */
  constructor(store, { writer, now = () => Date.now(), limits = DRAFT_LIMITS, delay = SAVE_DELAY_MS,
                       setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (t) => clearTimeout(t) } = {}) {
    this.store = store;
    this.writer = writer;
    this.now = now;
    this.limits = limits;
    this.delay = delay;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.table = new Map(); // place → {text, attachments, references, at}
    this.pending = null; // {place, read, timer}: a save waiting for the composer to settle
  }

  /* Once after a sign-in: what this browser kept, the old dropped (§3.3). */
  async load() {
    const now = this.now();
    for (const [place, value] of await this.store.entries("drafts")) {
      if (!value || typeof value.at !== "number" || now - value.at > this.limits.ageMs) continue;
      if (!this.table.has(place)) this.table.set(place, kept(value));
    }
    await this.store.prune("drafts", this.limits.places, this.limits.ageMs);
  }

  get(place) {
    const found = this.table.get(place);
    return found ? copy(found) : emptyDraft();
  }

  at(place) {
    const found = this.table.get(place);
    return found ? found.at : 0;
  }

  /* Whether a message from ``place`` was being sent when it was kept, and not known to be taken (§3.1, N6). */
  sending(place) {
    const found = this.table.get(place);
    return Boolean(found && found.sending);
  }

  /* ``place``'s draft is now ``draft``: in memory at once, and in the store for a storable place. A
   * ``sending`` mark stays until ``sent`` says the message was taken (or ``sending`` sets it). */
  set(place, draft, { sending = undefined, sent = false } = {}) {
    if (this.pending && this.pending.place === place) this.cancel();
    const before = this.table.get(place);
    const mark = sent ? null : sending !== undefined ? sending : before && before.sending ? before.sending : null;
    if (isEmpty(draft)) {
      this.table.delete(place);
      if (storable(place)) this.store.remove("drafts", place);
      return;
    }
    const at = this.now();
    const entry = { ...copy(draft), at };
    if (mark) entry.sending = mark;
    this.table.set(place, entry);
    if (!storable(place)) return;
    this.store.put("drafts", place, { ...entry, text: draft.text.slice(0, this.limits.chars), writer: this.writer });
  }

  /* A message from ``place`` is on its way: what is kept says so until the 202 (§3.1, N6). */
  markSending(place, draft) {
    this.set(place, draft, { sending: this.now() });
  }

  /* The composer of ``place`` changed: ``read()`` is saved once it settles (§3.3). */
  schedule(place, read) {
    if (this.pending && this.pending.place !== place) this.flush();
    if (this.pending) this.clearTimer(this.pending.timer);
    const pending = { place, read, timer: null };
    pending.timer = this.setTimer(() => {
      if (this.pending === pending) {
        this.pending = null;
        this.set(place, read());
      }
    }, this.delay);
    this.pending = pending;
  }

  /* A waiting save, now (leaving the place, the page going to the background). */
  flush() {
    const pending = this.pending;
    if (!pending) return;
    this.clearTimer(pending.timer);
    this.pending = null;
    this.set(pending.place, pending.read());
  }

  cancel() {
    if (this.pending) this.clearTimer(this.pending.timer);
    this.pending = null;
  }

  /* Out of memory only: what the store has for ``place`` stays until ``remove`` (a draft carried into a
   * conversation being created, §3.1, N5). */
  forget(place) {
    if (this.pending && this.pending.place === place) this.cancel();
    this.table.delete(place);
  }

  /* The message was refused: the draft is not "maybe sent" any more. */
  clearSending(place) {
    const found = this.table.get(place);
    if (found && found.sending) this.set(place, copy(found), { sent: true });
  }

  remove(place) {
    if (this.pending && this.pending.place === place) this.cancel();
    this.table.delete(place);
    if (storable(place)) this.store.remove("drafts", place);
  }

  /* §3.1 (review B2): what was sent from ``place`` leaves its draft, wherever the composer is now. */
  removeSent(place, sent) {
    const found = this.table.get(place);
    if (!found) return;
    this.set(place, withoutSent(copy(found), sent), { sent: true });
  }

  /* §3.1: ``more`` added after ``place``'s draft (a conversation being created, left for elsewhere). */
  merge(place, more) {
    if (isEmpty(more)) return;
    this.set(place, mergeDrafts(this.get(place), more));
  }

  /* What another tab kept for ``place`` after this tab's copy (§3.1), or null. */
  async fresher(place) {
    if (!storable(place)) return null;
    const value = await this.store.get("drafts", place);
    if (!value || typeof value.at !== "number" || value.writer === this.writer || value.at <= this.at(place)) return null;
    if (this.now() - value.at > this.limits.ageMs) return null;
    this.table.set(place, kept(value));
    return copy(value);
  }

  /* Sign out (§5.3): nothing kept in memory, nothing waiting to be written. */
  clear() {
    this.cancel();
    this.table.clear();
  }
}
