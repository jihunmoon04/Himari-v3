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
  await screen.findByRole("region", { name: "HIMARI" }); // read
}

const part = (name: string) => within(screen.getByRole("region", { name }));

describe("the life screen", () => {
  beforeEach(() => { state.data = aLife(); vi.clearAllMocks(); });
  afterEach(cleanup);

  it("says whose life it is and groups the entries by kind, with their dates and where they came from", async () => {
    await shown();
    expect(await screen.findByText("Friend's life")).toBeTruthy();
    expect(screen.getByText(/Life runs: on \(deepseek/)).toBeTruthy();
    expect(screen.queryByLabelText("Mind")).toBeNull();
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

  it("shows a plan's time, HIMARI's mood and what it is doing now (character-state)", async () => {
    state.data = {
      ...aLife(),
      entries: [{ ...entry(9, "plan", "2026-11-26", "도서관에 간다.", "writer"), time: "14:00" }],
      mood: { mood: "happy", set_at: at, source: "writer", reason: "새 책을 찾았다" },
      now: { asleep: false, doing: "빵집 들르기", since: "13:30" },
    };
    await shown();
    expect(part("Plans").getByText("2026-11-26 14:00 · written by the story writer")).toBeTruthy();
    const line = screen.getByLabelText("Now").textContent ?? "";
    expect(line).toContain("Mood: happy (새 책을 찾았다)");
    expect(line).toContain("Doing: 빵집 들르기 (from 13:30)");
    state.data = { ...aLife(), now: { asleep: true, doing: null, since: null } };
    cleanup();
    await shown();
    expect(screen.getByLabelText("Now").textContent).toBe("Asleep");
  });

  it("shows no mood line when there is nothing to tell", async () => {
    await shown();
    expect(screen.queryByLabelText("Now")).toBeNull();
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

  it("shows an arc with its title, status and ending, and edits its title and goal (character-world §4)", async () => {
    state.data = {
      ...aLife(),
      world: { weather: "on (Open-Meteo at 37.57,126.98)", today: "light rain, 3 to 9°C", topics: "on (exa/web_search_exa)" },
      entries: [
        { ...entry(11, "arc", "2026-11-10", "풍경화 한 장", "writer"), title: "그림 연습", status: "open", end_day: null },
        { ...entry(10, "arc", "2026-11-01", "매일 십 분", "writer", 2), title: "우쿨렐레", status: "closed",
          end_day: "2026-11-20", ending: "한 곡을 끝까지 쳤다." },
      ],
      limits: { ...aLife().limits, arc_chars: 200, arc_title_chars: 60 },
    };
    await shown();
    expect(screen.getByLabelText("World").textContent).toBe(
      "Weather: light rain, 3 to 9°C · The day's topics: on (exa/web_search_exa)");
    expect(part("Arcs").getByText("그림 연습")).toBeTruthy();
    expect(part("Arcs").getByText("since 2026-11-10 · open · written by the story writer")).toBeTruthy();
    expect(part("Arcs").getByText("2026-11-01 – 2026-11-20 · closed · written by the story writer")).toBeTruthy();
    expect(part("Arcs").getByText("Ending: 한 곡을 끝까지 쳤다.")).toBeTruthy();
    fireEvent.click(part("Arcs").getAllByRole("button", { name: "Edit" })[0] as HTMLElement);
    const title = screen.getByLabelText("Arc title") as HTMLInputElement;
    const goal = screen.getByLabelText("Arc goal") as HTMLTextAreaElement;
    expect([title.value, title.maxLength, goal.value, goal.maxLength]).toEqual(["그림 연습", 60, "풍경화 한 장", 200]);
    fireEvent.change(title, { target: { value: " 수채화 연습 " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(11, "풍경화 한 장", 1, "수채화 연습"));
  });

  it("shows the projects with their so far and pieces, the worker and the next work plan (character-work §8)",
    async () => {
      const plan = { ...entry(21, "plan", "2026-11-26", "냉각부 도면 그려야지.", "writer"), time: "20:00", arc_id: 11 };
      state.data = {
        ...aLife(),
        work: { running: true, next: plan },
        entries: [
          { ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open",
            end_day: null, so_far: "포신과 냉각부까지 정했다." },
          { ...entry(12, "arc", "2026-11-12", "매일 한 곡", "writer"), title: "작곡 노트", status: "open", end_day: null },
          plan,
          { ...entry(30, "work", "2026-11-25", "냉각부 설계.\n증발열은 포신 뒤로.", "writer"), time: "20:00",
            title: "냉각부", summary: "냉각부 설계 끝냈어.", arc_id: 11, plan_id: 20 },
        ],
        limits: { ...aLife().limits, work_chars: 3000, summary_chars: 200, arc_title_chars: 60 },
      };
      await shown();
      expect(screen.getByLabelText("Work status").textContent).toBe(
        "Work: on · next: 2026-11-26 20:00 냉각부 도면 그려야지. (블랙홀 전자포)");
      const project = within(part("Work").getByLabelText("Project 블랙홀 전자포"));
      expect(project.getByText("So far: 포신과 냉각부까지 정했다.")).toBeTruthy();
      expect(project.getByText("냉각부 설계 끝냈어.")).toBeTruthy();
      expect(project.getByText("2026-11-25 20:00 · written by the story writer")).toBeTruthy();
      expect(project.getByText("Show the piece")).toBeTruthy();
      expect(within(part("Work").getByLabelText("Project 작곡 노트")).getByText("Nothing made yet.")).toBeTruthy();
      expect(part("Plans").getByText("2026-11-26 20:00 · work on 블랙홀 전자포 · written by the story writer"))
        .toBeTruthy();
      fireEvent.click(project.getByRole("button", { name: "Edit" }));
      const summary = screen.getByLabelText("Piece summary") as HTMLInputElement;
      expect([summary.value, summary.maxLength]).toEqual(["냉각부 설계 끝냈어.", 200]);
      expect((screen.getByLabelText("Piece text") as HTMLTextAreaElement).maxLength).toBe(3000);
      fireEvent.change(summary, { target: { value: " 냉각부 끝! " } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(editLife).toHaveBeenCalledWith(
        30, "냉각부 설계.\n증발열은 포신 뒤로.", 1, "냉각부", "냉각부 끝!"));
    });

  it("marks a piece begun at its work time's start as a draft (character-mind §4.4)", async () => {
    state.data = {
      ...aLife(),
      entries: [
        { ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open",
          end_day: null },
        { ...entry(31, "work", "2026-11-26", "첫 스케치.", "writer"), time: "20:00", title: "냉각부 스케치",
          summary: "세 겹 방열판", arc_id: 11, plan_id: 21, draft: true },
      ],
    };
    await shown();
    const project = within(part("Work").getByLabelText("Project 블랙홀 전자포"));
    expect(project.getByText("draft")).toBeTruthy();
    expect(project.getByText("Show the draft")).toBeTruthy();
    expect(project.getByText("세 겹 방열판")).toBeTruthy();
  });

  it("says which model HIMARI's self runs use and why (character-mind §8)", async () => {
    state.data = { ...aLife(), mind: { model: "memory-model", reason: "the memory model (no companion conversation)" } };
    await shown();
    expect(screen.getByLabelText("Mind").textContent)
      .toBe("Mind: memory-model (the memory model (no companion conversation))");
  });

  it("says when the worker is off", async () => {
    state.data = { ...aLife(), work: { running: false, next: null } };
    await shown();
    expect(screen.getByLabelText("Work status").textContent).toBe("Work: off (it runs with the story writer)");
    expect(part("Work").getByText("No projects yet.")).toBeTruthy();
  });

  it("lists the kept thoughts with what they are about, and deletes but never edits one (character-mind §8)",
    async () => {
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      state.data = {
        ...aLife(),
        entries: [{ ...entry(12, "thought", null, "요즘 지쳐 보인다.", "conversation"), about: "user",
          expires_at: "2026-11-28T01:00:00+00:00" }],
      };
      await shown();
      expect(part("Thoughts").getByText("요즘 지쳐 보인다.")).toBeTruthy();
      expect(part("Thoughts").getByText(/^about you · kept .* · until .* · from a conversation$/)).toBeTruthy();
      expect(part("Thoughts").queryByRole("button", { name: "Edit" })).toBeNull();
      fireEvent.click(part("Thoughts").getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(deleteLife).toHaveBeenCalledWith(12, 1));
      expect(part("Diary").getByText("No diary yet.")).toBeTruthy();
      confirm.mockRestore();
    });

  it("lists what the world made happen under Events, edited like any entry, and when it next runs", async () => {
    state.data = {
      ...aLife(),
      entries: [{ ...entry(14, "event", "2026-11-25", "택배가 늦게 왔다.", "writer"), time: "15:00" }],
      world: { weather: "on", today: null, topics: "off", next: "2026-11-25T23:00:00+00:00" },
    };
    await shown();
    expect(part("Events").getByText("택배가 늦게 왔다.")).toBeTruthy();
    expect(part("Events").getByText("2026-11-25 15:00 · written by the story writer")).toBeTruthy();
    expect(screen.getByLabelText("World").textContent).toContain("World: next");
    fireEvent.click(part("Events").getByRole("button", { name: "Edit" }));
    const text = await screen.findByLabelText("Entry text");
    fireEvent.change(text, { target: { value: "택배가 일찍 왔다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(14, "택배가 일찍 왔다.", 1));
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
