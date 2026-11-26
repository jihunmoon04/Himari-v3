// The timeline's diary lines (character-day §7, character-diary §10): the diary written, folded with its text, and
// what was added to it when the session closed, a line with its time only.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EMPTY_LIVE } from "@/features/himari/live";
import type { Item } from "@/features/timeline";
import { Timeline } from "./Timeline";

const time = (at: string) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

describe("the diary in a day session's timeline", () => {
  afterEach(cleanup);

  it("folds the diary written with its text, and shows what was added as a line without text", () => {
    const items: Item[] = [
      { kind: "diary", key: "diary:lw_9", at: "2026-11-26T13:00:00Z", text: "오늘은 방송을 같이 봤다.", step: "write" },
      { kind: "diary", key: "diary:lw_12", at: "2026-11-26T18:00:00Z", text: "", step: "amend" },
    ];
    render(<Timeline items={items} live={EMPTY_LIVE} running={false} conversation="c1" />);
    const [written, added] = screen.getAllByLabelText("Diary");
    expect(written?.tagName).toBe("DETAILS");
    expect(written?.querySelector("summary")?.textContent).toBe(`Wrote the diary · ${time("2026-11-26T13:00:00Z")}`);
    expect(written?.textContent).toContain("오늘은 방송을 같이 봤다.");
    expect(added?.tagName).toBe("P");
    expect(added?.textContent).toBe(`Added to the diary · ${time("2026-11-26T18:00:00Z")}`);
  });
});
