import { describe, expect, it } from "vitest";
import type { EventView } from "@/api/himari";
import { stepTitle, toItems } from "./items";
import { EMPTY_LIVE, reduceLive } from "./live";

const at = "2026-11-16T10:00:00Z";
const event = (sequence: number, kind: string, rest: Partial<EventView> = {}): EventView => ({ sequence, kind, created_at: at, ...rest });

describe("work handed to the hub (V3-P102 §5.1)", () => {
  const calls = (id: string, args: Record<string, unknown>, tool = "hub/hub_delegate") =>
    event(Number(id.slice(1)) * 10, "assistant_tool_calls", { run_id: "r", calls: [{ call_id: id, tool_id: tool, arguments: args }] });
  const result = (id: string, text: string, is_error = false) =>
    event(Number(id.slice(1)) * 10 + 1, "tool_result", { run_id: "r", call_id: id, text, is_error });

  it("draws a card after each hub_delegate result: the task, the hub's refusal, or an unreadable answer", () => {
    const items = toItems([
      calls("c1", { project: "work", prompt: "Fix it", agent: "codex" }),
      result("c1", JSON.stringify({ id: 41, status: "queued", agent: "codex", runner: null })),
      calls("c2", { project: "nowhere", prompt: "x" }),
      result("c2", "모르는 프로젝트: nowhere", true),
      calls("c3", { project: "work", prompt: "y", session: 7 }),
      result("c3", "not json"),
      calls("c4", { id: 41, wait_seconds: 30 }, "hub/hub_task"), // following it is not another card
      result("c4", JSON.stringify({ id: 41, status: "done" })),
    ], "hub");
    expect(items.map((i) => i.kind)).toEqual(["work", "delegation", "work", "delegation", "work", "delegation", "work"]);
    const cards = items.flatMap((i) => (i.kind === "delegation" ? [i.delegation] : []));
    expect(cards[0]).toEqual({ taskId: 41, project: "work", agent: "codex", prompt: "Fix it", session: null,
      status: "queued", error: null });
    expect(cards[1]).toMatchObject({ taskId: null, project: "nowhere", error: "모르는 프로젝트: nowhere" });
    expect(cards[2]).toMatchObject({ taskId: null, session: 7, error: "The hub's answer could not be read." });
  });

  it("knows the hub only by the server's name for it", () => {
    const events = [calls("c1", { project: "work", prompt: "p" }), result("c1", JSON.stringify({ id: 1 }))];
    expect(toItems(events).map((i) => i.kind)).toEqual(["work"]); // no hub here
    expect(toItems(events, "other").map((i) => i.kind)).toEqual(["work"]);
    expect(toItems([calls("c1", { project: "w", prompt: "p" }, "other/hub_delegate"),
      result("c1", JSON.stringify({ id: 1 }))], "other").map((i) => i.kind)).toEqual(["work", "delegation"]);
  });
});

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
