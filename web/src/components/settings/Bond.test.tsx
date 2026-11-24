// The bond screen (character-bond §1): whose bond it is, the days together and today's days, the stage set from the
// revision read, an entry added (a day to mark needs its day), edited from the revision it was opened with or deleted,
// and the moments together, newest first.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { addBond, deleteBond, editBond, setBondStage, type Bond, type BondEntry } from "@/api/settings";
import { toast } from "@/features/toast";
import { BondSettings, localDay } from "./Bond";

const at = "2026-11-20T12:00:00+00:00";
const entry = (id: number, tag: BondEntry["tag"], text: string, origin: BondEntry["origin"] = "conversation",
  monthDay: string | null = null, revision = 1): BondEntry =>
  ({ id, tag, text, month_day: monthDay, origin, revision, created_at: at, updated_at: at });

function aBond(): Bond {
  return {
    persona: { id: 2, name: "Lucy" },
    enabled: true,
    stage: { stage: "familiar", since: at, reason: "b3", revision: 2 },
    together_days: 100,
    since: "2026-08-18",
    today: ["100 days together"],
    entries: [
      entry(3, "name", "사용자를 대장이라고 부른다.", "conversation", null, 2),
      entry(5, "promise", "주말마다 영화 얘기 하기.", "user"),
      entry(7, "date", "사용자 생일", "conversation", "03-02"),
    ],
    memories: [
      { kind: "bond", id: 7, at: "2026-11-24T12:00:00+00:00", tag: "date", text: "사용자 생일" },
      { kind: "episode", id: 4, at: "2026-11-22T12:00:00+00:00", text: "같이 발표 자료를 다듬었다.", title: "발표" },
      { kind: "episode", id: 2, at: "2026-11-21T12:00:00+00:00", text: "산책 이야기를 했다.", title: null },
    ],
    limits: { entries: 100, chars: 300 },
  };
}

const state: { data: Bond } = { data: aBond() };

vi.mock("@/api/settings", async (original) => ({
  ...(await original<typeof import("@/api/settings")>()),
  readBond: vi.fn(async () => state.data),
  addBond: vi.fn(async () => ({})),
  editBond: vi.fn(async () => ({})),
  deleteBond: vi.fn(async () => undefined),
  setBondStage: vi.fn(async () => ({})),
}));

vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

async function shown() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><BondSettings /></QueryClientProvider>);
  await screen.findByRole("region", { name: "Bond" }); // read
}

const part = (name: string) => within(screen.getByRole("region", { name }));

describe("the bond screen", () => {
  beforeEach(() => { state.data = aBond(); vi.clearAllMocks(); });
  afterEach(cleanup);

  it("says whose bond it is, the days together, today's days, the stage and the entries", async () => {
    await shown();
    expect(screen.getByText("Lucy and you")).toBeTruthy();
    expect(screen.getByText("Together 100 days, since 2026-08-18.")).toBeTruthy();
    expect(screen.getByText("Today: 100 days together")).toBeTruthy();
    expect((part("Stage").getByLabelText("Stage") as HTMLSelectElement).value).toBe("familiar");
    const settled = part("Settled between you");
    expect(settled.getByText("3 of 100")).toBeTruthy();
    expect(settled.getByText("사용자를 대장이라고 부른다.")).toBeTruthy();
    expect(settled.getByText("Day to mark · 03-02 · from a conversation")).toBeTruthy();
    expect(settled.getByText("Promise · yours")).toBeTruthy();
    expect(screen.queryByText(/not used now/)).toBeNull();
  });

  it("shows the moments together newest first, with the episodes' conversations", async () => {
    await shown();
    const lines = part("Moments together").getAllByText(/^2026-11-2\d · /).map((line) => line.textContent);
    expect(lines).toEqual([`${localDay("2026-11-24T12:00:00+00:00")} · Day to mark`,
      `${localDay("2026-11-22T12:00:00+00:00")} · 발표`, `${localDay("2026-11-21T12:00:00+00:00")} · A conversation`]);
    expect(part("Moments together").getByText("같이 발표 자료를 다듬었다.")).toBeTruthy();
  });

  it("shows the default's bond, no days before a conversation, and says when it is not used", async () => {
    state.data = { ...aBond(), persona: null, enabled: false, since: null, together_days: 0, today: [], entries: [],
      memories: [] };
    await shown();
    expect(screen.getByText("Default (no persona) and you")).toBeTruthy();
    expect(screen.queryByText(/^Together/)).toBeNull();
    expect(screen.getByText(/not used now/)).toBeTruthy();
    expect(screen.getByText("Nothing settled yet.")).toBeTruthy();
    expect(screen.getByText("Nothing yet.")).toBeTruthy();
  });

  it("sets the stage from the revision read, for the persona shown", async () => {
    await shown();
    fireEvent.change(part("Stage").getByLabelText("Stage"), { target: { value: "new" } });
    await waitFor(() => expect(setBondStage).toHaveBeenCalledWith("new", 2, 2));
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/Stage set to new/));
  });

  it("says the persona changed when the stage is refused for it", async () => {
    vi.mocked(setBondStage).mockRejectedValueOnce(new ApiError(409, "persona_changed", "Changed."));
    await shown();
    fireEvent.change(part("Stage").getByLabelText("Stage"), { target: { value: "close" } });
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/persona in use changed/), "error"));
  });
});

describe("an entry of the bond", () => {
  beforeEach(() => { state.data = aBond(); vi.clearAllMocks(); });
  afterEach(cleanup);

  it("is added with its kind, a day to mark only with its day", async () => {
    await shown();
    fireEvent.change(screen.getByLabelText("New bond entry"), { target: { value: " 감자 사건 " } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "joke" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(addBond).toHaveBeenCalledWith("joke", "감자 사건", null, 2));
    await waitFor(() => expect((screen.getByLabelText("New bond entry") as HTMLTextAreaElement).value).toBe(""));

    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "date" } });
    fireEvent.change(screen.getByLabelText("New bond entry"), { target: { value: "처음 만난 날" } });
    const add = screen.getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true); // no day yet
    fireEvent.change(screen.getByLabelText("Month and day"), { target: { value: "08-18" } });
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    await waitFor(() => expect(addBond).toHaveBeenLastCalledWith("date", "처음 만난 날", "08-18", 2));
  });

  it("keeps what was written and says so when the persona changed", async () => {
    vi.mocked(addBond).mockRejectedValueOnce(new ApiError(409, "persona_changed", "Changed."));
    await shown();
    fireEvent.change(screen.getByLabelText("New bond entry"), { target: { value: "대장" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/persona in use changed/), "error"));
    expect((screen.getByLabelText("New bond entry") as HTMLTextAreaElement).value).toBe("대장");
  });

  it("opens with its kind and text and saves from the revision it was opened with", async () => {
    await shown();
    fireEvent.click(part("Settled between you").getAllByRole("button", { name: "Edit" })[0] as HTMLElement);
    const text = screen.getByLabelText("Bond entry text") as HTMLTextAreaElement;
    expect(text.value).toBe("사용자를 대장이라고 부른다.");
    expect(text.maxLength).toBe(300);
    const editor = within(screen.getByRole("form", { name: "Bond entry editor" }));
    expect((editor.getByLabelText("Kind") as HTMLSelectElement).value).toBe("name");
    fireEvent.change(text, { target: { value: "사용자를 선장이라고 부른다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editBond).toHaveBeenCalledWith(3, "name", "사용자를 선장이라고 부른다.", null, 2));
    await waitFor(() => expect(screen.queryByLabelText("Bond entry text")).toBeNull());
  });

  it("keeps the text when it was changed elsewhere", async () => {
    vi.mocked(editBond).mockRejectedValueOnce(new ApiError(409, "revision_conflict", "Changed."));
    await shown();
    fireEvent.click(part("Settled between you").getAllByRole("button", { name: "Edit" })[2] as HTMLElement);
    expect((screen.getByLabelText("Month and day") as HTMLInputElement).value).toBe("03-02");
    fireEvent.change(screen.getByLabelText("Bond entry text"), { target: { value: "생일" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/changed elsewhere/), "error"));
    expect((screen.getByLabelText("Bond entry text") as HTMLTextAreaElement).value).toBe("생일");
  });

  it("is deleted only when confirmed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await shown();
    const remove = part("Settled between you").getAllByRole("button", { name: "Delete" })[1] as HTMLElement;
    fireEvent.click(remove);
    expect(deleteBond).not.toHaveBeenCalled();
    fireEvent.click(remove);
    await waitFor(() => expect(deleteBond).toHaveBeenCalledWith(5, 1));
    confirm.mockRestore();
  });
});
