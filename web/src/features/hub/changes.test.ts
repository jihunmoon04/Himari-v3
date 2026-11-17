import { describe, expect, it } from "vitest";
import type { HubEvent } from "@/api/hub";
import { byFile, changesByTurn, planHistory, splitRows } from "./changes";

const at = "2026-11-17T10:00:00Z";
const ev = (seq: number, kind: string, body: Record<string, unknown>, attempt = 1): HubEvent => ({ seq, kind, attempt, at, body });
const edit = (path: string, old: string | null, now: string) => ({ path, old, new: now, cut: false, more: 0 });

describe("a session's changes", () => {
  it("are grouped by turn, a call counted once by its last diffs", () => {
    const turns = changesByTurn([
      ev(1, "turn_start", { prompt: "first" }),
      ev(2, "tool", { id: "t1", status: "in_progress", title: "Edit" }),
      ev(3, "tool", { id: "t1", diffs: [edit("a.py", "x\n", "y\n")] }),
      ev(4, "tool", { id: "t1", status: "completed", diffs: [edit("a.py", "x\n", "z\n")] }), // the call's last word
      ev(5, "turn_end", { outcome: "done" }),
      ev(6, "turn_start", { prompt: "nothing changed" }),
      ev(7, "turn_start", { prompt: "third" }),
      ev(8, "tool", { id: "t1", diffs: [edit("b.md", null, "new\n")] }, 3), // same id, another attempt
    ]);
    expect(turns.map((t) => [t.id, t.turn, t.prompt, t.diffs.map((d) => d.new)])).toEqual([
      [1, 1, "first", ["z\n"]],
      [7, 3, "third", ["new\n"]],
    ]);
    // earlier events read in front: the place changes, the id does not
    const again = changesByTurn([ev(0, "turn_start", { prompt: "zero" }), ev(0.5, "tool", { id: "t0", diffs: [edit("c", "a", "b")] }),
      ev(1, "turn_start", { prompt: "first" }), ev(3, "tool", { id: "t1", diffs: [edit("a.py", "x\n", "z\n")] })]);
    expect(again.find((t) => t.prompt === "first")).toMatchObject({ id: 1, turn: 2 });
  });

  it("are gathered by file with their line counts", () => {
    const files = byFile([
      { id: 1, turn: 1, prompt: "", diffs: [edit("a.py", "1\n2\n", "1\n3\n4\n"), edit("b.md", null, "x\n")] },
      { id: 5, turn: 2, prompt: "", diffs: [edit("a.py", "1\n3\n4\n", "1\n")] },
    ]);
    expect(files.map((f) => [f.path, f.diffs.length, f.added, f.removed, f.created])).toEqual([
      ["a.py", 2, 2, 3, false],
      ["b.md", 1, 1, 0, true],
    ]);
  });

  it("keeps every plan with how much was done", () => {
    const plans = planHistory([
      ev(1, "plan", { entries: [{ content: "a", status: "in_progress" }, { content: "b", status: "pending" }] }),
      ev(2, "text", { text: "x" }),
      ev(3, "plan", { entries: [{ content: "a", status: "completed" }, { content: "b", status: "in_progress" }] }),
    ]);
    expect(plans.map((p) => [p.key, p.done, p.entries.length])).toEqual([["1", 0, 2], ["3", 1, 2]]);
  });

  it("are shown side by side: a replaced run beside its replacement, long same runs shortened", () => {
    const { rows, more } = splitRows(edit("a", "1\n2\n3\n4\n5\n6\nold\n", "1\n2\n3\n4\n5\n6\nnew\nmore\n"));
    expect(more).toBe(false);
    expect(rows).toEqual([
      { left: "1", right: "1", kind: "same" },
      { left: "2", right: "2", kind: "same" },
      { left: null, right: null, kind: "gap" },
      { left: "5", right: "5", kind: "same" },
      { left: "6", right: "6", kind: "same" },
      { left: "old", right: "new", kind: "changed" },
      { left: null, right: "more", kind: "changed" },
    ]);
    expect(splitRows(edit("n", null, "a\nb\n")).rows).toEqual([
      { left: null, right: "a", kind: "added" },
      { left: null, right: "b", kind: "added" },
    ]);
    const long = splitRows(edit("l", null, Array.from({ length: 30 }, (_, n) => `line ${n}`).join("\n")), 10);
    expect(long.rows).toHaveLength(10);
    expect(long.more).toBe(true); // cut here: the view says so
  });
});
