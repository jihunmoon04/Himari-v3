// V3-P103 §5: the project requests' one store. The same result whatever order events, the open list and ``ids``
// reads arrive in; nothing gone for being absent; followed ids read at once and kept across a reload.
import { describe, expect, it, vi } from "vitest";
import type { HubProjectRequest } from "@/api/hub";
import { KEEP_ENDED_MS, MAX_TRACKED, openRequests, RequestStore, TRACKED_KEY, type KeyValue, type Reader } from "./requests";

type Read = { requests: HubProjectRequest[]; missing: number[] | null };

const req = (id: number, rev: number, state: string, more: Partial<HubProjectRequest> = {}): HubProjectRequest => ({
  id, rev, state, kind: "create", project: `p${id}`, runner: "laptop", ...more,
});

class Memory implements KeyValue {
  data = new Map<string, string>();
  getItem(key: string) { return this.data.get(key) ?? null; }
  setItem(key: string, value: string) { this.data.set(key, value); }
  removeItem(key: string) { this.data.delete(key); }
}

/** A hub with these requests now (by id); ``gone``: ids it no longer has. */
class FakeHub implements Reader {
  now = new Map<number, HubProjectRequest>();
  gone = new Set<number>();
  asked: number[][] = [];
  failIds = false;
  failOpen = false;
  h12 = true;
  open = vi.fn(async (): Promise<Read> => {
    if (this.failOpen) throw new Error("down");
    return { requests: [...this.now.values()].filter((r) => !["done", "refused", "expired", "failed", "cancelled"]
      .includes(r.state)), missing: null };
  });
  ids = vi.fn(async (ids: readonly number[]): Promise<Read> => {
    this.asked.push([...ids]);
    if (this.failIds) throw new Error("down");
    return { requests: ids.flatMap((id) => this.now.get(id) ?? []),
      missing: this.h12 ? ids.filter((id) => this.gone.has(id)) : null };
  });
  set(...requests: HubProjectRequest[]) { for (const r of requests) this.now.set(r.id, r); }
}

const states = (store: RequestStore) =>
  Object.fromEntries([...store.getSnapshot().entries].map(([id, e]) => [id, e.unknown ? "unknown" : e.request?.state]));

describe("the project requests' store", () => {
  it("keeps the highest rev whatever order the stream, the open list and an ids read come in", async () => {
    const orders = [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]];
    const results = new Set<string>();
    for (const order of orders) {
      const hub = new FakeHub();
      const store = new RequestStore(hub, () => null);
      const steps = [
        () => store.merge(req(1, 3, "applying")), // a stream event
        () => { for (const r of [req(1, 2, "waiting_device"), req(2, 1, "waiting_user")]) store.merge(r); }, // a list
        () => store.merge(req(1, 4, "done", { result_path: "/r/p1" })), // an ids read
      ];
      for (const n of order) steps[n]!();
      results.add(JSON.stringify(states(store)));
      expect(store.getSnapshot().entries.get(1)?.request?.result_path).toBe("/r/p1");
    }
    expect([...results]).toEqual([JSON.stringify({ 1: "done", 2: "waiting_user" })]);
  });

  it("never lets a read that lands late cover a newer state, and keeps no same_name_on", async () => {
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => null);
    let answer!: (read: Read) => void;
    hub.open.mockImplementationOnce(() => new Promise<Read>((resolve) => { answer = resolve; }));
    const reading = store.refresh();
    store.merge(req(1, 5, "lost", { same_name_on: ["linux"] })); // the stream, while the read runs
    answer({ requests: [req(1, 4, "applying")], missing: null });
    await reading;
    expect(store.getSnapshot().entries.get(1)?.request).toEqual(req(1, 5, "lost"));
    expect(store.getSnapshot().entries.get(1)?.request).not.toHaveProperty("same_name_on");
  });

  it("does not drop an open record the open list lacks: its id is read and gives its end", async () => {
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => null);
    store.merge(req(1, 2, "applying"));
    store.merge(req(2, 1, "waiting_device"));
    hub.set(req(1, 3, "done"), req(2, 1, "waiting_device"));
    hub.ids.mockImplementationOnce(async () => ({ requests: [], missing: [] })); // the hub said nothing of it yet
    await store.refresh();
    expect(states(store)).toEqual({ 1: "applying", 2: "waiting_device" }); // still there
    await store.refresh();
    expect(states(store)).toEqual({ 1: "done", 2: "waiting_device" });
    expect(hub.ids.mock.calls.map(([ids]) => ids)).toEqual([[1], [1]]);
  });

  it("marks only what the hub says is missing; a read that fails changes nothing", async () => {
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => null);
    store.merge(req(1, 2, "applying"));
    store.merge(req(2, 2, "applying"));
    store.merge(req(3, 1, "done"));
    hub.failIds = true;
    await store.refresh();
    expect(states(store)).toEqual({ 1: "applying", 2: "applying", 3: "done" });
    hub.failIds = false;
    hub.gone = new Set([1, 3]);
    hub.set(req(2, 3, "failed"));
    await store.refresh();
    expect(states(store)).toEqual({ 1: "unknown", 2: "failed", 3: "done" }); // 3's end was known: kept
    hub.failOpen = true; // the open list fails: followed ids are still asked
    await store.track(9);
    expect(hub.asked.at(-1)).toEqual([9]);
  });

  it("with a hub without H12, what it does not give back cannot be confirmed", async () => {
    const hub = new FakeHub();
    hub.h12 = false;
    const store = new RequestStore(hub, () => null);
    store.merge(req(1, 2, "applying"));
    await store.refresh();
    expect(states(store)).toEqual({ 1: "unknown" });
  });

  it("reads a newly followed id at once, unless its end is known", async () => {
    const hub = new FakeHub();
    hub.set(req(4, 2, "waiting_user"));
    const store = new RequestStore(hub, () => null);
    await store.track(4);
    expect(hub.asked).toEqual([[4]]);
    expect(states(store)).toEqual({ 4: "waiting_user" });
    store.merge(req(5, 3, "done"));
    await store.track(5);
    expect(hub.asked).toEqual([[4]]);
  });

  it("reads more than 50 ids in batches of 50", async () => {
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => null);
    for (let id = 1; id <= 120; id += 1) store.merge(req(id, 1, "applying"));
    hub.open.mockResolvedValueOnce({ requests: [], missing: null });
    await store.refresh();
    expect(hub.asked.map((chunk) => chunk.length)).toEqual([50, 50, 20]);
  });

  it("after a reload right after a request was made, the card comes back with how it ended meanwhile", async () => {
    const storage = new Memory();
    const hub = new FakeHub();
    const before = new RequestStore(hub, () => storage);
    before.merge(req(7, 1, "waiting_device"));
    void before.track(7); // the answer's id is followed first (then the unconfirmed entry goes)
    expect(JSON.parse(storage.getItem(TRACKED_KEY)!)).toEqual([7]);
    hub.set(req(7, 4, "done", { result_path: "D:/code/p7" })); // it ends while the page reloads
    const after = new RequestStore(hub, () => storage); // a new page, the same tab
    expect(after.getSnapshot().tracked).toEqual([7]);
    await after.refresh();
    expect(after.getSnapshot().entries.get(7)?.request?.state).toBe("done");
  });

  it("keeps open, followed and recently ended records only, and the newest 50 followed ids", async () => {
    let now = 1_000_000;
    const storage = new Memory();
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => storage, () => now);
    store.merge(req(1, 1, "done"));
    store.merge(req(2, 1, "done"));
    store.merge(req(3, 1, "waiting_user"));
    hub.set(req(2, 1, "done"), req(3, 1, "waiting_user"));
    await store.track(2);
    now += KEEP_ENDED_MS + 1;
    await store.refresh();
    expect(states(store)).toEqual({ 2: "done", 3: "waiting_user" });
    expect(openRequests(store.getSnapshot()).map((r) => r.id)).toEqual([3]);
    store.untrack(2);
    expect(states(store)).toEqual({ 3: "waiting_user" });
    for (let id = 100; id < 100 + MAX_TRACKED + 5; id += 1) void store.track(id);
    expect(store.getSnapshot().tracked).toHaveLength(MAX_TRACKED);
    expect(store.getSnapshot().tracked[0]).toBe(105);
  });

  it("asked to read while it reads, it reads once more after", async () => {
    const hub = new FakeHub();
    const store = new RequestStore(hub, () => null);
    const first = store.refresh();
    void store.refresh();
    void store.refresh();
    await first;
    expect(hub.open).toHaveBeenCalledTimes(2);
  });
});
