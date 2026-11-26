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

describe("HIMARI's thoughts (character-mind §3.2, §3.3)", () => {
  it("keeps the thought of a first Run that called a Tool, folded on its work block (review P1-6)", () => {
    const items = toItems([
      event(1, "assistant_tool_calls", { run_id: "r1", thought: "날씨부터 보자.", text: "[[think]]날씨부터 보자.[[/think]]",
        display: { text: "" }, calls: [{ call_id: "c", tool_id: "weather", arguments: {} }] }),
      event(2, "tool_result", { run_id: "r1", call_id: "c", text: "맑음" }),
      event(3, "assistant_final", { text: "맑대!", thought: null }),
    ]);
    const work = items[0];
    if (work?.kind !== "work") throw new Error("work");
    expect(work.thoughts).toEqual(["날씨부터 보자."]);
    expect(work.notes).toEqual([]);
  });

  it("shows a free wake it let pass as a dim line with its thought (review P1-5)", () => {
    const items = toItems([event(4, "wake_thought", { text: "spoke on its own", thought: "회의 중이다. 두자." })]);
    expect(items).toEqual([{ kind: "notice", key: "4", text: "Let pass: spoke on its own", thought: "회의 중이다. 두자." }]);
  });
});

describe("a day session (character-day §5.6, §7)", () => {
  it("marks a message HIMARI got asleep with how it took it, and leaves the others plain", () => {
    const items = toItems([
      event(1, "user_message", { text: "자?", while_asleep: "none" }),
      event(2, "user_message", { text: "자냐고", while_asleep: "mumble" }),
      event(3, "user_message", { text: "일어나!", while_asleep: "wake" }),
      event(4, "user_message", { text: "안녕", while_asleep: null }),
    ]);
    expect(items.map((item) => (item.kind === "user" ? item.asleep ?? "-" : item.kind))).toEqual(["none", "mumble", "wake", "-"]);
  });

  it("shows the diary written from the session as its own folded line, after the turn it follows", () => {
    const items = toItems([
      event(5, "assistant_tool_calls", { run_id: "r", calls: [{ call_id: "c", tool_id: "schedule_read", arguments: {} }] }),
      event(5, "diary", { id: "lw_9", created_at: "2026-11-26T13:00:00Z", text: "오늘은 방송을 같이 봤다." }),
    ]);
    expect(items.map((item) => item.kind)).toEqual(["work", "diary"]);
    expect(items[1]).toEqual({ kind: "diary", key: "diary:lw_9", at: "2026-11-26T13:00:00Z", text: "오늘은 방송을 같이 봤다." });
  });

  it("keys each diary on its id, not the sequence it follows, and shows one that comes again once (review 9)", () => {
    const items = toItems([
      event(5, "user_message", { text: "잘 자" }),
      event(5, "diary", { id: "lw_1", text: "첫 일기" }),
      event(5, "diary", { id: "lw_2", text: "다음 일기" }), // the same sequence: nothing new between them
      event(5, "diary", { id: "lw_1", text: "첫 일기" }), // the next page gave it again
      event(6, "user_message", { text: "좋은 아침" }),
    ]);
    expect(items.map((item) => item.key)).toEqual(["5", "diary:lw_1", "diary:lw_2", "6"]);
  });

  it("tells diaries without an id (an older server) apart by order", () => {
    const items = toItems([event(5, "diary", { text: "하나" }), event(5, "diary", { text: "둘" })]);
    expect(items.map((item) => item.key)).toEqual(["5:diary:0", "5:diary:1"]);
  });
});

describe("a wake dropped unshown (review 2)", () => {
  it("is worded as not said, not as one the model let pass", () => {
    const items = toItems([event(5, "wake_thought", { text: "spoke on its own", thought: "말할까 했다.", status: "dropped" })]);
    expect(items).toEqual([{ kind: "notice", key: "5", text: "Not said: spoke on its own", thought: "말할까 했다." }]);
  });
});
