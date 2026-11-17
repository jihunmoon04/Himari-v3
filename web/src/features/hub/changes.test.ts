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
    expect(turns.map((t) => [t.turn, t.prompt, t.diffs.map((d) => d.new)])).toEqual([
      [1, "first", ["z\n"]],
      [3, "third", ["new\n"]],
    ]);
  });

  it("are gathered by file with their line counts", () => {
    const files = byFile([
      { turn: 1, prompt: "", diffs: [edit("a.py", "1\n2\n", "1\n3\n4\n"), edit("b.md", null, "x\n")] },
      { turn: 2, prompt: "", diffs: [edit("a.py", "1\n3\n4\n", "1\n")] },
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
    const rows = splitRows(edit("a", "1\n2\n3\n4\n5\n6\nold\n", "1\n2\n3\n4\n5\n6\nnew\nmore\n"));
    expect(rows).toEqual([
      { left: "1", right: "1", kind: "same" },
      { left: "2", right: "2", kind: "same" },
      { left: null, right: null, kind: "gap" },
      { left: "5", right: "5", kind: "same" },
      { left: "6", right: "6", kind: "same" },
      { left: "old", right: "new", kind: "changed" },
      { left: null, right: "more", kind: "changed" },
    ]);
    expect(splitRows(edit("n", null, "a\nb\n"))).toEqual([
      { left: null, right: "a", kind: "added" },
      { left: null, right: "b", kind: "added" },
    ]);
  });
});
