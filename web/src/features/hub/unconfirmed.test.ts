// V3-P103 §4.1: a request or a removal sent with a client key is kept in this tab until its answer is known; after a
// lost answer it survives a reload and is sent again with the same key, time and body.
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { HubProjectRequest } from "@/api/hub";
import { RequestStore, TRACKED_KEY, type KeyValue } from "./requests";
import { keyRefusal, sameAs, sendKeyed, UNCONFIRMED_KEY, UnconfirmedList, type Senders, type Unconfirmed } from "./unconfirmed";

class Memory implements KeyValue {
  data = new Map<string, string>();
  getItem(key: string) { return this.data.get(key) ?? null; }
  setItem(key: string, value: string) { this.data.set(key, value); }
  removeItem(key: string) { this.data.delete(key); }
}

const made: HubProjectRequest = { id: 11, rev: 1, state: "waiting_device", kind: "create", project: "site", runner: "laptop" };
const entry: Unconfirmed = {
  client_key: "k1", key_time: "2026-11-18T10:00:00Z", route: "request", at: 1,
  body: { kind: "create", project: "site", runner: "laptop", path: "D:/code" },
};
const lost = new ApiError(503, "hub_unknown", "The hub did not answer");

function setup() {
  const storage = new Memory();
  const list = new UnconfirmedList(() => storage);
  const store = new RequestStore({ open: async () => ({ requests: [], missing: [] }),
    ids: async () => ({ requests: [made], missing: [] }) }, () => storage);
  const senders: Senders = { request: vi.fn(), remove: vi.fn() };
  return { storage, list, store, senders };
}

describe("a request sent with a client key", () => {
  it("is kept before it is sent and goes once its id is followed", async () => {
    const { storage, list, store, senders } = setup();
    vi.mocked(senders.request).mockImplementation(async () => {
      expect(JSON.parse(storage.getItem(UNCONFIRMED_KEY)!)[0].client_key).toBe("k1"); // written first
      return { request: made };
    });
    const sent = await sendKeyed(entry, false, list, store, senders);
    expect(sent).toEqual({ request: made });
    expect(senders.request).toHaveBeenCalledWith({ ...entry.body, client_key: "k1", key_time: entry.key_time });
    expect(JSON.parse(storage.getItem(TRACKED_KEY)!)).toEqual([11]);
    expect(storage.getItem(UNCONFIRMED_KEY)).toBeNull();
  });

  it("after a lost answer survives a reload and is sent again with the same key, time and body", async () => {
    const { storage, list, store, senders } = setup();
    vi.mocked(senders.request).mockRejectedValueOnce(lost).mockResolvedValueOnce({ request: made });
    const first = await sendKeyed(entry, false, list, store, senders);
    expect("error" in first).toBe(true);
    const reloaded = new UnconfirmedList(() => storage); // the page again, the same tab
    expect(reloaded.getSnapshot()).toEqual([entry]);
    await sendKeyed(reloaded.getSnapshot()[0]!, true, reloaded, store, senders);
    expect(vi.mocked(senders.request).mock.calls[1]![0]).toEqual(vi.mocked(senders.request).mock.calls[0]![0]);
    expect(reloaded.getSnapshot()).toEqual([]);
    expect(store.isTracked(11)).toBe(true);
  });

  it("a key the hub no longer takes ends the resending: only discarding is left", async () => {
    const { list, store, senders } = setup();
    const expired = new ApiError(409, "hub_refused", "client_key_expired: 키가 너무 오래됐습니다");
    expect(keyRefusal(expired)).toBe(true);
    expect(keyRefusal(new ApiError(409, "hub_refused", "기기가 연결돼 있지 않습니다"))).toBe(false);
    vi.mocked(senders.request).mockRejectedValueOnce(expired);
    await sendKeyed(entry, true, list, store, senders);
    expect(list.getSnapshot()[0]?.refused).toBe("client_key_expired: 키가 너무 오래됐습니다");
    list.drop("k1");
    expect(list.getSnapshot()).toEqual([]);
  });

  it("an answer without a request (empty, cut) is a lost answer: the entry stays", async () => {
    const { list, store, senders } = setup();
    vi.mocked(senders.request).mockResolvedValueOnce(null as never).mockResolvedValueOnce({} as never);
    expect("error" in await sendKeyed(entry, false, list, store, senders)).toBe(true);
    expect(list.getSnapshot()).toHaveLength(1);
    expect("error" in await sendKeyed(entry, true, list, store, senders)).toBe(true);
    expect(list.getSnapshot()).toEqual([{ ...entry, refused: null }]); // still to be sent again
  });

  it("a first send the hub refuses, or that never reached it, leaves nothing behind", async () => {
    const { list, store, senders } = setup();
    vi.mocked(senders.remove)
      .mockRejectedValueOnce(new ApiError(409, "hub_refused", "기기에 이 프로젝트의 폴더가 없습니다"))
      .mockRejectedValueOnce(new ApiError(503, "hub_unavailable", "The hub could not be reached"))
      .mockRejectedValueOnce(new ApiError(0, "offline", "The server could not be reached."));
    const removal: Unconfirmed = { ...entry, route: "remove", body: { project: "site", runner: "linux" } };
    await sendKeyed(removal, false, list, store, senders);
    await sendKeyed(removal, false, list, store, senders);
    expect(list.getSnapshot()).toEqual([]);
    await sendKeyed(removal, false, list, store, senders); // this server's answer lost: it may have been relayed
    expect(list.getSnapshot()).toHaveLength(1);
  });

  it("without H13, the open requests like it are found by kind, project and device", () => {
    const open: HubProjectRequest[] = [made, { ...made, id: 12, runner: "linux" },
      { ...made, id: 13, kind: "remove_folder" }];
    expect(sameAs(entry, open).map((r) => r.id)).toEqual([11]);
  });

  it("without storage it still works for this page", async () => {
    const list = new UnconfirmedList(() => { throw new Error("no storage"); });
    list.put(entry);
    expect(list.getSnapshot()).toEqual([entry]);
  });
});
