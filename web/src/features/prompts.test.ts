import { describe, expect, it } from "vitest";
import type { PromptInfo } from "@/api/himari";
import { matchPrompts } from "./prompts";

const prompt = (name: string, title: string | null = null): PromptInfo =>
  ({ source: "hub", name, title, description: null, arguments: [] });

describe("the / picker", () => {
  it("offers names and titles that hold the query, names that start with it first", () => {
    const all = [prompt("summarize"), prompt("review", "Review a change"), prompt("pre-review"), prompt("plan")];
    expect(matchPrompts(all, "rev").map((p) => p.name)).toEqual(["review", "pre-review"]);
    expect(matchPrompts(all, "change").map((p) => p.name)).toEqual(["review"]);
    expect(matchPrompts(all, "").map((p) => p.name)).toEqual(["summarize", "review", "pre-review", "plan"]);
    expect(matchPrompts(Array.from({ length: 12 }, (_, n) => prompt(`p${n}`)), "p")).toHaveLength(8);
  });
});
