// Settings → HIMARI → Schedule (character-day §12, character-routine §9.2): the slots from today on by day (a slot of
// a day before today is not here), written, edited and deleted; the sleep and diary slots only moved; together slots
// left to HIMARI; the schedule's refusal said.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { addPlan, deleteLife, editPlan, type Life } from "@/api/settings";
import { toast } from "@/features/toast";
import { aLife, entry, living, onThursday, part, shownAt } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  deleteLife: vi.fn(async () => undefined),
  addPlan: vi.fn(async () => ({})),
  editPlan: vi.fn(async () => ({})),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const sleepSlot = { ...entry(40, "plan", "2026-11-26", "잠", "writer", 4), time: "23:30", minutes: 460,
  plan_kind: "sleep" as const };
const withSchedule = (): Life => ({
  ...aLife(),
  entries: [
    { ...entry(41, "plan", "2026-11-27", "방송 같이 보기", "himari"), time: "03:00", minutes: 120,
      plan_kind: "together" as const },
    sleepSlot,
    { ...entry(42, "plan", "2026-11-26", "산책", "himari"), time: "15:00", minutes: 60, plan_kind: "own" as const,
      plan_state: "cancelled" as const, reason: "비가 와서 못 나가" },
    { ...entry(43, "plan", "2026-11-25", "같이 게임", "himari"), time: "21:00", minutes: 90,
      plan_kind: "together" as const, plan_state: "missed" as const },
    { ...entry(44, "plan", "2026-11-24", "지난 산책", "user"), time: "10:00", minutes: 30, plan_kind: "own" as const },
  ],
});
const shown = () => shownAt("/settings/himari/schedule");

describe("the Schedule tab", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(aLife()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("shows the slots from today on by day, with their kind, time, length, state and why; none from before", async () => {
    living(withSchedule());
    await shown();
    const schedule = part("Schedule");
    expect(screen.getByText(/from today on; the sleep and diary slots only move/)).toBeTruthy();
    expect(schedule.getAllByText(/^2026-11-2\d/).map((line) => line.textContent))
      .toEqual(["2026-11-26 · today", "2026-11-27 · tomorrow"]);
    expect(schedule.queryByLabelText("Day 2026-11-25")).toBeNull(); // in that date's record
    expect(schedule.queryByText("같이 게임")).toBeNull();
    expect(schedule.queryByText("지난 산책")).toBeNull();
    const today = within(schedule.getByLabelText("Day 2026-11-26"));
    expect(today.getAllByText(/^\d\d:\d\d–/).map((line) => line.textContent))
      .toEqual(["15:00–16:00 · 1h", "23:30–07:10 · 7h 40m"]); // by start
    expect(today.getByText("Sleep")).toBeTruthy();
    expect(today.getByText("cancelled")).toBeTruthy();
    expect(today.getByText("Why: 비가 와서 못 나가")).toBeTruthy();
    const tomorrow = within(schedule.getByLabelText("Day 2026-11-27"));
    expect(tomorrow.getByText("Together")).toBeTruthy();
    expect(tomorrow.getByText("by HIMARI")).toBeTruthy();
  });

  it("starts at the server's today, not the browser's (review 3)", async () => {
    living({ ...withSchedule(), today: "2026-11-27", since: "2026-10-28" }); // the browser says 11-26
    await shown();
    const schedule = part("Schedule");
    expect(schedule.getAllByText(/^2026-11-2\d/).map((line) => line.textContent)).toEqual(["2026-11-27 · today"]);
    fireEvent.click(schedule.getByRole("button", { name: "Add a slot" }));
    expect((screen.getByLabelText("Slot day") as HTMLSelectElement).value).toBe("2026-11-27");
  });

  it("says the persona changed when a slot's add is refused for it, and keeps what was written", async () => {
    vi.mocked(addPlan).mockRejectedValueOnce(new ApiError(409, "persona_changed", "The persona changed."));
    await shown();
    fireEvent.click(part("Schedule").getByRole("button", { name: "Add a slot" }));
    fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "16:00" } });
    fireEvent.change(screen.getByLabelText("Slot text"), { target: { value: "책 읽기" } });
    fireEvent.click(screen.getByRole("button", { name: "Add slot" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "The persona in use changed since this page was read; nothing was added.", "error"));
    expect((screen.getByLabelText("Slot text") as HTMLTextAreaElement).value).toBe("책 읽기");
  });

  it("says when nothing is on from today", async () => {
    living({ ...aLife(), entries: [withSchedule().entries[3]!] });
    await shown();
    expect(part("Schedule").getByText("Nothing on the schedule from today on.")).toBeTruthy();
  });

  it("deletes a slot only when confirmed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await shown();
    const remove = await part("Schedule").findByRole("button", { name: "Delete" });
    fireEvent.click(remove);
    expect(deleteLife).not.toHaveBeenCalled();
    fireEvent.click(remove);
    await waitFor(() => expect(deleteLife).toHaveBeenCalledWith(9, 1));
    confirm.mockRestore();
  });

  it("only moves the sleep slot, never deletes it, and leaves a cancelled or missed slot unedited", async () => {
    living({ ...aLife(), entries: [sleepSlot, withSchedule().entries[2]!] });
    await shown();
    const schedule = part("Schedule");
    expect(schedule.getAllByRole("button", { name: "Delete" })).toHaveLength(1); // the cancelled one's
    expect(schedule.queryByRole("button", { name: "Edit" })).toBeNull();
    fireEvent.click(schedule.getByRole("button", { name: "Move" }));
    expect(screen.queryByLabelText("Slot text")).toBeNull();
    expect((screen.getByLabelText("Slot kind") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Slot day"), { target: { value: "2026-11-27" } });
    fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "01:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editPlan).toHaveBeenCalledWith(40,
      { day: "2026-11-27", time: "01:00", minutes: 460, plan_kind: "sleep", text: "잠" }, 4));
  });

  it("only moves the diary slot (10 to 60 minutes), never deletes it, and never offers it new (character-diary §10)",
    async () => {
      const diarySlot = { ...entry(45, "plan", "2026-11-26", "일기 쓰기", "writer", 2), time: "22:10", minutes: 30,
        plan_kind: "diary" as const };
      living({ ...aLife(), entries: [diarySlot, sleepSlot] });
      await shown();
      const schedule = part("Schedule");
      expect(schedule.getByText("Diary")).toBeTruthy();
      expect(schedule.getByText("22:10–22:40 · 30m")).toBeTruthy();
      expect(schedule.queryByRole("button", { name: "Delete" })).toBeNull();
      expect(schedule.queryByRole("button", { name: "Edit" })).toBeNull();
      fireEvent.click(schedule.getAllByRole("button", { name: "Move" })[0] as HTMLElement); // the diary slot, by start
      expect(screen.queryByLabelText("Slot text")).toBeNull();
      const kind = screen.getByLabelText("Slot kind") as HTMLSelectElement;
      expect([...kind.options].map((o) => o.value)).toEqual(["diary"]);
      expect(kind.disabled).toBe(true);
      const minutes = screen.getByLabelText("Slot minutes") as HTMLInputElement;
      expect([minutes.min, minutes.max]).toEqual(["10", "60"]);
      fireEvent.change(minutes, { target: { value: "70" } });
      expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
      fireEvent.change(minutes, { target: { value: "5" } });
      expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
      fireEvent.change(minutes, { target: { value: "40" } });
      fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "21:40" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(editPlan).toHaveBeenCalledWith(45,
        { day: "2026-11-26", time: "21:40", minutes: 40, plan_kind: "diary", text: "일기 쓰기" }, 2));
      await waitFor(() => expect(screen.queryByLabelText("Slot editor")).toBeNull());
      fireEvent.click(part("Schedule").getByRole("button", { name: "Add a slot" }));
      expect([...(screen.getByLabelText("Slot kind") as HTMLSelectElement).options].map((o) => o.value))
        .toEqual(["own"]);
    });

  it("adds a slot to the persona's schedule, from today to six days on", async () => {
    await shown();
    fireEvent.click(part("Schedule").getByRole("button", { name: "Add a slot" }));
    const day = screen.getByLabelText("Slot day") as HTMLSelectElement;
    expect(day.value).toBe("2026-11-26");
    expect([...day.options].map((o) => o.value)).toEqual(
      ["2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02"]);
    const kind = screen.getByLabelText("Slot kind") as HTMLSelectElement;
    expect([...kind.options].map((o) => o.value)).toEqual(["own"]); // no open project: no work slot; never together
    expect(kind.disabled).toBe(true);
    fireEvent.change(day, { target: { value: "2026-11-27" } });
    fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "03:00" } });
    fireEvent.change(screen.getByLabelText("Slot minutes"), { target: { value: "120" } });
    fireEvent.change(screen.getByLabelText("Slot text"), { target: { value: " 방송 보기 " } });
    fireEvent.click(screen.getByRole("button", { name: "Add slot" }));
    await waitFor(() => expect(addPlan).toHaveBeenCalledWith(
      { day: "2026-11-27", time: "03:00", minutes: 120, plan_kind: "own", text: "방송 보기" }, 2));
    await waitFor(() => expect(screen.queryByLabelText("Slot text")).toBeNull());
  });

  it("leaves together slots to HIMARI: none offered new or from an own slot; one may stay so or turn own (review 14)",
    async () => {
      living({
        ...aLife(),
        entries: [
          withSchedule().entries[0]!, // together, 2026-11-27 03:00
          { ...entry(44, "plan", "2026-11-26", "책 읽기", "user"), time: "23:00", minutes: 30, plan_kind: "own" as const },
          { ...entry(60, "arc", "2026-11-20", "노래를 만든다.", "writer"), title: "첫 노래", status: "open" as const },
        ],
      });
      await shown();
      const kinds = () => [...(screen.getByLabelText("Slot kind") as HTMLSelectElement).options].map((o) => o.value);
      fireEvent.click(part("Schedule").getByRole("button", { name: "Add a slot" }));
      expect(kinds()).toEqual(["own", "work"]);
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      fireEvent.click(within(part("Schedule").getByLabelText("Day 2026-11-26")).getByRole("button", { name: "Edit" }));
      expect(kinds()).toEqual(["own"]);
      expect((screen.getByLabelText("Slot kind") as HTMLSelectElement).disabled).toBe(true);
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      fireEvent.click(within(part("Schedule").getByLabelText("Day 2026-11-27")).getByRole("button", { name: "Edit" }));
      expect(kinds()).toEqual(["together", "own"]);
      fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "04:00" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(editPlan).toHaveBeenCalledWith(41,
        { day: "2026-11-27", time: "04:00", minutes: 120, plan_kind: "together", text: "방송 같이 보기" }, 1));
    });

  it("names a work slot's project", async () => {
    living({
      ...aLife(),
      entries: [
        { ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open" },
        { ...entry(21, "plan", "2026-11-26", "냉각부 도면 그려야지.", "writer"), time: "20:00", arc_id: 11 },
      ],
    });
    await shown();
    expect(part("Schedule").getByText("work on 블랙홀 전자포 · written by the story writer")).toBeTruthy();
  });

  it("says why the schedule refused a slot and keeps what was written", async () => {
    vi.mocked(addPlan).mockRejectedValueOnce(new ApiError(409, "plan_refused",
      "It overlaps the sleep slot (23:30-07:10); move the sleep slot first."));
    await shown();
    fireEvent.click(part("Schedule").getByRole("button", { name: "Add a slot" }));
    fireEvent.change(screen.getByLabelText("Slot start"), { target: { value: "23:50" } });
    fireEvent.change(screen.getByLabelText("Slot text"), { target: { value: "책 읽기" } });
    fireEvent.click(screen.getByRole("button", { name: "Add slot" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "The schedule did not take it: It overlaps the sleep slot (23:30-07:10); move the sleep slot first.", "error"));
    expect((screen.getByLabelText("Slot text") as HTMLTextAreaElement).value).toBe("책 읽기");
  });
});
