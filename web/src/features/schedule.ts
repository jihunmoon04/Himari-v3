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

// --- character-routine §9.2: the Records tab, a line a day -------------------------------------------------------

/** A date's weekday, short: "Thu". */
export function weekdayOf(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year ?? 1970, (month ?? 1) - 1, date ?? 1).toLocaleDateString("en", { weekday: "short" });
}

/** One date of HIMARI's record: its diary, events, slots and work pieces (each by start) and the sleeps that fell
 *  then (earliest first). */
export interface DayRecord {
  day: string;
  diary: LifeEntry[];
  events: LifeEntry[];
  slots: LifeEntry[];
  pieces: LifeEntry[];
  sleeps: SleepRecord[];
}

/** The record by date, newest first, from today back: the dated entries (each once, whichever read brought it) and
 *  the sleeps by the date they fell on this browser's clock. Nothing after today (the Schedule tab has it). */
export function recordDays(entries: readonly LifeEntry[], sleeps: readonly SleepRecord[], today: string): DayRecord[] {
  const days = new Map<string, DayRecord>();
  const of = (day: string) => {
    let found = days.get(day);
    if (!found) {
      found = { day, diary: [], events: [], slots: [], pieces: [], sleeps: [] };
      days.set(day, found);
    }
    return found;
  };
  const seen = new Set<number>();
  for (const entry of entries) {
    if (!entry.day || entry.day > today || seen.has(entry.id)) continue;
    const group = entry.kind === "diary" ? "diary" : entry.kind === "event" ? "events" : entry.kind === "plan" ? "slots"
      : entry.kind === "work" ? "pieces" : null;
    if (group === null) continue;
    seen.add(entry.id);
    of(entry.day)[group].push(entry);
  }
  const slept = new Set<string>();
  for (const sleep of sleeps) {
    const key = sleep.id != null ? String(sleep.id) : sleep.fell_at;
    const day = localDay(new Date(sleep.fell_at));
    if (slept.has(key) || day > today) continue;
    slept.add(key);
    of(day).sleeps.push(sleep);
  }
  const byTime = (a: { time?: string | null }, b: { time?: string | null }) => (a.time ?? "").localeCompare(b.time ?? "");
  return [...days.values()]
    .sort((a, b) => b.day.localeCompare(a.day))
    .map((record) => ({
      ...record, events: record.events.sort(byTime), slots: record.slots.sort(byTime),
      pieces: record.pieces.sort(byTime),
      sleeps: record.sleeps.sort((a, b) => a.fell_at.localeCompare(b.fell_at)),
    }));
}

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** A date's line: "2026-11-26 (Thu) · diary · 2 events · 3 slots · sleep" — only what the date holds. */
export function recordLine(record: DayRecord, today: string): string {
  const parts = [`${record.day} (${weekdayOf(record.day)})${record.day === today ? " · today" : ""}`];
  if (record.diary.length) parts.push("diary");
  if (record.events.length) parts.push(count(record.events.length, "event", "events"));
  if (record.slots.length) parts.push(count(record.slots.length, "slot", "slots"));
  if (record.pieces.length) parts.push(count(record.pieces.length, "piece", "pieces"));
  if (record.sleeps.length) parts.push(record.sleeps.length === 1 ? "sleep" : `${record.sleeps.length} sleeps`);
  return parts.join(" · ");
}
