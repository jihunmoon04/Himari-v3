// What the tabs of Settings → HIMARI share (character-routine §9.2): the life's query, the wording, an entry's row and
// editor (edited from the revision opened; a save over a change made elsewhere is refused with 409 and says so), a
// schedule slot's row and editor, and the delete that asks first (entries retire: no undo). An editor with changes
// not saved says so to the section (useUnsaved), which asks before a tab change would throw them away.
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import {
  addLifeFact, addPlan, deleteLife, editLife, editPlan, type Life, type LifeEntry, type LifeKind, type LifeOrigin,
  type PlanInput, type PlanKind, type PlanState, type SleepRecord,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import { dayLabel, minutesOf, onlyMoves, scheduleDays, sleepNote, spanOf } from "@/features/schedule";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

/** The life's query; the history pages (Records' "Show older") sit under it, so a change reads them again too. */
export const KEY = ["settings", "life"];
export const HISTORY_KEY = [...KEY, "history"];
export const field = "block w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-faint";
export const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);
export const refused = (failure: unknown, code: string) =>
  failure instanceof ApiError && failure.status === 409 && failure.code === code;
export const NEXT_TURN = "Used from each conversation's next turn.";

export const ORIGINS: Record<LifeOrigin, string> = {
  writer: "written by the story writer",
  conversation: "from a conversation",
  user: "yours",
  himari: "by HIMARI", // character-day §4.2: its schedule Tool, in a day session
};
export const NAMES: Record<LifeKind, string> = {
  fact: "fact", plan: "slot", diary: "diary entry", arc: "arc", work: "work piece", thought: "kept thought",
  event: "event",
};
export const KINDS: Record<PlanKind, string> = {
  sleep: "Sleep", diary: "Diary", work: "Work", together: "Together", own: "Own",
};
const STATES: Record<PlanState, string> = { cancelled: "cancelled", missed: "missed" };
const ABOUT: Record<string, string> = {
  user: "about you", self: "about itself", work: "about its work", intent: "something it wants to say or do",
};
export const clock = (at: string) =>
  new Date(at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const ARC_CHARS = 200; // as the server's, when it does not say
const ARC_TITLE_CHARS = 60;
const WORK_CHARS = 3000;
const SUMMARY_CHARS = 200;
const EVENT_CHARS = 200;
export const when = (entry: LifeEntry) => `${entry.day ?? ""}${entry.time ? ` ${entry.time}` : ""}`;

/** Where the editors say whether they hold changes not saved (keyed by each editor's own token). */
export const Unsaved = createContext<(token: symbol, dirty: boolean) => void>(() => {});

/** This editor holds changes not saved (``dirty``) until it closes. */
export function useUnsaved(dirty: boolean) {
  const report = useContext(Unsaved);
  const [token] = useState(() => Symbol("editor"));
  useEffect(() => {
    report(token, dirty);
    return () => report(token, false);
  }, [report, token, dirty]);
}

/** character-work §8: the projects' titles by id, and a work plan's project. */
export function projectsOf(entries: readonly LifeEntry[]) {
  const titles = new Map(entries.filter((entry) => entry.kind === "arc").map((entry) => [entry.id, entry.title]));
  const projectOf = (entry: LifeEntry) =>
    entry.kind === "plan" && entry.arc_id != null ? titles.get(entry.arc_id) ?? null : null;
  return { titles, projectOf };
}

function Editor({ entry, revision, limits, onDone }: {
  entry: LifeEntry; revision: number; limits: Life["limits"]; onDone: () => void;
}) {
  const client = useQueryClient();
  const [text, setText] = useState(entry.text);
  const arc = entry.kind === "arc";
  const piece = entry.kind === "work";
  const titled = arc || piece;
  const [title, setTitle] = useState(entry.title ?? "");
  const [summary, setSummary] = useState(entry.summary ?? "");
  const [saving, setSaving] = useState(false);
  const max = entry.kind === "diary" ? limits.diary_chars : arc ? limits.arc_chars ?? ARC_CHARS
    : piece ? limits.work_chars ?? WORK_CHARS : entry.kind === "event" ? limits.event_chars ?? EVENT_CHARS
    : limits.chars;
  const changed = text !== entry.text || (titled && title !== (entry.title ?? ""))
    || (piece && summary !== (entry.summary ?? ""));
  useUnsaved(changed);
  const save = async () => {
    setSaving(true);
    try {
      // from the revision it was opened with, whatever is read later
      if (piece) await editLife(entry.id, text.trim(), revision, title.trim(), summary.trim());
      else if (arc) await editLife(entry.id, text.trim(), revision, title.trim());
      else await editLife(entry.id, text.trim(), revision);
      toast(`Saved. ${NEXT_TURN}`);
      onDone();
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? `The ${NAMES[entry.kind]} was changed elsewhere since you opened it; nothing was saved. Your text is still `
          + "here; cancel to see it as it is now."
        : why(failure, `Could not save the ${NAMES[entry.kind]}.`), "error");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  return (
    <form className="space-y-2" aria-label="Life entry editor"
      onSubmit={(event) => { event.preventDefault(); void save(); }}>
      {titled && (
        <input aria-label={arc ? "Arc title" : "Piece title"} value={title}
          maxLength={limits.arc_title_chars ?? ARC_TITLE_CHARS} autoFocus readOnly={saving}
          onChange={(event) => setTitle(event.target.value)} className={field} />
      )}
      {piece && (
        <input aria-label="Piece summary" value={summary} maxLength={limits.summary_chars ?? SUMMARY_CHARS}
          readOnly={saving} onChange={(event) => setSummary(event.target.value)} className={field} />
      )}
      <textarea aria-label={arc ? "Arc goal" : piece ? "Piece text" : "Entry text"} value={text}
        rows={entry.kind === "diary" ? 6 : piece ? 12 : 2} maxLength={max} autoFocus={!titled} readOnly={saving}
        onChange={(event) => setText(event.target.value)} className={cn(field, "resize-y")} />
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{text.length.toLocaleString()} / {max.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={saving} onClick={onDone}>Cancel</Button>
          <Button type="submit" variant="primary"
            disabled={saving || !text.trim() || (titled && !title.trim()) || (piece && !summary.trim()) || !changed}>
            Save
          </Button>
        </div>
      </div>
    </form>
  );
}

function Row({ entry, locked, onEdit, onDelete, project }: {
  entry: LifeEntry; locked: boolean; onEdit: () => void; onDelete: () => void; project?: string | null;
}) {
  const piece = entry.kind === "work";
  const thought = entry.kind === "thought";
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        {(entry.kind === "arc" || piece) && (
          <p className="font-medium text-ink">
            {entry.title}
            {piece && entry.draft && <span className="ml-2 text-xs font-normal text-wait">draft</span>}
          </p>
        )}
        {piece ? (
          <>
            <p className="break-words">{entry.summary}</p>
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-muted">
                {entry.draft ? "Show the draft" : "Show the piece"}
              </summary>
              <p className="mt-1 whitespace-pre-wrap break-words">{entry.text}</p>
            </details>
          </>
        ) : <p className="whitespace-pre-wrap break-words">{entry.text}</p>}
        {entry.kind === "arc" && entry.so_far && <p className="mt-1 text-muted">So far: {entry.so_far}</p>}
        {entry.kind === "arc" && entry.ending && <p className="mt-1 text-muted">Ending: {entry.ending}</p>}
        <p className="mt-1 text-xs text-faint">
          {thought ? `${entry.about ? `${ABOUT[entry.about] ?? entry.about} · ` : ""}kept ${clock(entry.created_at)}`
            + `${entry.expires_at ? ` · until ${clock(entry.expires_at)}` : ""} · ` : ""}
          {entry.kind === "arc"
            ? `${entry.status === "closed" ? `${entry.day} – ${entry.end_day} · closed` : `since ${entry.day} · open`} · `
            : entry.day ? `${when(entry)} · ` : ""}
          {project ? `work on ${project} · ` : ""}
          {ORIGINS[entry.origin]}
        </p>
      </div>
      {!thought && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEdit}>Edit</Button>
      )}
      <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked} onClick={onDelete}>
        Delete
      </Button>
    </div>
  );
}

/** The entry open in an editor (one at a time on a tab) and the delete that asks first. */
export interface Editing {
  open: { id: number; revision: number } | null;
  setOpen: (open: { id: number; revision: number } | null) => void;
  busy: boolean;
  remove: (entry: LifeEntry) => Promise<void>;
}

export function useEditing(): Editing {
  const client = useQueryClient();
  const [open, setOpen] = useState<{ id: number; revision: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const remove = async (entry: LifeEntry) => {
    const what = NAMES[entry.kind];
    if (!window.confirm(`Delete this ${what}? HIMARI will not know it any more; there is no undo.`)) return;
    setBusy(true);
    try {
      await deleteLife(entry.id, entry.revision);
      toast(`Deleted the ${what}. ${NEXT_TURN}`);
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? `The ${what} was changed elsewhere since you opened it; nothing was deleted. See it as it is now.`
        : why(failure, `Could not delete the ${what}.`), "error");
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  return { open, setOpen, busy, remove };
}

/** An entry as a list item: its row, or its editor while open. */
export function EntryItem({ entry, editing, locked, limits, project }: {
  entry: LifeEntry; editing: Editing; locked: boolean; limits: Life["limits"]; project?: string | null;
}) {
  return (
    <li className="rounded-lg border border-line px-3 py-2 text-sm">
      {editing.open?.id === entry.id ? (
        <Editor entry={entry} revision={editing.open.revision} limits={limits} onDone={() => editing.setOpen(null)} />
      ) : (
        <Row entry={entry} locked={locked} project={project}
          onEdit={() => editing.setOpen({ id: entry.id, revision: entry.revision })}
          onDelete={() => void editing.remove(entry)} />
      )}
    </li>
  );
}

/** character-day §4.1: a plan's kind; one from before the schedule is work with a project, its own otherwise. */
export const kindOf = (entry: LifeEntry): PlanKind => entry.plan_kind ?? (entry.arc_id != null ? "work" : "own");

/** A schedule slot written (``entry`` null) or edited from the revision opened. The sleep and diary slots only move
 *  (their day, start and length; a diary slot 10..60 minutes), are never offered new and keep their kind; a work slot
 *  keeps its project. Only HIMARI makes a together slot (review 14: the server's rule), so it is never offered new,
 *  nor as what an own slot becomes; a together slot may stay so or turn into its own. */
export function PlanEditor({ entry, revision, personaId, projects, today, max, onDone }: {
  entry: LifeEntry | null; revision: number; personaId: number | null; projects: { id: number; title: string }[];
  today: string; max: number; onDone: () => void;
}) {
  const client = useQueryClient();
  const was = entry ? kindOf(entry) : null;
  const [day, setDay] = useState(entry?.day ?? today);
  const [time, setTime] = useState(entry?.time ?? "");
  const [minutes, setMinutes] = useState(String(entry?.minutes ?? 60));
  const [kind, setKind] = useState<PlanKind>(was ?? "own");
  const [project, setProject] = useState<number | null>(entry?.arc_id ?? projects[0]?.id ?? null);
  const [text, setText] = useState(entry?.text ?? "");
  const [saving, setSaving] = useState(false);
  const moves = onlyMoves(kind);
  const range = minutesOf(kind);
  const kinds: PlanKind[] = was !== null && (onlyMoves(was) || was === "work" || was === "own") ? [was]
    : was === "together" ? ["together", "own"] : ["own", ...(projects.length ? ["work" as const] : [])];
  const days = scheduleDays(today);
  if (entry?.day && !days.includes(entry.day)) days.unshift(entry.day);
  const length = Number(minutes);
  useUnsaved(day !== (entry?.day ?? today) || time !== (entry?.time ?? "") || minutes !== String(entry?.minutes ?? 60)
    || kind !== (was ?? "own") || text !== (entry?.text ?? ""));
  const valid = /^\d{2}:\d{2}$/.test(time) && Number.isInteger(length) && length >= range.min && length <= range.max
    && (moves || text.trim() !== "") && (kind !== "work" || project !== null);
  const save = async () => {
    setSaving(true);
    const plan: PlanInput = { day, time, minutes: length, plan_kind: kind, text: moves ? entry?.text ?? "" : text.trim(),
      ...(kind === "work" ? { arc_id: project } : {}) };
    try {
      if (entry) await editPlan(entry.id, plan, revision);
      else await addPlan(plan, personaId);
      toast(`${entry ? "Saved" : "Added"}. ${NEXT_TURN}`);
      onDone();
    } catch (failure) {
      toast(refused(failure, "plan_refused") ? `The schedule did not take it: ${why(failure, "past its limits")}`
        : refused(failure, "revision_conflict")
          ? "The slot was changed elsewhere since you opened it; nothing was saved. Cancel to see it as it is now."
          : refused(failure, "persona_changed")
            ? "The persona in use changed since this page was read; nothing was added."
            : why(failure, entry ? "Could not save the slot." : "Could not add the slot."), "error");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  const small = "rounded-lg border border-line bg-bg p-1.5 text-sm outline-none focus:border-faint";
  return (
    <form className="space-y-2" aria-label={entry ? "Slot editor" : "Add a slot"}
      onSubmit={(event) => { event.preventDefault(); if (valid) void save(); }}>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Slot day" value={day} disabled={saving} onChange={(event) => setDay(event.target.value)}
          className={small}>
          {days.map((option) => <option key={option} value={option}>{dayLabel(option, today)}</option>)}
        </select>
        <input aria-label="Slot start" type="time" value={time} readOnly={saving}
          onChange={(event) => setTime(event.target.value)} className={small} />
        <label className="flex items-center gap-1 text-xs text-muted">
          <input aria-label="Slot minutes" type="number" min={range.min} max={range.max} step={5} value={minutes}
            readOnly={saving} onChange={(event) => setMinutes(event.target.value)} className={cn(small, "w-20")} />
          minutes
        </label>
        <select aria-label="Slot kind" value={kind} disabled={saving || kinds.length < 2}
          onChange={(event) => setKind(event.target.value as PlanKind)} className={small}>
          {kinds.map((option) => <option key={option} value={option}>{KINDS[option]}</option>)}
        </select>
        {kind === "work" && !entry && (
          <select aria-label="Slot project" value={project ?? ""} disabled={saving}
            onChange={(event) => setProject(Number(event.target.value))} className={small}>
            {projects.map((option) => <option key={option.id} value={option.id}>{option.title}</option>)}
          </select>
        )}
      </div>
      {!moves && (
        <textarea aria-label="Slot text" value={text} rows={2} maxLength={max} readOnly={saving}
          placeholder="What HIMARI does then, in a line." onChange={(event) => setText(event.target.value)}
          className={cn(field, "resize-y")} />
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={saving} onClick={onDone}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={saving || !valid}>{entry ? "Save" : "Add slot"}</Button>
      </div>
    </form>
  );
}

/** A slot of the schedule: its kind, start and length, what it is, and a cancelled or missed one with why. Without
 *  ``onEdit`` it is only read (Today's and Records' slots). */
export function SlotRow({ entry, locked = false, project, onEdit, onDelete }: {
  entry: LifeEntry; locked?: boolean; project: string | null; onEdit?: () => void; onDelete?: () => void;
}) {
  const kind = kindOf(entry);
  const gone = entry.plan_state != null; // cancelled or missed: kept, never edited
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2">
          <span className="rounded border border-line px-1 text-xs text-muted">{KINDS[kind]}</span>
          <span className={cn("text-xs text-muted", gone && "line-through")}>{spanOf(entry)}</span>
          {entry.plan_state && <span className="text-xs text-wait">{STATES[entry.plan_state]}</span>}
        </p>
        {entry.text && <p className={cn("break-words", gone && "text-faint line-through")}>{entry.text}</p>}
        {entry.reason && <p className="mt-1 text-muted">Why: {entry.reason}</p>}
        <p className="mt-1 text-xs text-faint">{project ? `work on ${project} · ` : ""}{ORIGINS[entry.origin]}</p>
      </div>
      {onEdit && !gone && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEdit}>
          {onlyMoves(kind) ? "Move" : "Edit"}
        </Button>
      )}
      {onDelete && !onlyMoves(kind) && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked} onClick={onDelete}>
          Delete
        </Button>
      )}
    </div>
  );
}

/** One night of the record: when it fell asleep and woke, and how. */
export function SleepItem({ sleep }: { sleep: SleepRecord }) {
  return (
    <li className="rounded-lg border border-line px-3 py-2 text-sm">
      <p>{clock(sleep.fell_at)} → {sleep.woke_at ? clock(sleep.woke_at) : "…"}</p>
      <p className="mt-1 text-xs text-faint">{sleepNote(sleep)}</p>
    </li>
  );
}

export function AddFact({ personaId, full, max }: { personaId: number | null; full: boolean; max: number }) {
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useUnsaved(text.trim() !== "");
  const submit = async () => {
    setBusy(true);
    try {
      await addLifeFact(text.trim(), personaId);
      toast(`Fact added. ${NEXT_TURN}`);
      setText("");
    } catch (failure) {
      toast(refused(failure, "persona_changed")
        ? "The persona in use changed since this page was read; nothing was added. The page now shows the life of the "
          + "one in use: add it there if you still want it."
        : why(failure, "Could not add the fact."), "error"); // a failed add keeps what was written
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  return (
    <form className="space-y-2" aria-label="Add a fact" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <textarea aria-label="New fact" value={text} rows={2} maxLength={max} readOnly={busy}
        placeholder="Something settled about HIMARI's own life, in a sentence or two."
        onChange={(event) => setText(event.target.value)} className={cn(field, "resize-y")} />
      <div className="flex items-center justify-end gap-3">
        {full && <span className="text-xs text-faint">Full of your own facts; delete one first.</span>}
        <Button type="submit" variant="primary" disabled={busy || full || !text.trim()}>Add fact</Button>
      </div>
    </form>
  );
}

export function Group({ title, note, top, empty, children }: {
  title: string; note?: string; top?: ReactNode; empty: string; children: ReactNode[];
}) {
  return (
    <section className="space-y-2" aria-label={title}>
      <h3 className="text-sm font-semibold">
        {title}{note && <span className="ml-2 text-xs font-normal text-faint">{note}</span>}
      </h3>
      {top}
      {children.length ? <ul className="space-y-2">{children}</ul> : <p className="text-sm text-faint">{empty}</p>}
    </section>
  );
}

/** character-work §8: whether the worker runs, and the next work plan with its project. */
export function workLine(work: Life["work"], projectOf: (entry: LifeEntry) => string | null): string | null {
  if (!work) return null;
  const next = work.next;
  return work.running
    ? `Work: on${next ? ` · next: ${when(next)} ${next.text}${projectOf(next) ? ` (${projectOf(next)})` : ""}`
      : " · no work plan ahead"}`
    : "Work: off (it runs with the story writer)";
}
