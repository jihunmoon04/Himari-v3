// Settings → HIMARI → Today (character-routine §9.2): HIMARI now (life, mind, work, world, mood, doing, sleep), its
// slots for today and tomorrow only read with the way to the Schedule tab, and today's events edited like any entry.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { editLife } from "@/api/settings";
import { aLife, at, entry, living, onThursday, part, shownAt, where } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  editLife: vi.fn(async () => ({})),
  deleteLife: vi.fn(async () => undefined),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const slot = (id: number, day: string, time: string, text: string, extra: object = {}) =>
  ({ ...entry(id, "plan", day, text, "himari"), time, minutes: 60, plan_kind: "own" as const, ...extra });

describe("the Today tab", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(aLife()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("tells HIMARI's state: life, mind, world, mood and what it does", async () => {
    living({
      ...aLife(),
      mind: { model: "memory-model", reason: "the memory model (no companion conversation)" },
      world: { weather: "on (Open-Meteo at 37.57,126.98)", today: "light rain, 3 to 9°C", topics: "on (exa/web_search_exa)",
        next: "2026-11-26T23:00:00+00:00" },
      mood: { mood: "happy", set_at: at, source: "writer", reason: "새 책을 찾았다" },
      now: { asleep: false, doing: "빵집 들르기", since: "13:30" },
    });
    await shownAt();
    expect(screen.getByText(/Life runs: on \(deepseek/)).toBeTruthy();
    expect(screen.getByLabelText("Mind").textContent)
      .toBe("Mind: memory-model (the memory model (no companion conversation))");
    expect(screen.getByLabelText("World").textContent).toMatch(
      /^Weather: light rain, 3 to 9°C · The day's topics: on \(exa\/web_search_exa\) · World: next /);
    const line = screen.getByLabelText("Now").textContent ?? "";
    expect(line).toContain("Mood: happy (새 책을 찾았다)");
    expect(line).toContain("Doing: 빵집 들르기 (from 13:30)");
  });

  it("shows no Now line when there is nothing to tell, and says asleep or the sleep", async () => {
    await shownAt();
    expect(screen.queryByLabelText("Now")).toBeNull();
    expect(screen.queryByLabelText("Mind")).toBeNull();
    cleanup();
    living({ ...aLife(), now: { asleep: true, doing: null, since: null } });
    await shownAt();
    expect(screen.getByLabelText("Now").textContent).toBe("Asleep");
    cleanup();
    const tonight = { day: "2026-11-26", time: "23:30", minutes: 460 };
    living({ ...aLife(), sleep: { state: "drowsy", minutes_to_sleep: 20, tonight } });
    await shownAt();
    expect(screen.getByLabelText("Now").textContent).toBe("Sleep: Drowsy · sleeps in 20 min");
    cleanup();
    living({ ...aLife(), now: { asleep: true, doing: null, since: null }, sleep: { state: "asleep", deep: true, tonight } });
    await shownAt();
    expect(screen.getByLabelText("Now").textContent).toBe("Sleep: Asleep (deep) · wakes at 07:10");
  });

  it("says whether the worker runs and the next work time", async () => {
    const plan = { ...slot(21, "2026-11-27", "20:00", "냉각부 도면 그려야지."), plan_kind: "work" as const, arc_id: 11 };
    living({
      ...aLife(),
      work: { running: true, next: plan },
      entries: [{ ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open" },
        plan],
    });
    await shownAt();
    expect(screen.getByLabelText("Work status").textContent).toBe(
      "Work: on · next: 2026-11-27 20:00 냉각부 도면 그려야지. (블랙홀 전자포)");
    cleanup();
    living({ ...aLife(), work: { running: false, next: null } });
    await shownAt();
    expect(screen.getByLabelText("Work status").textContent).toBe("Work: off (it runs with the story writer)");
  });

  it("shows only today's and tomorrow's slots, only read, with the way to the Schedule tab", async () => {
    living({
      ...aLife(),
      entries: [
        slot(50, "2026-11-28", "10:00", "모레 일"),
        slot(41, "2026-11-27", "03:00", "방송 같이 보기", { plan_kind: "together" }),
        { ...slot(40, "2026-11-26", "23:30", "잠", { plan_kind: "sleep" }), minutes: 460 },
        slot(42, "2026-11-26", "15:00", "산책", { plan_state: "cancelled", reason: "비가 와서 못 나가" }),
        slot(43, "2026-11-25", "21:00", "어제 일"),
      ],
    });
    await shownAt();
    const days = part("Today and tomorrow");
    expect(days.getAllByText(/^2026-11-2\d/).map((line) => line.textContent))
      .toEqual(["2026-11-26 · today", "2026-11-27 · tomorrow"]);
    const today = within(days.getByLabelText("Day 2026-11-26"));
    expect(today.getAllByText(/^\d\d:\d\d–/).map((line) => line.textContent))
      .toEqual(["15:00–16:00 · 1h", "23:30–07:10 · 7h 40m"]);
    expect(today.getByText("Why: 비가 와서 못 나가")).toBeTruthy();
    expect(days.queryByText("어제 일")).toBeNull();
    expect(days.queryByText("모레 일")).toBeNull();
    expect(days.queryAllByRole("button")).toHaveLength(0); // moved and changed on the Schedule tab
    fireEvent.click(days.getByRole("link", { name: /Schedule tab/ }));
    expect(where()).toBe("/settings/himari/schedule");
  });

  it("lists today's events, edited like any entry, and none of another day", async () => {
    living({
      ...aLife(),
      entries: [
        { ...entry(14, "event", "2026-11-26", "택배가 늦게 왔다.", "writer"), time: "15:00" },
        { ...entry(13, "event", "2026-11-25", "어제의 일.", "writer"), time: "18:00" },
      ],
    });
    await shownAt();
    const events = part("Today's events");
    expect(events.getByText("2026-11-26 15:00 · written by the story writer")).toBeTruthy();
    expect(events.queryByText("어제의 일.")).toBeNull();
    fireEvent.click(events.getByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Entry text"), { target: { value: "택배가 일찍 왔다." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(14, "택배가 일찍 왔다.", 1));
  });

  it("says when nothing is on today", async () => {
    living({ ...aLife(), entries: [] });
    await shownAt();
    expect(part("Today and tomorrow").getByText("Nothing on the schedule today or tomorrow.")).toBeTruthy();
    expect(part("Today's events").getByText("Nothing happened today yet.")).toBeTruthy();
  });
});
