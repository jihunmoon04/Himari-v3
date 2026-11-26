// character-day §4, §12: HIMARI's schedule as Settings → HIMARI shows it — its slots by day, each with its start,
// length, kind and state — and its sleep in words. Pure functions, tested in schedule.test.ts.
import type { LifeEntry, PlanKind, SleepRecord, SleepView } from "@/api/settings";

/** The schedule's days: today and the six after it (character-day §4.4). */
export const SCHEDULE_DAYS = 7;

const pad = (value: number) => String(value).padStart(2, "0");

/** ``at``'s date on this browser's clock, YYYY-MM-DD. */
export const localDay = (at: Date) => `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;

/** The day ``count`` days after ``day`` (YYYY-MM-DD). */
export function addDays(day: string, count: number): string {
  const [year, month, date] = day.split("-").map(Number);
  return localDay(new Date(year ?? 1970, (month ?? 1) - 1, (date ?? 1) + count));
}

/** The days a slot may be written on: today and the six after it. */
export const scheduleDays = (today: string) => Array.from({ length: SCHEDULE_DAYS }, (_, index) => addDays(today, index));

/** Whether a slot of this kind only moves (its day, start and length): the night's sleep, and (character-diary §3) its
 *  diary time. The owner never deletes one, never makes one, and never turns it into another kind or another into it. */
export const onlyMoves = (kind: PlanKind) => kind === "sleep" || kind === "diary";

/** A slot's length limits in minutes, as the server's: a diary slot 10..60 (character-diary §3), any other 5..720. */
export const minutesOf = (kind: PlanKind): { min: number; max: number } =>
  kind === "diary" ? { min: 10, max: 60 } : { min: 5, max: 720 };

/** When a slot ends, HH:MM (past midnight it wraps: a sleep slot from 23:30 for 460 minutes ends at 07:10). */
export function endOf(time: string, minutes: number): string {
  const [hours, mins] = time.split(":").map(Number);
  const end = ((hours ?? 0) * 60 + (mins ?? 0) + minutes) % (24 * 60);
  return `${pad(Math.floor(end / 60))}:${pad(end % 60)}`;
}

/** A length in words: "45m", "2h", "7h 40m". */
export function lengthOf(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours === 0 ? `${rest}m` : rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
}

/** A slot's time: "23:30–07:10 · 7h 40m" (a plan from before the schedule has no length: its start only). */
export function spanOf(entry: Pick<LifeEntry, "time" | "minutes">): string {
  if (!entry.time) return "";
  return entry.minutes ? `${entry.time}–${endOf(entry.time, entry.minutes)} · ${lengthOf(entry.minutes)}` : entry.time;
}

/** The plans by day, earliest first, each day's slots by start. */
export function slotsByDay(entries: readonly LifeEntry[]): { day: string; slots: LifeEntry[] }[] {
  const days = new Map<string, LifeEntry[]>();
  for (const entry of entries) {
    if (entry.kind !== "plan" || !entry.day) continue;
    days.set(entry.day, [...(days.get(entry.day) ?? []), entry]);
  }
  return [...days.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, slots]) => ({ day, slots: slots.sort((a, b) => (a.time ?? "").localeCompare(b.time ?? "")) }));
}

/** A day's heading: its date, and "today" or "tomorrow" when it is. */
export function dayLabel(day: string, today: string): string {
  if (day === today) return `${day} · today`;
  if (day === addDays(today, 1)) return `${day} · tomorrow`;
  return day;
}

/** HIMARI's sleep now, in a few words (character-day §5.1). */
export function sleepText(sleep: SleepView): string {
  const slot = sleep.tonight;
  switch (sleep.state) {
    case "awake":
      return `Awake${slot ? ` · sleeps at ${slot.time}` : ""}`;
    case "drowsy":
      return `Drowsy${sleep.minutes_to_sleep != null ? ` · sleeps in ${sleep.minutes_to_sleep} min`
        : slot ? ` · sleeps at ${slot.time}` : ""}`;
    case "asleep":
      return `Asleep${sleep.deep ? " (deep)" : sleep.deep === false ? " (light)" : ""}`
        + `${slot ? ` · wakes at ${endOf(slot.time, slot.minutes)}` : ""}`;
    case "half_asleep":
      return "Half asleep";
    default:
      return sleep.state;
  }
}

const FELL: Record<SleepRecord["how_fell"], string> = {
  schedule: "fell asleep at its sleep slot",
  tool: "went to sleep early",
  missed: "fell asleep waiting for a promise",
};
const WOKE: Record<NonNullable<SleepRecord["how_woke"]>, string> = {
  schedule: "woke at its slot's end",
  user: "woken by you",
};

/** One night of the sleep log after its times: how it fell asleep and woke, what came meanwhile, whether the day
 * closed with it. */
export function sleepNote(sleep: SleepRecord): string {
  const parts = [FELL[sleep.how_fell] ?? sleep.how_fell,
    sleep.woke_at === null ? "still asleep" : sleep.how_woke ? WOKE[sleep.how_woke] ?? sleep.how_woke : "woke"];
  if (sleep.messages) parts.push(`${sleep.messages} ${sleep.messages === 1 ? "message" : "messages"}`
    + `${sleep.mumbles ? `, ${sleep.mumbles} ${sleep.mumbles === 1 ? "mumble" : "mumbles"}` : ""}`);
  if (sleep.closed_day) parts.push("closed the day");
  return parts.join(" · ");
}
