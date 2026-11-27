// Settings → HIMARI → Records (character-routine §9.2): a line a date, newest first (today too, nothing after it),
// saying what the date holds; opened, its diary and events (edited), its slots only read (a past slot, cancelled or
// missed, with why), its sleeps and its pieces; "Show older" reads on through /api/life/history.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { editLife, readLifeHistory, type Life, type LifeHistory, type SleepRecord } from "@/api/settings";
import { toast } from "@/features/toast";
import { aLife, entry, living, onThursday, part, shownAt } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  editLife: vi.fn(async () => ({})),
  deleteLife: vi.fn(async () => undefined),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const night = (id: number, fell: string, woke: string | null, extra: Partial<SleepRecord> = {}): SleepRecord => ({
  id, fell_at: new Date(fell).toISOString(), how_fell: "schedule", woke_at: woke && new Date(woke).toISOString(),
  how_woke: woke ? "schedule" : null, messages: 0, mumbles: 0, closed_day: false, ...extra,
});

const withRecords = (): Life => ({
  ...aLife(),
  entries: [
    entry(5, "fact", null, "고양이를 무서워한다.", "conversation", 3),
    { ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open" },
    { ...entry(50, "plan", "2026-11-27", "내일 산책", "himari"), time: "10:00", minutes: 60, plan_kind: "own" },
    { ...entry(9, "plan", "2026-11-26", "도서관에 간다.", "writer"), time: "14:00", minutes: 60, plan_kind: "own" },
    { ...entry(30, "work", "2026-11-25", "냉각부 설계.", "writer"), time: "20:00", title: "냉각부",
      summary: "냉각부 설계 끝냈어.", arc_id: 11, plan_id: 20 },
    { ...entry(43, "plan", "2026-11-25", "같이 게임", "himari"), time: "21:00", minutes: 90,
      plan_kind: "together", plan_state: "missed" },
    { ...entry(42, "plan", "2026-11-25", "산책", "himari"), time: "15:00", minutes: 60, plan_kind: "own",
      plan_state: "cancelled", reason: "비가 와서 못 나가" },
    { ...entry(14, "event", "2026-11-25", "택배가 늦게 왔다.", "writer"), time: "15:00" },
    { ...entry(15, "event", "2026-11-25", "유나가 왔다.", "writer"), time: "18:00" },
    entry(8, "diary", "2026-11-25", "비가 왔다.\n우산을 잃어버렸다.", "writer"),
    entry(7, "diary", "2026-11-24", "늦잠을 잤다.", "user"),
  ],
  sleeps: [night(2, "2026-11-25T23:40:00", "2026-11-26T07:10:00", { messages: 3, mumbles: 1, closed_day: true })],
});
const page = (from: string, before: string, more: boolean, entries: LifeHistory["entries"] = [],
  sleeps: SleepRecord[] = []): LifeHistory => ({ entries, sleeps, from, before, more });
const shown = () => shownAt("/settings/himari/records");
const record = (day: string) => within(screen.getByRole("listitem", { name: `Record ${day}` }));
const lines = () => part("Records").getAllByRole("button", { expanded: false }).map((b) => b.textContent);

describe("the Records tab", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(withRecords()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("lists a line a date, newest first, from today back, saying what each holds", async () => {
    await shown();
    expect(lines()).toEqual([
      "2026-11-26 (Thu) · today · 1 slot▸",
      "2026-11-25 (Wed) · diary · 2 events · 2 slots · 1 piece · sleep▸",
      "2026-11-24 (Tue) · diary▸",
    ]);
    expect(part("Records").queryByText(/2026-11-27/)).toBeNull(); // the Schedule tab's
    expect(part("Records").queryByText("고양이를 무서워한다.")).toBeNull();
    expect(readLifeHistory).not.toHaveBeenCalled(); // until asked for
  });

  it("opens a date: its diary and events edited, its past slots only read with why, its sleep and pieces", async () => {
    await shown();
    fireEvent.click(record("2026-11-25").getByRole("button", { expanded: false }));
    const day = record("2026-11-25");
    expect(within(day.getByLabelText("Diary")).getByText(/우산을 잃어버렸다/)).toBeTruthy();
    expect(within(day.getByLabelText("Events")).getAllByText(/^2026-11-25 \d\d:00 · /).map((l) => l.textContent))
      .toEqual(["2026-11-25 15:00 · written by the story writer", "2026-11-25 18:00 · written by the story writer"]);
    const slots = within(day.getByLabelText("Slots"));
    expect(slots.getAllByText(/^\d\d:\d\d–/).map((line) => line.textContent))
      .toEqual(["15:00–16:00 · 1h", "21:00–22:30 · 1h 30m"]);
    expect(slots.getByText("cancelled")).toBeTruthy();
    expect(slots.getByText("Why: 비가 와서 못 나가")).toBeTruthy();
    expect(slots.getByText("missed")).toBeTruthy();
    expect(slots.queryAllByRole("button")).toHaveLength(0); // a past slot is only read
    expect(within(day.getByLabelText("Sleep"))
      .getByText("fell asleep at its sleep slot · woke at its slot's end · 3 messages, 1 mumble · closed the day"))
      .toBeTruthy();
    const pieces = within(day.getByLabelText("Pieces"));
    expect(pieces.getByText("냉각부 설계 끝냈어.")).toBeTruthy();
    expect(pieces.getByText("2026-11-25 20:00 · work on 블랙홀 전자포 · written by the story writer")).toBeTruthy();

    fireEvent.click(within(day.getByLabelText("Diary")).getByRole("button", { name: "Edit" }));
    const text = screen.getByLabelText("Entry text") as HTMLTextAreaElement;
    expect(text.maxLength).toBe(1500); // a diary entry is longer
    expect(within(day.getByLabelText("Events")).getAllByRole("button", { name: "Edit" })
      .every((b) => (b as HTMLButtonElement).disabled)).toBe(true); // one open at a time
    fireEvent.change(text, { target: { value: "맑았다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(8, "맑았다.", 1));
  });

  it("keeps the text and says so when the diary was changed elsewhere", async () => {
    vi.mocked(editLife).mockRejectedValueOnce(new ApiError(409, "revision_conflict", "Changed."));
    await shown();
    fireEvent.click(record("2026-11-24").getByRole("button", { expanded: false }));
    fireEvent.click(record("2026-11-24").getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Entry text"), { target: { value: "일찍 일어났다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      expect.stringMatching(/changed elsewhere since you opened it; nothing was saved/), "error"));
    expect((screen.getByLabelText("Entry text") as HTMLTextAreaElement).value).toBe("일찍 일어났다.");
  });

  it("reads older dates on with Show older, from where the last read stopped, until nothing is older", async () => {
    living(withRecords(),
      page("2026-09-27", "2026-10-27", true, [
        { ...entry(70, "plan", "2026-10-20", "옛 산책", "himari"), time: "09:00", minutes: 30, plan_kind: "own",
          plan_state: "cancelled", reason: "늦잠" },
        { ...entry(71, "event", "2026-10-20", "옛날 일.", "writer"), time: "12:00" },
        entry(7, "diary", "2026-11-24", "늦잠을 잤다.", "user"), // the same entry again: listed once
      ], [night(1, "2026-10-19T23:00:00", "2026-10-20T07:00:00", { how_woke: "user" })]),
      page("2026-08-28", "2026-09-27", false, [entry(72, "diary", "2026-09-01", "구월의 일기.", "writer")]));
    await shown();
    fireEvent.click(screen.getByRole("button", { name: "Show older" }));
    await screen.findByRole("listitem", { name: "Record 2026-10-20" });
    expect(readLifeHistory).toHaveBeenLastCalledWith(undefined); // from where /api/life stops
    expect(lines().slice(2)).toEqual([
      "2026-11-24 (Tue) · diary▸", "2026-10-20 (Tue) · 1 event · 1 slot▸", "2026-10-19 (Mon) · sleep▸"]);
    fireEvent.click(record("2026-10-20").getByRole("button", { expanded: false }));
    expect(within(record("2026-10-20").getByLabelText("Slots")).getByText("Why: 늦잠")).toBeTruthy();
    fireEvent.click(record("2026-10-19").getByRole("button", { expanded: false }));
    expect(within(record("2026-10-19").getByLabelText("Sleep")).getByText(/woken by you/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Show older" }));
    await screen.findByRole("listitem", { name: "Record 2026-09-01" });
    expect(readLifeHistory).toHaveBeenLastCalledWith("2026-09-27");
    expect(await screen.findByText("Nothing older.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show older" })).toBeNull();
    // an older date's entry is edited like any
    fireEvent.click(record("2026-10-20").getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Entry text"), { target: { value: "옛날의 일." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(71, "옛날의 일.", 1));
  });

  it("says when nothing is recorded and when older dates could not be read", async () => {
    living({ ...aLife(), entries: [], sleeps: [] });
    vi.mocked(readLifeHistory).mockRejectedValueOnce(new Error("down"));
    await shown();
    expect(part("Records").getByText("Nothing recorded in the last 30 days.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show older" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Show older" })).toBeTruthy(); // tried again when asked
  });
});
