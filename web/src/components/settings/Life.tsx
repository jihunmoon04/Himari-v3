// HIMARI's life (character-life §6): for the persona in use (or the default), the facts it has settled about itself,
// its arcs (character-world §4: threads over weeks the story writer starts and ends), its plans and its diary. The
// owner adds facts, edits any entry from the revision opened (an arc's title and goal; a save over a change made
// elsewhere is refused with 409 and says so) and deletes them (they retire: no undo). The app never shows them.
// character-work §8: the Work group shows each project (open first) with its "so far" and its pieces (date and time,
// title, summary, the full text when opened), which are edited and deleted like any entry; the status line says
// whether the worker runs and the next work plan, and a work plan names its project.
// character-mind §8: the Thoughts group lists the kept thoughts (what HIMARI thought and left unsaid: the text, what it
// is about, when it was kept and when it stops being told); they are only deleted, never edited. The Events group lists
// what the world made happen to HIMARI (date and time, text), edited and deleted like any entry; a line says when the
// world next runs.
// character-day §12, §18.12: the plans are HIMARI's Schedule — its slots from today on, by day, each with its kind,
// start and length, and a cancelled or missed one with why; the owner adds, edits and deletes slots, and the sleep
// slot only moves (it is never deleted). The Sleep log below lists its last 14 sleeps, and the Now line its sleep.
// character-diary §10: the diary slot (the night's diary time, placed by the server) only moves, as the sleep slot.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import {
  addLifeFact, addPlan, deleteLife, editLife, editPlan, readLife, type Life, type LifeEntry, type LifeKind,
  type LifeOrigin, type PlanInput, type PlanKind, type PlanState,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import {
  dayLabel, localDay, minutesOf, onlyMoves, scheduleDays, sleepNote, sleepText, slotsByDay, spanOf,
} from "@/features/schedule";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const KEY = ["settings", "life"];
const field = "block w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);
const refused = (failure: unknown, code: string) =>
  failure instanceof ApiError && failure.status === 409 && failure.code === code;
const NEXT_TURN = "Used from each conversation's next turn.";

const ORIGINS: Record<LifeOrigin, string> = {
  writer: "written by the story writer",
  conversation: "from a conversation",
  user: "yours",
  himari: "by HIMARI", // character-day §4.2: its schedule Tool, in a day session
};
const NAMES: Record<LifeKind, string> = {
  fact: "fact", plan: "slot", diary: "diary entry", arc: "arc", work: "work piece", thought: "kept thought",
  event: "event",
};
const KINDS: Record<PlanKind, string> = {
  sleep: "Sleep", diary: "Diary", work: "Work", together: "Together", own: "Own",
};
const STATES: Record<PlanState, string> = { cancelled: "cancelled", missed: "missed" };
const ABOUT: Record<string, string> = {
  user: "about you", self: "about itself", work: "about its work", intent: "something it wants to say or do",
};
const clock = (at: string) =>
  new Date(at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const ARC_CHARS = 200; // as the server's, when it does not say
const ARC_TITLE_CHARS = 60;
const WORK_CHARS = 3000;
const SUMMARY_CHARS = 200;
const EVENT_CHARS = 200;
const when = (entry: LifeEntry) => `${entry.day ?? ""}${entry.time ? ` ${entry.time}` : ""}`;

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

/** character-day §4.1: a plan's kind; one from before the schedule is work with a project, its own otherwise. */
const kindOf = (entry: LifeEntry): PlanKind => entry.plan_kind ?? (entry.arc_id != null ? "work" : "own");

/** A schedule slot written (``entry`` null) or edited from the revision opened. The sleep and diary slots only move
 *  (their day, start and length; a diary slot 10..60 minutes), are never offered new and keep their kind; a work slot
 *  keeps its project. Only HIMARI makes a together slot (review 14: the server's rule), so it is never offered new,
 *  nor as what an own slot becomes; a together slot may stay so or turn into its own. */
function PlanEditor({ entry, revision, personaId, projects, today, max, onDone }: {
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

/** A slot of the schedule: its kind, start and length, what it is, and a cancelled or missed one with why. */
function SlotRow({ entry, locked, project, onEdit, onDelete }: {
  entry: LifeEntry; locked: boolean; project: string | null; onEdit: () => void; onDelete: () => void;
}) {
  const kind = kindOf(entry);
  const gone = entry.plan_state != null; // cancelled or missed: kept seven days, never edited
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
      {!gone && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEdit}>
          {onlyMoves(kind) ? "Move" : "Edit"}
        </Button>
      )}
      {!onlyMoves(kind) && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked} onClick={onDelete}>
          Delete
        </Button>
      )}
    </div>
  );
}

function AddFact({ personaId, full, max }: { personaId: number | null; full: boolean; max: number }) {
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
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

function Group({ title, note, top, empty, children }: {
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

export function LifeSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: KEY, queryFn: readLife });
  const [open, setOpen] = useState<{ id: number; revision: number } | null>(null); // the entry being edited
  const [adding, setAdding] = useState(false); // a schedule slot being written
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read HIMARI's life.</p>;
  const { persona, enabled, writer, entries, limits, mood, now, world, work, mind, sleep, sleeps } = query.data;
  const today = localDay(new Date());
  // character-work §8: the projects by id, for a work plan's project and the Work group
  const titles = new Map(entries.filter((entry) => entry.kind === "arc").map((entry) => [entry.id, entry.title]));
  const projectOf = (entry: LifeEntry) =>
    entry.kind === "plan" && entry.arc_id != null ? titles.get(entry.arc_id) ?? null : null;
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
  // while an entry is open (or a slot being added), nothing else may change under its unsaved text
  const locked = busy || open !== null || adding;
  const item = (entry: LifeEntry) => (
    <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
      {open?.id === entry.id ? (
        <Editor entry={entry} revision={open.revision} limits={limits} onDone={() => setOpen(null)} />
      ) : (
        <Row entry={entry} locked={locked} onEdit={() => setOpen({ id: entry.id, revision: entry.revision })}
          onDelete={() => void remove(entry)} project={projectOf(entry)} />
      )}
    </li>
  );
  const rows = (kind: LifeKind) => entries.filter((entry) => entry.kind === kind).map(item);
  // character-day §12: the schedule by day, from today on (and the cancelled or missed slots kept a few days)
  const openProjects = entries.filter((entry) => entry.kind === "arc" && entry.status === "open")
    .map((entry) => ({ id: entry.id, title: entry.title ?? `Project ${entry.id}` }));
  const slot = (entry: LifeEntry) => (
    <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
      {open?.id === entry.id ? (
        <PlanEditor entry={entry} revision={open.revision} personaId={persona?.id ?? null} projects={openProjects}
          today={today} max={limits.chars} onDone={() => setOpen(null)} />
      ) : (
        <SlotRow entry={entry} locked={locked} project={projectOf(entry)}
          onEdit={() => setOpen({ id: entry.id, revision: entry.revision })} onDelete={() => void remove(entry)} />
      )}
    </li>
  );
  const schedule = slotsByDay(entries).map(({ day, slots }) => (
    <li key={day} className="space-y-2" aria-label={`Day ${day}`}>
      <p className="text-xs font-medium text-muted">{dayLabel(day, today)}</p>
      <ul className="space-y-2">{slots.map(slot)}</ul>
    </li>
  ));
  const addSlot = adding ? (
    <div className="rounded-lg border border-line px-3 py-2">
      <PlanEditor entry={null} revision={0} personaId={persona?.id ?? null} projects={openProjects} today={today}
        max={limits.chars} onDone={() => setAdding(false)} />
    </div>
  ) : (
    <div className="flex justify-end">
      <Button disabled={locked} onClick={() => setAdding(true)}>Add a slot</Button>
    </div>
  );
  // HIMARI's mood, what it does, and (character-day §18.12) its sleep, which tells asleep from its other states
  const nowParts: ReactNode[] = [];
  if (mood) {
    nowParts.push(<>Mood: <span className="text-ink">{mood.mood}</span>{mood.reason ? ` (${mood.reason})` : ""}
      {` · since ${new Date(mood.set_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}</>);
  }
  if (now?.doing) nowParts.push(`Doing: ${now.doing}${now.since ? ` (from ${now.since})` : ""}`);
  else if (now?.asleep && !sleep) nowParts.push("Asleep");
  if (sleep) nowParts.push(`Sleep: ${sleepText(sleep)}`);
  const sleepLog = (sleeps ?? []).map((night) => (
    <li key={night.fell_at} className="rounded-lg border border-line px-3 py-2 text-sm">
      <p>{clock(night.fell_at)} → {night.woke_at ? clock(night.woke_at) : "…"}</p>
      <p className="mt-1 text-xs text-faint">{sleepNote(night)}</p>
    </li>
  ));
  // the projects (open first, as listed) with their pieces, newest first; a piece of a project not listed goes last
  const pieces = entries.filter((entry) => entry.kind === "work");
  const projects = entries.filter((entry) => entry.kind === "arc"
    && (entry.status === "open" || pieces.some((made) => made.arc_id === entry.id)));
  const loose = pieces.filter((made) => made.arc_id == null || !titles.has(made.arc_id));
  const projectBlocks = [
    ...projects.map((project) => {
      const own = pieces.filter((made) => made.arc_id === project.id);
      return (
        <li key={`p${project.id}`} className="space-y-2" aria-label={`Project ${project.title ?? ""}`}>
          <div className="text-sm">
            <p className="font-medium text-ink">
              {project.title}<span className="ml-2 text-xs font-normal text-faint">{project.status}</span>
            </p>
            <p className="text-muted">{project.so_far ? `So far: ${project.so_far}` : "Nothing made yet."}</p>
          </div>
          {own.length > 0 && <ul className="space-y-2">{own.map(item)}</ul>}
        </li>
      );
    }),
    ...(loose.length ? [<li key="loose"><ul className="space-y-2">{loose.map(item)}</ul></li>] : []),
  ];
  const next = work?.next;
  const workLine = !work ? null : work.running
    ? `Work: on${next ? ` · next: ${when(next)} ${next.text}${projectOf(next) ? ` (${projectOf(next)})` : ""}`
      : " · no work plan ahead"}`
    : "Work: off (it runs with the story writer)";
  const facts = rows("fact");
  // §2: at the limit a new fact retires the oldest one not yours; only when all are yours is there no room
  const kept = entries.filter((entry) => entry.kind === "fact");
  const full = kept.length >= limits.facts && kept.every((entry) => entry.origin === "user");
  return (
    <section className="space-y-4" aria-label="HIMARI">
      <h2 className="text-base font-semibold">HIMARI</h2>
      <p className="text-sm text-muted">
        <span className="font-medium text-ink">{persona ? `${persona.name}'s life` : "Default (no persona)"}</span>.
        HIMARI lives it apart from you: the facts it has settled about itself, the arcs it follows over weeks, its
        schedule and sleep, and its diary. Each persona has its own; this is the one in use. The app never shows it.
        {` ${NEXT_TURN}`}
      </p>
      {!enabled && (
        <p className="text-sm text-wait">
          HIMARI's life is not used now (the companion or life is off on this server); it is kept as it is.
        </p>
      )}
      <p className="text-xs text-faint">Life runs: {writer.told}</p>
      {mind && <p className="text-xs text-faint" aria-label="Mind">Mind: {mind.model} ({mind.reason})</p>}
      {workLine && <p className="text-xs text-faint" aria-label="Work status">{workLine}</p>}
      {world && (
        <p className="text-xs text-faint" aria-label="World">
          Weather: {world.today ?? world.weather} · The day's topics: {world.topics}
          {world.next ? ` · World: next ${clock(world.next)}` : ""}
        </p>
      )}
      {nowParts.length > 0 && (
        <p className="text-sm text-muted" aria-label="Now">
          {nowParts.map((part, index) => <Fragment key={index}>{index > 0 && " · "}{part}</Fragment>)}
        </p>
      )}
      <Group title="Work" note="what HIMARI makes for its projects, a piece at each work plan"
        empty="No projects yet.">{projectBlocks}</Group>
      <Group title="Events" note="what the world made happen to HIMARI, at most 2 a day"
        empty="Nothing happened lately.">{rows("event")}</Group>
      <Group title="Arcs" note="HIMARI starts, carries on and ends them in its day's review; at most 2 open"
        empty="No arcs yet.">{rows("arc")}</Group>
      <Group title="Schedule" note="HIMARI's slots for the next seven days; the sleep and diary slots only move"
        top={addSlot} empty="Nothing on the schedule.">{schedule}</Group>
      <Group title="Sleep log" note="its last 14 sleeps" empty="No sleep logged yet.">{sleepLog}</Group>
      <Group title="Facts" note={`${facts.length} of ${limits.facts}`} empty="No facts yet."
        top={<AddFact personaId={persona?.id ?? null} full={full} max={limits.chars} />}>
        {facts}
      </Group>
      <Group title="Diary" empty="No diary yet.">{rows("diary")}</Group>
      <Group title="Thoughts" note="what HIMARI thought and left unsaid, kept a few days from conversations"
        empty="No kept thoughts.">{rows("thought")}</Group>
    </section>
  );
}
