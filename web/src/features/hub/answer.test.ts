import { describe, expect, it } from "vitest";
import type { HubPending } from "@/api/hub";
import { answerContent, hasOwnReject, whereFrom } from "./answer";

describe("answering an agent", () => {
  it("sends only what was filled, and a choice as its own value", () => {
    const fields = [
      { name: "count", choices: [{ value: 1, title: "One" }, { value: 2, title: "Two" }] },
      { name: "tags", multiple: true, choices: [{ value: "a" }, { value: "b" }] },
      { name: "note" },
      { name: "skipped" },
    ];
    expect(answerContent(fields, { count: "2", tags: ["b", "a"], note: "hi", skipped: "" }))
      .toEqual({ count: 2, tags: ["b", "a"], note: "hi" });
    expect(answerContent(fields, { tags: [] })).toEqual({});
    expect(answerContent(fields, { note: "free text", count: "3" })).toEqual({ note: "free text", count: "3" });
  });

  it("knows the agent's own refusal and says where a request comes from", () => {
    const item: HubPending = {
      id: 1, task_id: 9, kind: "permission", asks: "Run pytest",
      task: { project: "demo", agent: "claude", runner: "linux" },
      options: [{ option_id: "a", name: "Allow", kind: "allow_once" }, { option_id: "r", name: "Reject", kind: "reject_once" }],
    };
    expect(hasOwnReject(item)).toBe(true);
    expect(hasOwnReject({ ...item, options: [item.options![0]!] })).toBe(false);
    expect(whereFrom(item)).toBe("demo · claude · on linux");
    expect(whereFrom({ ...item, task: { project: "demo", runner: "local" } })).toBe("demo");
    expect(whereFrom({ ...item, task: undefined })).toBe("task 9");
  });
});
