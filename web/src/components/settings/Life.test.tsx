// HIMARI's life screen (character-life §6): whose life it is, the entries under Facts, Plans and Diary, a fact added
// to the life shown, and an entry edited or deleted from the revision it was opened with.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { addLifeFact, deleteLife, editLife, type Life, type LifeEntry } from "@/api/settings";
import { toast } from "@/features/toast";
import { LifeSettings } from "./Life";

const at = "2026-11-25T01:00:00+00:00";
const entry = (id: number, kind: LifeEntry["kind"], day: string | null, text: string, origin: LifeEntry["origin"],
  revision = 1): LifeEntry => ({ id, kind, day, text, origin, revision, created_at: at, updated_at: at });

function aLife(): Life {
  return {
    persona: { id: 2, name: "Friend" },
    enabled: true,
    writer: { running: true, told: "on (deepseek/chat, at 22:00, 200,000 tokens a day each)" },
    entries: [
      entry(5, "fact", null, "고양이를 무서워한다.", "conversation", 3),
      entry(9, "plan", "2026-11-26", "도서관에 간다.", "writer"),
      entry(8, "diary", "2026-11-25", "비가 왔다.\n우산을 잃어버렸다.", "writer"),
      entry(7, "diary", "2026-11-24", "늦잠을 잤다.", "user"),
    ],
    limits: { facts: 200, chars: 500, diary_chars: 1500 },
  };
}

const state: { data: Life } = { data: aLife() };

vi.mock("@/api/settings", () => ({
  readLife: vi.fn(async () => state.data),
  addLifeFact: vi.fn(async () => ({})),
  editLife: vi.fn(async () => ({})),
  deleteLife: vi.fn(async () => undefined),
}));

vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

async function shown() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LifeSettings /></QueryClientProvider>);
  await screen.findByRole("region", { name: "Life" }); // read
}

const part = (name: string) => within(screen.getByRole("region", { name }));

describe("the life screen", () => {
  beforeEach(() => { state.data = aLife(); vi.clearAllMocks(); });
  afterEach(cleanup);

  it("says whose life it is and groups the entries by kind, with their dates and where they came from", async () => {
    await shown();
    expect(await screen.findByText("Friend's life")).toBeTruthy();
    expect(screen.getByText(/Story writer: on \(deepseek/)).toBeTruthy();
    expect(screen.queryByText(/not used now/)).toBeNull();
    expect(part("Facts").getByText("고양이를 무서워한다.")).toBeTruthy();
    expect(part("Facts").getByText("from a conversation")).toBeTruthy();
    expect(part("Facts").getByText("1 of 200")).toBeTruthy();
    expect(part("Plans").getByText("도서관에 간다.")).toBeTruthy();
    expect(part("Plans").getByText("2026-11-26 · written by the story writer")).toBeTruthy();
    expect(part("Plans").queryByText("늦잠을 잤다.")).toBeNull();
    const days = part("Diary").getAllByText(/^2026-11-2\d · /).map((line) => line.textContent);
    expect(days).toEqual(["2026-11-25 · written by the story writer", "2026-11-24 · yours"]); // newest first
    expect(part("Diary").getByText(/우산을 잃어버렸다/)).toBeTruthy();
  });

  it("shows the default's life and says when the life is not used", async () => {
    state.data = { ...aLife(), persona: null, enabled: false, entries: [] };
    await shown();
    expect(await screen.findByText("Default (no persona)")).toBeTruthy();
    expect(screen.getByText(/not used now/)).toBeTruthy();
    expect(part("Facts").getByText("No facts yet.")).toBeTruthy();
    expect(part("Diary").getByText("No diary yet.")).toBeTruthy();
  });

  it("adds a fact to the life shown: the persona's, or the default's", async () => {
    await shown();
    fireEvent.change(await screen.findByLabelText("New fact"), { target: { value: " 바다를 좋아한다. " } });
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    await waitFor(() => expect(addLifeFact).toHaveBeenCalledWith("바다를 좋아한다.", 2));
    cleanup();

    state.data = { ...aLife(), persona: null };
    await shown();
    fireEvent.change(await screen.findByLabelText("New fact"), { target: { value: "산을 좋아한다." } });
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    await waitFor(() => expect(addLifeFact).toHaveBeenLastCalledWith("산을 좋아한다.", null));
  });

  it("says the persona changed when the add is refused for it, and keeps the text", async () => {
    vi.mocked(addLifeFact).mockRejectedValueOnce(new ApiError(409, "persona_changed", "The persona changed."));
    await shown();
    fireEvent.change(await screen.findByLabelText("New fact"), { target: { value: "바다를 좋아한다." } });
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/persona in use changed/), "error"));
    expect((screen.getByLabelText("New fact") as HTMLTextAreaElement).value).toBe("바다를 좋아한다.");
  });
});

describe("an entry", () => {
  beforeEach(() => { state.data = aLife(); vi.clearAllMocks(); });
  afterEach(cleanup);

  it("opens with its text and saves from the revision it was opened with", async () => {
    await shown();
    fireEvent.click(await part("Facts").findByRole("button", { name: "Edit" }));
    const text = screen.getByLabelText("Entry text") as HTMLTextAreaElement;
    expect(text.value).toBe("고양이를 무서워한다.");
    expect(text.maxLength).toBe(500);
    expect(screen.getAllByRole("button", { name: "Edit" }).every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    fireEvent.change(text, { target: { value: "고양이를 좋아하게 됐다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(5, "고양이를 좋아하게 됐다.", 3));
    await waitFor(() => expect(screen.queryByLabelText("Entry text")).toBeNull());
  });

  it("keeps the text and says so when it was changed elsewhere", async () => {
    vi.mocked(editLife).mockRejectedValueOnce(new ApiError(409, "revision_conflict", "Changed."));
    await shown();
    fireEvent.click((await part("Diary").findAllByRole("button", { name: "Edit" }))[0] as HTMLElement);
    const text = screen.getByLabelText("Entry text") as HTMLTextAreaElement;
    expect(text.maxLength).toBe(1500); // a diary entry is longer
    fireEvent.change(text, { target: { value: "맑았다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/changed elsewhere since you opened it; nothing was saved/), "error"));
    expect((screen.getByLabelText("Entry text") as HTMLTextAreaElement).value).toBe("맑았다.");
  });

  it("is deleted only when confirmed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await shown();
    const remove = await part("Plans").findByRole("button", { name: "Delete" });
    fireEvent.click(remove);
    expect(deleteLife).not.toHaveBeenCalled();
    fireEvent.click(remove);
    await waitFor(() => expect(deleteLife).toHaveBeenCalledWith(9, 1));
    confirm.mockRestore();
  });
});
