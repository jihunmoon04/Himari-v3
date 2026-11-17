// What a hub session changed and planned, for its right-hand panel (V3-P102 §4.4). Pure, tested in changes.test.ts.
//
// - Changes: every tool event's ``diffs``, by turn (a turn starts at ``turn_start``) and then by file. An update of
//   a call that repeats its diffs replaces them (the last word of the call), so a call is counted once.
// - Plan: every ``plan`` event, oldest first, with how much of it was done.
import { diffLines } from "diff";
import type { HubEvent } from "@/api/hub";
import type { Diff } from "@/features/timeline";

export interface TurnChanges {
  id: number; // the seq of its turn_start (or of its first change): stays when earlier events are read
  turn: number; // 1, 2, … in the events held (earlier turns not read yet are not counted): for its label only
  prompt: string;
  diffs: Diff[];
}

const str = (value: unknown) => (typeof value === "string" ? value : "");
const num = (value: unknown) => (typeof value === "number" ? value : 0);

function diffsOf(value: unknown): Diff[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((d): d is Record<string, unknown> => Boolean(d) && typeof d === "object")
    .map((d) => ({ path: str(d.path), old: typeof d.old === "string" ? d.old : null, new: str(d.new),
      cut: d.cut === true, more: num(d.more) }));
}

/** The diffs of ``events`` by turn; turns without any change are left out. */
export function changesByTurn(events: readonly HubEvent[]): TurnChanges[] {
  const turns: { id: number; turn: number; prompt: string; calls: Map<string, Diff[]> }[] = [];
  let current: (typeof turns)[number] | null = null;
  for (const event of events) {
    const body = event.body ?? {};
    if (event.kind === "turn_start") {
      current = { id: event.seq, turn: turns.length + 1, prompt: str(body.prompt), calls: new Map() };
      turns.push(current);
      continue;
    }
    if (event.kind !== "tool" || !("diffs" in body)) continue;
    if (current === null) { // changes before the first turn held: a turn of their own
      current = { id: event.seq, turn: 1, prompt: "", calls: new Map() };
      turns.push(current);
    }
    current.calls.set(`${event.attempt ?? ""}/${str(body.id) || String(event.seq)}`, diffsOf(body.diffs));
  }
  return turns
    .map((t) => ({ id: t.id, turn: t.turn, prompt: t.prompt, diffs: [...t.calls.values()].flat() }))
    .filter((t) => t.diffs.length > 0);
}

export interface FileChanges {
  path: string;
  diffs: Diff[]; // in the order they were made
  added: number;
  removed: number;
  created: boolean; // the first change made the file
}

export function lineCounts(diff: Diff): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const part of diffLines(diff.old ?? "", diff.new)) {
    if (part.added) added += part.count ?? 0;
    else if (part.removed) removed += part.count ?? 0;
  }
  return { added, removed };
}

/** The diffs of the turns given, by file (in the order each file was first changed). */
export function byFile(turns: readonly TurnChanges[]): FileChanges[] {
  const files = new Map<string, FileChanges>();
  for (const turn of turns) {
    for (const diff of turn.diffs) {
      const file = files.get(diff.path) ?? { path: diff.path, diffs: [], added: 0, removed: 0, created: diff.old === null };
      const counts = lineCounts(diff);
      file.diffs.push(diff);
      file.added += counts.added;
      file.removed += counts.removed;
      files.set(diff.path, file);
    }
  }
  return [...files.values()];
}

export interface PlanVersion {
  key: string;
  at: string | null;
  entries: { content: string; status: string }[];
  done: number;
}

/** Every plan the agent wrote, oldest first. */
export function planHistory(events: readonly HubEvent[]): PlanVersion[] {
  const out: PlanVersion[] = [];
  for (const event of events) {
    if (event.kind !== "plan") continue;
    const raw = (event.body ?? {}).entries;
    const entries = Array.isArray(raw)
      ? raw.filter((e): e is Record<string, unknown> => Boolean(e) && typeof e === "object")
          .map((e) => ({ content: str(e.content), status: str(e.status) }))
      : [];
    out.push({ key: String(event.seq), at: event.at ?? null, entries,
      done: entries.filter((e) => e.status === "completed").length });
  }
  return out;
}

export interface SplitRow {
  left: string | null; // the line before (null: nothing on this side)
  right: string | null; // the line after
  kind: "same" | "changed" | "removed" | "added" | "gap";
}

/** A diff as side-by-side rows: a removed run beside the added run that replaced it; long unchanged runs keep
 *  their first and last two lines around a gap. At most ``limit`` rows; ``more`` says some were left out. */
export function splitRows(diff: Diff, limit = 400): { rows: SplitRow[]; more: boolean } {
  const rows: SplitRow[] = [];
  const parts = diffLines(diff.old ?? "", diff.new);
  const lines = (value: string) => value.replace(/\n$/, "").split("\n");
  for (let index = 0; index < parts.length && rows.length <= limit; index += 1) { // one past: is there more?
    const part = parts[index]!;
    if (!part.added && !part.removed) {
      const same = lines(part.value);
      const shown = same.length > 5 ? [...same.slice(0, 2), null, ...same.slice(-2)] : same;
      for (const line of shown) rows.push(line === null ? { left: null, right: null, kind: "gap" } : { left: line, right: line, kind: "same" });
      continue;
    }
    if (part.removed && parts[index + 1]?.added) {
      const before = lines(part.value);
      const after = lines(parts[index + 1]!.value);
      for (let n = 0; n < Math.max(before.length, after.length); n += 1) {
        rows.push({ left: before[n] ?? null, right: after[n] ?? null, kind: "changed" });
      }
      index += 1;
      continue;
    }
    for (const line of lines(part.value)) {
      rows.push(part.added ? { left: null, right: line, kind: "added" } : { left: line, right: null, kind: "removed" });
    }
  }
  const more = rows.length > limit;
  return { rows: rows.slice(0, limit), more };
}
