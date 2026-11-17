// V3-P103 §5: while the list stream is away the page reads by asking, projects and requests on the same timer; when
// the stream is back the timer stops (never two at once) and what may have been lost is read once.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/hub";
import { onProjectSignals, openList } from "./list";

class FakeSource {
  static made: FakeSource[] = [];
  static CLOSED = 2;
  readyState = 0;
  listeners = new Map<string, ((event: unknown) => void)[]>();
  constructor(public url: string) {
    FakeSource.made.push(this);
  }
  addEventListener(name: string, listener: (event: unknown) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  fire(name: string, data?: unknown) {
    for (const listener of this.listeners.get(name) ?? []) listener({ data: JSON.stringify(data ?? {}) });
  }
  close() {
    this.readyState = FakeSource.CLOSED;
  }
}

describe("the list stream and its polling fallback", () => {
  const signals = { request: vi.fn(), projects: vi.fn(), connected: vi.fn(), polled: vi.fn() };
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSource.made = [];
    vi.stubGlobal("EventSource", FakeSource);
    vi.spyOn(api, "listHubSessions").mockResolvedValue({ sessions: [] });
    onProjectSignals(signals);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
    for (const fn of Object.values(signals)) fn.mockReset();
  });

  it("passes the two events on and reads once on each (re)connect", () => {
    const stop = openList();
    const source = FakeSource.made[0]!;
    source.fire("open");
    source.fire("project_request", { id: 3, rev: 2, state: "done" });
    source.fire("projects");
    expect(signals.connected).toHaveBeenCalledTimes(1);
    expect(signals.request).toHaveBeenCalledWith({ id: 3, rev: 2, state: "done" });
    expect(signals.projects).toHaveBeenCalledTimes(1);
    source.fire("open"); // the browser reconnected by itself
    expect(signals.connected).toHaveBeenCalledTimes(2);
    stop();
  });

  it("polls on one timer while the stream is refused, and stops it when the stream is back", async () => {
    const stop = openList();
    const first = FakeSource.made[0]!;
    first.readyState = FakeSource.CLOSED;
    first.fire("error"); // refused before it started
    await vi.advanceTimersByTimeAsync(0);
    expect(signals.polled).toHaveBeenCalledTimes(1); // read at once
    await vi.advanceTimersByTimeAsync(20_000);
    expect(signals.polled).toHaveBeenCalledTimes(3); // and every 10 s
    await vi.advanceTimersByTimeAsync(10_000); // after 30 s the stream is tried again
    expect(FakeSource.made).toHaveLength(2);
    const polls = signals.polled.mock.calls.length;
    FakeSource.made[1]!.fire("open");
    expect(signals.connected).toHaveBeenCalledTimes(1); // what may have been lost: read once
    await vi.advanceTimersByTimeAsync(60_000);
    expect(signals.polled.mock.calls.length).toBe(polls); // the timer stopped: one reader at a time
    stop();
  });

  it("reads by asking while a stream that broke off is connected again, and stops when it is back", async () => {
    const stop = openList();
    const source = FakeSource.made[0]!;
    source.fire("open");
    source.fire("error"); // broke off: the browser connects it again by itself (CONNECTING)
    source.fire("error");
    await vi.advanceTimersByTimeAsync(25_000);
    expect(signals.polled).toHaveBeenCalledTimes(2); // every 10 s, on one timer
    expect(FakeSource.made).toHaveLength(1); // no stream of its own: the browser's comes back
    source.fire("open");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(signals.polled).toHaveBeenCalledTimes(2);
    expect(signals.connected).toHaveBeenCalledTimes(2);
    stop();
  });
});
