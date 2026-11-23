import { describe, expect, it } from "vitest";
import type { EventView } from "@/api/himari";
import { stepTitle, toItems } from "./items";
import { EMPTY_LIVE, reduceLive } from "./live";

const at = "2026-11-16T10:00:00Z";
const event = (sequence: number, kind: string, rest: Partial<EventView> = {}): EventView => ({ sequence, kind, created_at: at, ...rest });

describe("files, references and memory (V3-P102 W6b)", () => {
  it("gives a message its files and references, and a Tool result its files and memory line", () => {
    const items = toItems([
      event(1, "user_message", { text: "look", attachments: [
        { file_id: "f1", version: 1, name: "a.png", size: 10, image: { state: "ready", preview: { width: 40, height: 20 } } },
        { file_id: "f2", version: 2, name: "old.txt", size: 5, expired: true }],
        references: [{ path: "/w/a.py", name: "a.py", whole: true }, { path: "/w/b.py", name: "b.py", whole: false, start_line: 3, end_line: 9 }] }),
      event(2, "assistant_tool_calls", { run_id: "r", calls: [{ call_id: "c", tool_id: "memory_save", arguments: { text: "x" } }] }),
      event(3, "tool_result", { run_id: "r", call_id: "c", text: "saved", memory: { action: "saved", id: "m1", text: "Likes tea.", undo: true },
        presented_files: [{ file_id: "f3", version: 1, name: "report.pdf", size: 99 }] }),
    ], "conv_1");
    expect(items.map((i) => i.kind)).toEqual(["user", "work", "files", "memory"]);
    const user = items[0];
    if (user?.kind !== "user") throw new Error("user");
    expect(user.files).toEqual([
      { key: "f1/1", name: "a.png", size: 10, url: "/api/conversations/conv_1/files/f1/1",
        preview: "/api/conversations/conv_1/files/f1/1/view", width: 40, height: 20 },
      { key: "f2/2", name: "old.txt", size: 5, url: null, preview: null, width: null, height: null },
    ]);
    expect(user.references).toEqual(["a.py", "b.py:3-9"]);
    expect(items[3]).toEqual({ kind: "memory", key: "3:memory", run: "r", call: "c", action: "saved", text: "Likes tea.", undo: true });
    const files = items[2];
    if (files?.kind !== "files") throw new Error("files");
    expect(files.files[0]).toMatchObject({ name: "report.pdf", url: "/api/conversations/conv_1/files/f3/1", preview: null });
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

  it("shows an ask_user call left in old history as any other tool step", () => {
    const items = toItems([
      event(1, "assistant_tool_calls", { run_id: "r1", text: "One thing first.", calls: [
        { call_id: "q", tool_id: "ask_user", arguments: { question: "Which city?" } }] }),
      event(2, "user_message", { text: "Seoul" }),
    ]);
    expect(items.map((item) => item.kind)).toEqual(["work", "user"]);
    const work = items[0];
    if (work?.kind !== "work") throw new Error("work");
    expect(work.notes).toEqual(["One thing first."]);
    expect(work.steps.map((step) => [step.toolId, step.result])).toEqual([["ask_user", null]]);
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
