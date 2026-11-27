// Settings → HIMARI → Schedule → Routines (character-routine §13.4): the persona's routines, which the server places on
// HIMARI's schedule each day they are on. The built-in sleep and diary come first and change only in their range and
// length (never turned off or deleted); the owner's own are added, edited (a trial with its last day, fixed, off; their
// weekdays; what HIMARI may do with a slot of one) and deleted. A change places the routine's unmoved slots again;
// turning one off or deleting it cancels them. Edited from the revision opened (a change made elsewhere is refused).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  addRoutine, deleteRoutine, editRoutine, readRoutines, type Routine, type RoutineInput, type RoutineSay,
  type RoutineState, type Routines,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import { addDays } from "@/features/schedule";
import { toast } from "@/features/toast";
import { field, Group, KEY, refused, useUnsaved, why } from "./shared";

export const ROUTINES_KEY = ["settings", "routines"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const STATES: Record<RoutineState, string> = { trial: "Trial", fixed: "Fixed", off: "Off" };
const SAYS: Record<RoutineSay, string> = {
  move: "HIMARI may move it", move_cancel: "HIMARI may move or skip it", none: "HIMARI leaves it as it is",
};

/** The weekdays a routine is on, in words. */
export function weekdaysText(weekdays: number): string {
  if (weekdays === 127) return "every day";
  if (weekdays === 31) return "weekdays";
  if (weekdays === 96) return "weekends";
  return WEEKDAYS.filter((_, index) => weekdays & (1 << index)).join(" ");
}

const length = (low: number, high: number) => (low === high ? `${low} min` : `${low}–${high} min`);

/** One routine in words: when, how long, which days, its state and HIMARI's say. */
export function routineLine(routine: Routine): string {
  if (routine.builtin === "sleep") {
    return `starts ${routine.start_from}–${routine.start_to} · ends ${routine.end_from}–${routine.end_to} · every night`;
  }
  if (routine.builtin === "diary") {
    return `from ${routine.start_from} until 40 min before the sleep · ${length(routine.minutes_min, routine.minutes_max)}`
      + " · every night";
  }
  const state = routine.state === "trial"
    ? (routine.trial_ended ? "trial ended" : `trial until ${routine.trial_until}`)
    : routine.state === "fixed" ? "fixed" : "off";
  return `starts ${routine.start_from}–${routine.start_to} · ${length(routine.minutes_min, routine.minutes_max)} · `
    + `${weekdaysText(routine.weekdays)} · ${state} · ${SAYS[routine.himari]}`;
}

/** Days from ``today`` to ``until``, both counted (at least 1). */
function daysTo(today: string, until: string | null, fallback: number): number {
  if (!until) return fallback;
  for (let days = 1; days <= 366; days += 1) if (addDays(today, days - 1) >= until) return days;
  return fallback;
}

function Editor({ routine, revision, data, onDone }: {
  routine: Routine | null; revision: number; data: Routines; onDone: () => void;
}) {
  const client = useQueryClient();
  const builtin = routine?.builtin ?? null;
  const { today, limits } = data;
  const start = {
    name: routine?.name ?? "", text: routine?.text ?? "", start_from: routine?.start_from ?? "18:30",
    start_to: routine?.start_to ?? "19:30", minutes_min: String(routine?.minutes_min ?? 40),
    minutes_max: String(routine?.minutes_max ?? 40), end_from: routine?.end_from ?? "", end_to: routine?.end_to ?? "",
    weekdays: routine?.weekdays ?? 127, state: routine?.state ?? ("trial" as RoutineState),
    days: String(daysTo(today, routine?.trial_until ?? null, limits.default_trial_days)),
    himari: routine?.himari ?? ("move" as RoutineSay),
  };
  const [form, setForm] = useState(start);
  const [saving, setSaving] = useState(false);
  const set = (change: Partial<typeof start>) => setForm((was) => ({ ...was, ...change }));
  const dirty = JSON.stringify(form) !== JSON.stringify(start);
  useUnsaved(dirty);
  const days = Number(form.days);
  const save = async () => {
    // a trial keeps its last day unless its days were changed; a new trial runs from today
    const until = form.state !== "trial" ? null
      : routine?.state === "trial" && form.days === start.days ? routine.trial_until : addDays(today, days - 1);
    const input: RoutineInput = {
      name: form.name.trim(), text: form.text.trim(), start_from: form.start_from,
      start_to: builtin === "diary" ? form.start_from : form.start_to, minutes_min: Number(form.minutes_min),
      minutes_max: Number(form.minutes_max), end_from: builtin === "sleep" ? form.end_from : null,
      end_to: builtin === "sleep" ? form.end_to : null, weekdays: form.weekdays, state: form.state,
      trial_until: until, himari: form.himari,
    };
    setSaving(true);
    try {
      if (routine) await editRoutine(routine.id, input, revision);
      else await addRoutine(input, data.persona?.id ?? null);
      toast("Saved. The schedule follows it from now on.");
      onDone();
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? "The routine was changed elsewhere since you opened it; nothing was saved. Cancel to see it as it is now."
        : refused(failure, "persona_changed") ? "Another persona is in use now; look again."
        : why(failure, "Could not save the routine."), "error");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: ROUTINES_KEY });
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  const time = (label: string, value: string, change: (value: string) => void) => (
    <label className="flex items-center gap-1 text-xs text-muted">
      {label}
      <input type="time" step={600} aria-label={label} value={value} readOnly={saving}
        onChange={(event) => change(event.target.value)} className={`${field} w-28`} />
    </label>
  );
  const minutes = (label: string, value: string, change: (value: string) => void) => (
    <label className="flex items-center gap-1 text-xs text-muted">
      {label}
      <input type="number" step={5} min={builtin === "diary" ? 10 : 5} max={builtin === "diary" ? 60 : 720}
        aria-label={label} value={value} readOnly={saving} onChange={(event) => change(event.target.value)}
        className={`${field} w-20`} />
    </label>
  );
  const own = builtin === null;
  const ready = !saving && dirty && (!own || (form.name.trim() !== "" && form.text.trim() !== "" && form.weekdays > 0
    && (form.state !== "trial" || (days >= 1 && days <= limits.trial_days))));
  return (
    <form className="space-y-2" aria-label="Routine editor" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      {own && (
        <>
          <input aria-label="Routine name" placeholder="Name (저녁 식사)" value={form.name} maxLength={limits.name_chars}
            autoFocus readOnly={saving} onChange={(event) => set({ name: event.target.value })} className={field} />
          <input aria-label="Routine line" placeholder="What HIMARI is doing then, in its words (저녁 먹는 중)"
            value={form.text} maxLength={limits.text_chars} readOnly={saving}
            onChange={(event) => set({ text: event.target.value })} className={field} />
        </>
      )}
      <div className="flex flex-wrap gap-3">
        {time(builtin === "diary" ? "Starts from" : "Earliest start", form.start_from, (v) => set({ start_from: v }))}
        {builtin !== "diary" && time("Latest start", form.start_to, (v) => set({ start_to: v }))}
        {builtin === "sleep" && time("Earliest end", form.end_from, (v) => set({ end_from: v }))}
        {builtin === "sleep" && time("Latest end", form.end_to, (v) => set({ end_to: v }))}
        {builtin !== "sleep" && minutes("Shortest (min)", form.minutes_min, (v) => set({ minutes_min: v }))}
        {builtin !== "sleep" && minutes("Longest (min)", form.minutes_max, (v) => set({ minutes_max: v }))}
      </div>
      {own && (
        <>
          <fieldset className="flex flex-wrap gap-2 text-xs text-muted" aria-label="Weekdays">
            {WEEKDAYS.map((name, index) => (
              <label key={name} className="flex items-center gap-1">
                <input type="checkbox" checked={(form.weekdays & (1 << index)) !== 0} disabled={saving}
                  onChange={() => set({ weekdays: form.weekdays ^ (1 << index) })} />
                {name}
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
            <label className="flex items-center gap-1">
              State
              <select aria-label="State" value={form.state} disabled={saving}
                onChange={(event) => set({ state: event.target.value as RoutineState })} className={`${field} w-28`}>
                {(Object.keys(STATES) as RoutineState[]).map((state) => (
                  <option key={state} value={state}>{STATES[state]}</option>
                ))}
              </select>
            </label>
            {form.state === "trial" && (
              <label className="flex items-center gap-1">
                for
                <input type="number" min={1} max={limits.trial_days} aria-label="Trial days" value={form.days}
                  readOnly={saving} onChange={(event) => set({ days: event.target.value })} className={`${field} w-16`} />
                days{days >= 1 ? ` (until ${addDays(today, days - 1)})` : ""}
              </label>
            )}
            <label className="flex items-center gap-1">
              HIMARI
              <select aria-label="What HIMARI may do" value={form.himari} disabled={saving}
                onChange={(event) => set({ himari: event.target.value as RoutineSay })} className={`${field} w-56`}>
                {(Object.keys(SAYS) as RoutineSay[]).map((say) => <option key={say} value={say}>{SAYS[say]}</option>)}
              </select>
            </label>
          </div>
        </>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={saving} onClick={onDone}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!ready}>Save</Button>
      </div>
    </form>
  );
}

export function RoutinesGroup() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ROUTINES_KEY, queryFn: readRoutines });
  const [open, setOpen] = useState<{ id: number | "new"; revision: number } | null>(null);
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading the routines…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the routines.</p>;
  const data = query.data;
  const locked = busy || open !== null;
  const remove = async (routine: Routine) => {
    if (!window.confirm(`Delete the routine "${routine.name}"? Its slots not started yet are cancelled.`)) return;
    setBusy(true);
    try {
      await deleteRoutine(routine.id, routine.revision);
      toast("Deleted the routine.");
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? "The routine was changed elsewhere since you opened it; nothing was deleted. See it as it is now."
        : why(failure, "Could not delete the routine."), "error");
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: ROUTINES_KEY });
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  const items = data.routines.map((routine) => (
    <li key={routine.id} className="rounded-lg border border-line px-3 py-2 text-sm" aria-label={`Routine ${routine.name}`}>
      {open?.id === routine.id ? (
        <Editor routine={routine} revision={open.revision} data={data} onDone={() => setOpen(null)} />
      ) : (
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="font-medium text-ink">
              {routine.name}
              {routine.builtin && <span className="ml-2 text-xs font-normal text-faint">built-in</span>}
            </p>
            {!routine.builtin && <p className="break-words text-muted">{routine.text}</p>}
            <p className="mt-1 text-xs text-faint">{routineLine(routine)}</p>
          </div>
          <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked}
            onClick={() => setOpen({ id: routine.id, revision: routine.revision })}>Edit</Button>
          {!routine.builtin && (
            <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked}
              onClick={() => void remove(routine)}>Delete</Button>
          )}
        </div>
      )}
    </li>
  ));
  const full = data.routines.length >= data.limits.routines;
  const top = open?.id === "new" ? (
    <div className="rounded-lg border border-line px-3 py-2">
      <Editor routine={null} revision={0} data={data} onDone={() => setOpen(null)} />
    </div>
  ) : (
    <div className="flex justify-end">
      <Button disabled={locked || full} onClick={() => setOpen({ id: "new", revision: 0 })}>Add a routine</Button>
    </div>
  );
  return (
    <Group title="Routines" top={top} empty="No routines."
      note="placed on the schedule each day they are on; try one out, then fix it or turn it off">
      {items}
    </Group>
  );
}
