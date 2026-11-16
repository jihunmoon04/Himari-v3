import { describe, expect, it } from "vitest";
import type { HubEvent } from "@/api/hub";
import { mergeEvents, toHubItems } from "./items";

const ev = (seq: number, kind: string, body: Record<string, unknown> = {}, at = "2026-11-17T10:00:00Z"): HubEvent =>
  ({ seq, kind, attempt: 1, at, body });

describe("toHubItems", () => {
  it("draws a turn: prompt, joined text, tool calls updated by id, plan, request and its outcome, the end", () => {
    const { items, stats } = toHubItems([
      ev(1, "session", { action: "resumed" }),
      ev(2, "turn_start", { task_id: 9, prompt: "fix it", agent: "claude", model: "opus", mode: "plan" },
        "2026-11-17T10:00:00Z"),
      ev(3, "thought", { text: "Let me " }),
      ev(4, "thought", { text: "look." }),
      ev(5, "tool", { id: "t1", title: "Edit a.py", kind: "edit", status: "in_progress", input: "{…}" }),
      ev(6, "tool", { id: "t2", title: "Read b.py", kind: "read", status: "in_progress" }),
      ev(7, "tool", { id: "t1", status: "completed", diffs: [{ path: "a.py", old: null, new: "x = 1\n" }] }),
      ev(8, "plan", { entries: [{ content: "edit", status: "in_progress" }] }),
      ev(9, "plan", { entries: [{ content: "edit", status: "completed" }] }),
      ev(10, "request", { interaction_id: "i1", kind: "permission", title: "Write c.py",
        options: [{ option_id: "a", name: "Allow", kind: "allow_once" }] }),
      ev(11, "answer", { interaction_id: "i1", kind: "permission", outcome: "refused" }),
      ev(12, "text", { text: "Done" }),
      ev(13, "text", { text: " now." }),
      ev(14, "usage", { used: 1200, size: 200000 }),
      ev(15, "turn_end", { task_id: 9, outcome: "done" }, "2026-11-17T10:01:05Z"),
      ev(16, "something_new", { x: 1 }),
    ]);
    expect(items.map((item) => item.kind)).toEqual(
      ["notice", "user", "thought", "work", "plan", "request", "answer", "turn"]);
    expect(items[1]).toMatchObject({ text: "fix it", meta: ["claude", "opus", "mode plan"] });
    expect(items[2]).toMatchObject({ text: "Let me look." });
    const work = items[3];
    if (work?.kind !== "work") throw new Error("work");
    expect(work.steps.map((s) => [s.title, s.toolId, s.status, s.result?.isError])).toEqual([
      ["Edit a.py", "edit", "completed", false],
      ["Read b.py", "read", "unknown", undefined], // still open when the turn ended
    ]);
    expect(work.steps[0]?.input).toBe("{…}"); // an update keeps what it does not carry
    expect(work.steps[0]?.diffs).toEqual([{ path: "a.py", old: null, new: "x = 1\n", cut: false, more: 0 }]);
    expect(items[4]).toMatchObject({ entries: [{ content: "edit", status: "completed" }] });
    expect(items[5]).toMatchObject({ ask: "Write c.py", options: ["Allow"], outcome: "refused" });
    expect(items[6]).toMatchObject({ kind: "answer", text: "Done now." });
    expect(items[7]).toMatchObject({ turn: { outcome: "done", seconds: 65 } });
    expect(stats.context).toEqual({ used: 1200, size: 200000 });
  });

  it("a failed call is an error, and a request still open has no outcome", () => {
    const { items } = toHubItems([
      ev(1, "tool", { id: 7, title: "Run tests", status: "failed", output: "boom" }),
      ev(2, "request", { interaction_id: 3, kind: "question", message: "Which one?" }),
    ]);
    expect(items[0]).toMatchObject({ steps: [{ result: { text: "boom", isError: true } }] });
    expect(items[1]).toMatchObject({ kind: "request", ask: "Which one?", outcome: null });
  });
});

describe("mergeEvents", () => {
  it("keeps one event per seq, oldest first", () => {
    expect(mergeEvents([ev(3, "text"), ev(1, "text")], [ev(2, "text"), ev(3, "plan")]).map((e) => [e.seq, e.kind]))
      .toEqual([[1, "text"], [2, "text"], [3, "plan"]]);
  });
});
