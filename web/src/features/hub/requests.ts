// The hub's project requests for the whole page (V3-P103 §5): one store of records by id, and the ids the page
// follows (the cards it shows). The open requests are the store's records that are not ended: nothing else holds
// a list.
//
// - **One merge rule.** A record from the list stream or from any read replaces the store's record of the same id
//   only when its ``rev`` is greater. So an old read that lands late never covers a newer state, and events that
//   arrive while a read runs need no buffer: any order gives the same result. ``same_name_on`` is never kept (the
//   hub computes it from the holds, not the request's row; held names come from ``projects.held``).
// - **Never gone for being absent.** An open record the open list no longer has stays; its id is read with ``ids``
//   (H12), which gives its end. Only an id the hub says is ``missing`` becomes "cannot be confirmed"; a read that
//   fails leaves the record as it was, and the next read asks again. A hub without H12 says nothing of missing ids:
//   one it does not give back cannot be confirmed either.
// - **When it reads.** On the list stream's (re)connect, on each ``projects`` signal, on each tick of the page's
//   polling fallback: the open list, then the ids of open records it lacked and of followed ids still open. A newly
//   followed id is read at once unless its record is known and ended.
// - **What it keeps.** Open records, followed ids' records, and records that ended less than KEEP_ENDED_MS ago.
// - **Followed ids outlive a reload** in this tab's sessionStorage (ids only, the newest MAX_TRACKED).
// - **In doubt.** After a lost answer to an action on a request (§4.1) its id is in doubt until a read that started
//   after the answer was lost gives the hub's record of it: every card of it offers nothing to press meanwhile. A
//   read already on its way, or a stream event, may tell of the request before the action: neither clears it.
import { useSyncExternalStore } from "react";
import { readProjectRequests, readProjectRequestsByIds, type HubProjectRequest } from "@/api/hub";

export const ENDED_STATES: ReadonlySet<string> = new Set(["done", "refused", "expired", "failed", "cancelled"]);
export const isOpen = (request: HubProjectRequest) => !ENDED_STATES.has(request.state);
export const BATCH = 50; // ids per read (H12)
export const MAX_TRACKED = 50;
export const KEEP_ENDED_MS = 10 * 60_000;
export const TRACKED_KEY = "himari.hub.tracked";

export interface Entry {
  request: HubProjectRequest | null; // null: never read
  unknown: boolean; // the hub no longer has it: its result cannot be confirmed
  endedSeen: number | null; // when this page first knew it ended (or unknown)
}

export interface Snapshot {
  entries: ReadonlyMap<number, Entry>;
  tracked: readonly number[];
  doubted: ReadonlySet<number>;
}

type Read = { requests: HubProjectRequest[]; missing: number[] | null };
export interface Reader {
  open: () => Promise<Read>;
  ids: (ids: readonly number[]) => Promise<Read>;
}
export interface KeyValue {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function strip(request: HubProjectRequest): HubProjectRequest {
  const { same_name_on: _ignored, ...kept } = request;
  return kept;
}

const valid = (raw: unknown): raw is HubProjectRequest => {
  const r = raw as HubProjectRequest | null;
  return !!r && Number.isInteger(r.id) && Number.isInteger(r.rev) && typeof r.state === "string";
};

export class RequestStore {
  private entries = new Map<number, Entry>();
  private tracked: number[];
  private doubted = new Map<number, number>(); // id: the read count when its answer was lost
  private reads = 0; // reads started so far
  private listeners = new Set<() => void>();
  private snapshot: Snapshot;
  private reading: Promise<boolean> | null = null;
  private again = false;

  constructor(private readonly reader: Reader, private readonly storage: () => KeyValue | null,
              private readonly now: () => number = Date.now) {
    this.tracked = this.load();
    this.snapshot = { entries: new Map(), tracked: [...this.tracked], doubted: new Set() };
  }

  // -- reading the state ------------------------------------------------------------------------

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = () => this.snapshot;

  private emit() {
    this.snapshot = { entries: new Map(this.entries), tracked: [...this.tracked], doubted: new Set(this.doubted.keys()) };
    for (const listener of this.listeners) listener();
  }

  // -- the one merge rule -----------------------------------------------------------------------

  /** A record from anywhere; true when it replaced what the store had. */
  merge(raw: unknown, quiet = false): boolean {
    if (!valid(raw)) return false;
    const before = this.entries.get(raw.id);
    if (before?.request && before.request.rev >= raw.rev) return false;
    const request = strip(raw);
    this.entries.set(raw.id, {
      request,
      unknown: false,
      endedSeen: isOpen(request) ? null : before?.endedSeen ?? this.now(),
    });
    if (!quiet) this.emit();
    return true;
  }

  /** A lost answer to an action on ``id``: in doubt until its record is read again. */
  doubt(id: number) {
    this.doubted.set(id, this.reads);
    this.emit();
  }

  /** ``id`` was read by a read that started as read number ``started``: its doubt goes if it began before. */
  private settled(id: number, started: number) {
    const since = this.doubted.get(id);
    if (since !== undefined && started > since) this.doubted.delete(id);
  }

  private markUnknown(id: number) {
    const before = this.entries.get(id);
    if (before?.request && !isOpen(before.request)) return; // its end is known: nothing to lose
    this.entries.set(id, { request: before?.request ?? null, unknown: true, endedSeen: before?.endedSeen ?? this.now() });
  }

  // -- reading the hub --------------------------------------------------------------------------

  /** The open list, then the ids it lacked; one at a time (asked meanwhile: once more after). True when the last
   *  read went through whole (the open list and every batch of ids). */
  refresh(): Promise<boolean> {
    if (this.reading) {
      this.again = true;
      return this.reading;
    }
    this.reading = (async () => {
      let whole = false;
      do {
        this.again = false;
        whole = await this.readOnce();
      } while (this.again);
      return whole;
    })().finally(() => {
      this.reading = null;
    });
    return this.reading;
  }

  private async readOnce(): Promise<boolean> {
    let listed: Set<number> | null = null;
    const started = ++this.reads;
    try {
      const got = await this.reader.open();
      listed = new Set();
      for (const request of got.requests ?? []) {
        this.merge(request, true);
        if (valid(request)) listed.add(request.id);
      }
      for (const id of listed) this.settled(id, started); // read again: as the hub has it now
    } catch {
      // the open list could not be read: the records stay as they are, and followed ids are still asked
    }
    const ask = new Set<number>();
    if (listed) {
      for (const [id, entry] of this.entries) {
        if (entry.request && !entry.unknown && isOpen(entry.request) && !listed.has(id)) ask.add(id);
      }
    }
    for (const id of this.tracked) {
      const entry = this.entries.get(id);
      if (listed?.has(id) || entry?.unknown) continue;
      if (!entry?.request || isOpen(entry.request)) ask.add(id);
    }
    const idsRead = await this.readIds([...ask], true);
    this.prune();
    this.emit();
    return listed !== null && idsRead;
  }

  /** These ids, in batches of BATCH. A batch that fails changes nothing. True when every batch was read. */
  async readIds(ids: readonly number[], quiet = false): Promise<boolean> {
    let whole = true;
    for (let start = 0; start < ids.length; start += BATCH) {
      const chunk = ids.slice(start, start + BATCH);
      const started = ++this.reads;
      try {
        const got = await this.reader.ids(chunk);
        const given = new Set<number>();
        for (const request of got.requests ?? []) {
          this.merge(request, true);
          if (valid(request)) given.add(request.id);
        }
        // H12 names the ids it no longer has; a hub without it cannot tell, so what it did not give back is unknown
        const missing = Array.isArray(got.missing) ? got.missing : chunk.filter((id) => !given.has(id));
        for (const id of missing) if (chunk.includes(id)) this.markUnknown(id);
        for (const id of chunk) this.settled(id, started); // read again: as the hub has it now
      } catch {
        whole = false; // asked again on the next read
      }
    }
    if (!quiet) {
      this.prune();
      this.emit();
    }
    return whole;
  }

  // -- followed ids -----------------------------------------------------------------------------

  /** Follow ``id`` (a card shows it). Read at once unless its end is already known. */
  track(id: number): Promise<void> {
    if (!Number.isInteger(id) || id < 1) return Promise.resolve();
    const known = this.tracked.includes(id);
    if (!known) {
      this.tracked = [...this.tracked, id].slice(-MAX_TRACKED);
      this.save();
    }
    const entry = this.entries.get(id);
    if (!known) this.emit();
    if (known && entry?.request) return Promise.resolve(); // followed already: the regular reads keep it
    if (entry?.unknown || (entry?.request && !isOpen(entry.request))) return Promise.resolve();
    return this.readIds([id]).then(() => undefined);
  }

  untrack(id: number) {
    if (!this.tracked.includes(id)) return;
    this.tracked = this.tracked.filter((t) => t !== id);
    this.save();
    this.prune();
    this.emit();
  }

  isTracked(id: number) {
    return this.tracked.includes(id);
  }

  private prune() {
    const now = this.now();
    for (const [id, entry] of this.entries) {
      if (this.tracked.includes(id)) continue;
      if (entry.request && !entry.unknown && isOpen(entry.request)) continue;
      if (entry.endedSeen !== null && now - entry.endedSeen < KEEP_ENDED_MS) continue;
      this.entries.delete(id);
    }
  }

  /** Signed out: nothing of it stays in this page; ``wipe`` (the user's own sign-out): nor in this tab. */
  reset(wipe: boolean) {
    this.entries.clear();
    this.doubted.clear();
    if (wipe) {
      this.tracked = [];
      this.save();
    } else {
      this.tracked = this.load();
    }
    this.emit();
  }

  private load(): number[] {
    try {
      const raw: unknown = JSON.parse(this.storage()?.getItem(TRACKED_KEY) ?? "[]");
      return Array.isArray(raw) ? raw.filter((id): id is number => Number.isInteger(id) && id > 0).slice(-MAX_TRACKED) : [];
    } catch {
      return [];
    }
  }

  private save() {
    try {
      const storage = this.storage();
      if (!storage) return;
      if (this.tracked.length) storage.setItem(TRACKED_KEY, JSON.stringify(this.tracked));
      else storage.removeItem(TRACKED_KEY);
    } catch {
      // no storage here (a private window): followed ids last as long as the page
    }
  }
}

export const sessionKeyValue = (): KeyValue | null => {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
};

/** The page's one store. */
export const requests = new RequestStore(
  { open: () => readProjectRequests(true), ids: (ids) => readProjectRequestsByIds(ids) },
  sessionKeyValue,
);

/** The store's records and followed ids, live. */
export function useHubRequests(): Snapshot {
  return useSyncExternalStore(requests.subscribe, requests.getSnapshot);
}

/** The open requests, oldest first. */
export const openRequests = (snapshot: Snapshot) =>
  [...snapshot.entries.values()]
    .flatMap((entry) => (entry.request && !entry.unknown && isOpen(entry.request) ? [entry.request] : []))
    .sort((a, b) => a.id - b.id);
