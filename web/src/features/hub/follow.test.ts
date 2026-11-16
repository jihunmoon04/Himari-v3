import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import * as api from "@/api/hub";
import { dismiss, failedBeforeRunning, follow, peekFollowed, resetFollowed, runsIn } from "./follow";

const one = (id: number) => peekFollowed().find((f) => f.task.id === id)!;

describe("following a task to its end", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    resetFollowed();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("reads a new task again until it has a session and ends, then stops", async () => {
    const read = vi.spyOn(api, "readHubTask")
      .mockResolvedValueOnce({ task: { id: 7, status: "running", session: null } })
      .mockResolvedValueOnce({ task: { id: 7, status: "running", session: 12, project: "demo" } })
      .mockResolvedValueOnce({ task: { id: 7, status: "done", session: 12, project: "demo" } });
    const seen: (number | null)[] = [];
    follow({ id: 7, status: "queued" }, null, "demo");
    for (let n = 0; n < 3; n += 1) {
      await vi.advanceTimersByTimeAsync(2_000);
      seen.push(runsIn(one(7)));
    }
    expect(seen).toEqual([null, 12, 12]);
    expect(one(7).done).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(read).toHaveBeenCalledTimes(3); // done: no more reads
  });

  it("does not take the session a send was sent to for the one it runs in", () => {
    vi.spyOn(api, "readHubTask").mockResolvedValue({ task: { id: 3, status: "queued", session: 5 } });
    follow({ id: 3, status: "queued", session: 5 }, 5, "demo");
    expect(runsIn(one(3))).toBeNull();
    expect(one(3).from).toBe(5);
  });

  it("keeps trying while the hub does not answer, and stops for a task it does not know", async () => {
    const read = vi.spyOn(api, "readHubTask")
      .mockRejectedValueOnce(new ApiError(503, "hub_unavailable", "down"))
      .mockResolvedValueOnce({ task: { id: 8, status: "failed", session: null, result: "closed" } })
      .mockRejectedValue(new ApiError(409, "hub_refused", "no such task"));
    follow({ id: 8, status: "queued", session: 5 }, 5, "demo");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(one(8).done).toBe(false);
    await vi.advanceTimersByTimeAsync(5_000); // after a miss: later
    expect(one(8).done && failedBeforeRunning(one(8))).toBe(true);
    follow({ id: 9, status: "queued" }, 5, "demo");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(one(9).lost).toBe("no such task");
    dismiss(8);
    expect(peekFollowed().map((f) => f.task.id)).toEqual([9]);
    expect(read).toHaveBeenCalledTimes(3);
  });
});
