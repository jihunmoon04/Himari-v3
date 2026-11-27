// Settings → HIMARI → Schedule → Routines (character-routine §13.4): the built-in sleep and diary (only their range and
// length edited, never deleted) and the owner's own routines (added with a trial's last day, edited, turned off,
// deleted after asking); the server's refusal said; a slot placed for a routine shows its name.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { addRoutine, deleteRoutine, editRoutine, readRoutines, type Routine, type Routines } from "@/api/settings";
import { toast } from "@/features/toast";
import { aLife, entry, living, onThursday, part, shownAt } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  readRoutines: vi.fn(),
  addRoutine: vi.fn(async () => ({})),
  editRoutine: vi.fn(async () => ({})),
  deleteRoutine: vi.fn(async () => undefined),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const base = { trial_until: null, trial_ended: false, himari: "move" as const, revision: 1, weekdays: 127,
  end_from: null, end_to: null };
const sleep: Routine = { ...base, id: 1, name: "잠", text: "잠", builtin: "sleep", start_from: "23:00",
  start_to: "02:00", minutes_min: 300, minutes_max: 660, end_from: "07:00", end_to: "10:00", state: "fixed" };
const diary: Routine = { ...base, id: 2, name: "일기", text: "일기 쓰기", builtin: "diary", start_from: "21:00",
  start_to: "21:00", minutes_min: 30, minutes_max: 30, state: "fixed" };
const dinner: Routine = { ...base, id: 3, name: "저녁 식사", text: "저녁 먹는 중", builtin: null, start_from: "18:30",
  start_to: "19:30", minutes_min: 40, minutes_max: 40, weekdays: 31, state: "trial", trial_until: "2026-12-02",
  himari: "move_cancel", revision: 2 };
const walk: Routine = { ...dinner, id: 4, name: "산책", text: "산책 중", trial_until: "2026-11-20", trial_ended: true,
  weekdays: 127, himari: "none" };
const routines = (...list: Routine[]): Routines => ({
  persona: { id: 2, name: "Friend" }, today: "2026-11-26", routines: list,
  limits: { routines: 20, name_chars: 40, text_chars: 100, trial_days: 60, default_trial_days: 7 },
});

const shown = async () => {
  await shownAt("/settings/himari/schedule");
  return within(await screen.findByRole("region", { name: "Routines" }));
};

describe("the Routines", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    onThursday();
    living(aLife());
    vi.mocked(readRoutines).mockResolvedValue(routines(sleep, diary, dinner, walk));
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("lists the built-ins first and each routine in words", async () => {
    const group = await shown();
    expect(within(group.getByLabelText("Routine 잠")).getByText(
      "starts 23:00–02:00 · ends 07:00–10:00 · every night")).toBeTruthy();
    expect(within(group.getByLabelText("Routine 일기")).getByText(
      "from 21:00 until 40 min before the sleep · 30 min · every night")).toBeTruthy();
    expect(within(group.getByLabelText("Routine 저녁 식사")).getByText(
      "starts 18:30–19:30 · 40 min · weekdays · trial until 2026-12-02 · HIMARI may move or skip it")).toBeTruthy();
    expect(within(group.getByLabelText("Routine 산책")).getByText(/trial ended · HIMARI leaves it as it is$/))
      .toBeTruthy();
    expect(within(group.getByLabelText("Routine 잠")).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(group.getByLabelText("Routine 저녁 식사")).getByRole("button", { name: "Delete" })).toBeTruthy();
  });

  it("adds a routine on trial for the days chosen", async () => {
    const group = await shown();
    fireEvent.click(group.getByRole("button", { name: "Add a routine" }));
    fireEvent.change(screen.getByLabelText("Routine name"), { target: { value: " 아침 산책 " } });
    fireEvent.change(screen.getByLabelText("Routine line"), { target: { value: "산책 중" } });
    fireEvent.change(screen.getByLabelText("Earliest start"), { target: { value: "07:30" } });
    fireEvent.change(screen.getByLabelText("Latest start"), { target: { value: "08:00" } });
    fireEvent.click(screen.getByLabelText("Sat"));
    fireEvent.click(screen.getByLabelText("Sun")); // weekdays only
    fireEvent.change(screen.getByLabelText("Trial days"), { target: { value: "3" } });
    expect(screen.getByText(/until 2026-11-28/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(addRoutine).toHaveBeenCalledWith({
      name: "아침 산책", text: "산책 중", start_from: "07:30", start_to: "08:00", minutes_min: 40, minutes_max: 40,
      end_from: null, end_to: null, weekdays: 31, state: "trial", trial_until: "2026-11-28", himari: "move",
    }, 2));
  });

  it("edits a built-in's range only, and keeps a trial's last day unless its days change", async () => {
    const group = await shown();
    fireEvent.click(within(group.getByLabelText("Routine 잠")).getByRole("button", { name: "Edit" }));
    expect(screen.queryByLabelText("Routine name")).toBeNull();
    fireEvent.change(screen.getByLabelText("Latest end"), { target: { value: "09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editRoutine).toHaveBeenCalledWith(1, expect.objectContaining({
      start_from: "23:00", start_to: "02:00", end_from: "07:00", end_to: "09:00" }), 1));
    await waitFor(() => expect(screen.queryByLabelText("Routine editor")).toBeNull());

    fireEvent.click(within(group.getByLabelText("Routine 저녁 식사")).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Earliest start"), { target: { value: "18:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editRoutine).toHaveBeenLastCalledWith(3, expect.objectContaining({
      start_from: "18:00", trial_until: "2026-12-02", state: "trial", himari: "move_cancel" }), 2));
  });

  it("says the server's refusal and deletes only after asking", async () => {
    vi.mocked(editRoutine).mockRejectedValueOnce(new ApiError(409, "routine_refused",
      "A routine's start range runs earliest first, within one day."));
    const group = await shown();
    fireEvent.click(within(group.getByLabelText("Routine 저녁 식사")).getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "off" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "A routine's start range runs earliest first, within one day.", "error"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const row = within(group.getByLabelText("Routine 산책"));
    fireEvent.click(row.getByRole("button", { name: "Delete" }));
    expect(deleteRoutine).not.toHaveBeenCalled();
    fireEvent.click(row.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteRoutine).toHaveBeenCalledWith(4, 2));
  });

  it("shows a slot's routine on the schedule", async () => {
    living({ ...aLife(), entries: [{ ...entry(50, "plan", "2026-11-26", "저녁 먹는 중", "writer"), time: "23:50",
      minutes: 40, plan_kind: "own", routine: { id: 3, name: "저녁 식사", trial: true, himari: "move_cancel" } }] });
    await shown();
    expect(part("Schedule").getByLabelText("Routine").textContent).toBe("저녁 식사 · trial");
  });
});
