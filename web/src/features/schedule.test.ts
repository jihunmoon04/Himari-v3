import { describe, expect, it } from "vitest";
import type { LifeEntry } from "@/api/settings";
import { addDays, dayLabel, endOf, lengthOf, scheduleDays, sleepNote, sleepText, slotsByDay, spanOf } from "./schedule";

const at = "2026-11-26T01:00:00+00:00";
const plan = (id: number, day: string, time: string | null, minutes: number | null = null): LifeEntry =>
  ({ id, kind: "plan", day, time, minutes, text: `p${id}`, origin: "himari", revision: 1, created_at: at, updated_at: at });

describe("the schedule's days and times (character-day §4)", () => {
  it("counts days over a month's and a year's end", () => {
    expect(addDays("2026-11-30", 1)).toBe("2026-12-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(scheduleDays("2026-11-26")).toEqual(
      ["2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02"]);
  });

  it("gives a slot its end, wrapping past midnight, and its length in words", () => {
    expect(endOf("23:30", 460)).toBe("07:10");
    expect(endOf("09:05", 55)).toBe("10:00");
    expect([lengthOf(45), lengthOf(120), lengthOf(460)]).toEqual(["45m", "2h", "7h 40m"]);
    expect(spanOf({ time: "03:00", minutes: 120 })).toBe("03:00–05:00 · 2h");
    expect(spanOf({ time: "14:00", minutes: null })).toBe("14:00"); // a plan from before the schedule
    expect(spanOf({ time: null, minutes: 60 })).toBe("");
  });

  it("groups the plans by day, earliest first, each by start", () => {
    const days = slotsByDay([plan(1, "2026-11-27", "03:00"), plan(2, "2026-11-26", "23:30"),
      plan(3, "2026-11-26", "15:00"), { ...plan(4, "2026-11-26", "10:00"), kind: "diary" }]);
    expect(days.map(({ day, slots }) => [day, slots.map((slot) => slot.id)])).toEqual([
      ["2026-11-26", [3, 2]], ["2026-11-27", [1]]]);
  });

  it("names today and tomorrow", () => {
    expect([dayLabel("2026-11-26", "2026-11-26"), dayLabel("2026-11-27", "2026-11-26"), dayLabel("2026-11-28", "2026-11-26")])
      .toEqual(["2026-11-26 · today", "2026-11-27 · tomorrow", "2026-11-28"]);
  });
});

describe("HIMARI's sleep in words (character-day §5)", () => {
  const tonight = { day: "2026-11-26", time: "23:30", minutes: 460 };

  it("says each state", () => {
    expect(sleepText({ state: "awake", tonight })).toBe("Awake · sleeps at 23:30");
    expect(sleepText({ state: "drowsy", minutes_to_sleep: 25, tonight })).toBe("Drowsy · sleeps in 25 min");
    expect(sleepText({ state: "asleep", deep: false, tonight })).toBe("Asleep (light) · wakes at 07:10");
    expect(sleepText({ state: "half_asleep" })).toBe("Half asleep");
    expect(sleepText({ state: "awake" })).toBe("Awake");
  });

  it("says how a night went", () => {
    expect(sleepNote({ fell_at: at, how_fell: "missed", woke_at: at, how_woke: "schedule", messages: 1, mumbles: 0,
      closed_day: true })).toBe("fell asleep waiting for a promise · woke at its slot's end · 1 message · closed the day");
  });
});
