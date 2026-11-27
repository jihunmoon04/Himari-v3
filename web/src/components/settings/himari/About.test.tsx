// Settings → HIMARI → About HIMARI (character-life §6, character-mind §8; character-routine §9.2): the facts (added to
// the life shown up to the limit, edited from the revision opened, deleted when confirmed) and the kept thoughts (only
// deleted).
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { addLifeFact, deleteLife, editLife } from "@/api/settings";
import { toast } from "@/features/toast";
import { aLife, entry, living, onThursday, part, shownAt } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  addLifeFact: vi.fn(async () => ({})),
  editLife: vi.fn(async () => ({})),
  deleteLife: vi.fn(async () => undefined),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const shown = () => shownAt("/settings/himari/about");

describe("the About HIMARI tab", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(aLife()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("lists the facts with where they came from and how many of the limit; nothing dated", async () => {
    await shown();
    expect(part("Facts").getByText("고양이를 무서워한다.")).toBeTruthy();
    expect(part("Facts").getByText("from a conversation")).toBeTruthy();
    expect(part("Facts").getByText("1 of 200")).toBeTruthy();
    expect(screen.queryByText("도서관에 간다.")).toBeNull();
    expect(screen.queryByText("늦잠을 잤다.")).toBeNull();
    expect(part("Thoughts").getByText("No kept thoughts.")).toBeTruthy();
  });

  it("adds a fact to the life shown: the persona's, or the default's", async () => {
    await shown();
    fireEvent.change(await screen.findByLabelText("New fact"), { target: { value: " 바다를 좋아한다. " } });
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    await waitFor(() => expect(addLifeFact).toHaveBeenCalledWith("바다를 좋아한다.", 2));
    cleanup();

    living({ ...aLife(), persona: null });
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

  it("is full only when all the facts at the limit are yours", async () => {
    const facts = (origin: "user" | "writer") => [entry(1, "fact", null, "하나.", "user"), entry(2, "fact", null, "둘.", origin)];
    living({ ...aLife(), entries: facts("user"), limits: { ...aLife().limits, facts: 2 } });
    await shown();
    expect(screen.getByText("Full of your own facts; delete one first.")).toBeTruthy();
    cleanup();
    living({ ...aLife(), entries: facts("writer"), limits: { ...aLife().limits, facts: 2 } });
    await shown();
    expect(screen.queryByText(/Full of your own facts/)).toBeNull();
  });

  it("edits a fact from the revision it was opened with, one at a time", async () => {
    living({ ...aLife(), entries: [...aLife().entries, entry(6, "fact", null, "비를 좋아한다.", "user")] });
    await shown();
    fireEvent.click(part("Facts").getAllByRole("button", { name: "Edit" })[0] as HTMLElement);
    const text = screen.getByLabelText("Entry text") as HTMLTextAreaElement;
    expect(text.value).toBe("고양이를 무서워한다.");
    expect(text.maxLength).toBe(500);
    expect(screen.getAllByRole("button", { name: "Edit" }).every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    fireEvent.change(text, { target: { value: "고양이를 좋아하게 됐다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(5, "고양이를 좋아하게 됐다.", 3));
    await waitFor(() => expect(screen.queryByLabelText("Entry text")).toBeNull());
  });

  it("deletes a fact only when confirmed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await shown();
    const remove = part("Facts").getByRole("button", { name: "Delete" });
    fireEvent.click(remove);
    expect(deleteLife).not.toHaveBeenCalled();
    fireEvent.click(remove);
    await waitFor(() => expect(deleteLife).toHaveBeenCalledWith(5, 3));
    confirm.mockRestore();
  });

  it("lists the kept thoughts with what they are about, and deletes but never edits one (character-mind §8)",
    async () => {
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      living({
        ...aLife(),
        entries: [{ ...entry(12, "thought", null, "요즘 지쳐 보인다.", "conversation"), about: "user",
          expires_at: "2026-11-28T01:00:00+00:00" }],
      });
      await shown();
      expect(part("Thoughts").getByText("요즘 지쳐 보인다.")).toBeTruthy();
      expect(part("Thoughts").getByText(/^about you · kept .* · until .* · from a conversation$/)).toBeTruthy();
      expect(part("Thoughts").queryByRole("button", { name: "Edit" })).toBeNull();
      fireEvent.click(part("Thoughts").getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(deleteLife).toHaveBeenCalledWith(12, 1));
      confirm.mockRestore();
    });
});
