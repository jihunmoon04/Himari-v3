import { describe, expect, it } from "vitest";
import type { EventView } from "@/api/himari";
import { stepTitle, toItems } from "./items";
import { EMPTY_LIVE, reduceLive } from "./live";

const at = "2026-11-16T10:00:00Z";
const event = (sequence: number, kind: string, rest: Partial<EventView> = {}): EventView => ({ sequence, kind, created_at: at, ...rest });

describe("toItems", () => {
  it("folds consecutive tool steps into one work block and gives each its result", () => {
    const items = toItems([
      event(1, "user_message", { text: "look" }),
      event(2, "assistant_tool_calls", { run_id: "r1", text: "Reading", calls: [
        { call_id: "c1", tool_id: "machine/read_file", arguments: { path: "a.txt" } }] }),
      event(3, "tool_result", { run_id: "r1", call_id: "c1", text: "hello", is_error: false }),
      event(4, "assistant_tool_calls", { run_id: "r2", calls: [
        { call_id: "c1", tool_id: "machine/list", arguments: {} }] }), // same call id, another Run
      event(5, "tool_result", { run_id: "r2", call_id: "c1", text: "boom", is_error: true }),
      event(6, "assistant_final", { text: "done", model: "m" }),
    ]);
    expect(items.map((item) => item.kind)).toEqual(["user", "work", "answer"]);
    const work = items[1];
    if (work?.kind !== "work") throw new Error("work");
    expect(work.notes).toEqual(["Reading"]);
    expect(work.steps.map((step) => [step.key, step.result])).toEqual([
      ["r1/c1", { text: "hello", isError: false }],
      ["r2/c1", { text: "boom", isError: true }],
    ]);
  });

  it("shows a question with its lead-in, and the answer as the user's message", () => {
    const items = toItems([
      event(1, "assistant_tool_calls", { run_id: "r1", text: "One thing first.", calls: [
        { call_id: "q", tool_id: "ask_user", arguments: { question: "Which **city**?" }, display: { text: "Which city?" } }] }),
      event(2, "user_message", { text: "Seoul", answers_call: "q", answers_run: "r1" }),
    ]);
    expect(items).toEqual([
      { kind: "answer", key: "1:lead", text: "One thing first.", model: null },
      { kind: "question", key: "1", text: "Which city?", callKey: "r1/q" },
      { kind: "user", key: "2", text: "Seoul", answers: true },
    ]);
  });

  it("shows the text without its marks, as the server gives it", () => {
    expect(toItems([event(1, "assistant_final", { text: "[[mood:happy]] Hi", display: { text: "Hi" } })])).toEqual([
      { kind: "answer", key: "1", text: "Hi", model: null },
    ]);
  });

  it("keeps unknown kinds out and a wake as a notice", () => {
    expect(toItems([event(1, "something_new"), event(2, "wake", { text: "back at the desk" })])).toEqual([
      { kind: "notice", key: "2", text: "back at the desk" },
    ]);
  });

  it("names a step by its tool and main argument", () => {
    expect(stepTitle({ key: "k", toolId: "machine/read_file", arguments: { path: "src/a.py" }, result: null })).toBe(
      "read_file · src/a.py");
    expect(stepTitle({ key: "k", toolId: "memory_save", arguments: {}, result: null })).toBe("memory_save");
  });
});

describe("reduceLive", () => {
  it("appends deltas of the same Run and starts over for another", () => {
    let state = reduceLive(EMPTY_LIVE, { event: "snapshot", data: { run_id: "r1", text: "Hel", tools: [], thinking: false } });
    state = reduceLive(state, { event: "delta", data: { run_id: "r1", text: "lo" } });
    expect(state.text).toBe("Hello");
    state = reduceLive(state, { event: "delta", data: { run_id: "r2", text: "Next" } });
    expect([state.runId, state.text]).toEqual(["r2", "Next"]);
  });

  it("clears on end of its Run, idle and done", () => {
    const writing = reduceLive(EMPTY_LIVE, { event: "snapshot", data: { run_id: "r1", text: "x" } });
    expect(reduceLive(writing, { event: "end", data: { run_id: "r0" } }).text).toBe("x");
    expect(reduceLive(writing, { event: "end", data: { run_id: "r1" } }).text).toBe("");
    expect(reduceLive(writing, { event: "done", data: {} })).toEqual({ ...EMPTY_LIVE, done: true });
    expect(reduceLive(writing, { event: "snapshot", data: { run_id: "r1", text: "x", ended: true } }).text).toBe("");
  });
});
