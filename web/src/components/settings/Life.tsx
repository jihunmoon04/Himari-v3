// HIMARI's life (character-life §6): for the persona in use (or the default), the facts it has settled about itself,
// its arcs (character-world §4: threads over weeks the story writer starts and ends), its plans and its diary. The
// owner adds facts, edits any entry from the revision opened (an arc's title and goal; a save over a change made
// elsewhere is refused with 409 and says so) and deletes them (they retire: no undo). The app never shows them.
// character-work §8: the Work group shows each project (open first) with its "so far" and its pieces (date and time,
// title, summary, the full text when opened), which are edited and deleted like any entry; the status line says
// whether the worker runs and the next work plan, and a work plan names its project.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import {
  addLifeFact, deleteLife, editLife, readLife, type Life, type LifeEntry, type LifeKind, type LifeOrigin,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
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
};
const NAMES: Record<LifeKind, string> = {
  fact: "fact", plan: "plan", diary: "diary entry", arc: "arc", work: "work piece",
};
const ARC_CHARS = 200; // as the server's, when it does not say
const ARC_TITLE_CHARS = 60;
const WORK_CHARS = 3000;
const SUMMARY_CHARS = 200;
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
    : piece ? limits.work_chars ?? WORK_CHARS : limits.chars;
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
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        {(entry.kind === "arc" || piece) && <p className="font-medium text-ink">{entry.title}</p>}
        {piece ? (
          <>
            <p className="break-words">{entry.summary}</p>
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-muted">Show the piece</summary>
              <p className="mt-1 whitespace-pre-wrap break-words">{entry.text}</p>
            </details>
          </>
        ) : <p className="whitespace-pre-wrap break-words">{entry.text}</p>}
        {entry.kind === "arc" && entry.so_far && <p className="mt-1 text-muted">So far: {entry.so_far}</p>}
        {entry.kind === "arc" && entry.ending && <p className="mt-1 text-muted">Ending: {entry.ending}</p>}
        <p className="mt-1 text-xs text-faint">
          {entry.kind === "arc"
            ? `${entry.status === "closed" ? `${entry.day} – ${entry.end_day} · closed` : `since ${entry.day} · open`} · `
            : entry.day ? `${when(entry)} · ` : ""}
          {project ? `work on ${project} · ` : ""}
          {ORIGINS[entry.origin]}
        </p>
      </div>
      <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEdit}>Edit</Button>
      <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked} onClick={onDelete}>
        Delete
      </Button>
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
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read HIMARI's life.</p>;
  const { persona, enabled, writer, entries, limits, mood, now, world, work } = query.data;
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
  const locked = busy || open !== null; // while an entry is open, nothing else may change under its unsaved text
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
    <section className="space-y-4" aria-label="Life">
      <h2 className="text-base font-semibold">Life</h2>
      <p className="text-sm text-muted">
        <span className="font-medium text-ink">{persona ? `${persona.name}'s life` : "Default (no persona)"}</span>.
        HIMARI lives it apart from you: the facts it has settled about itself, the arcs it follows over weeks, its
        plans and its diary. Each persona has its own; this is the one in use. The app never shows it. {NEXT_TURN}
      </p>
      {!enabled && (
        <p className="text-sm text-wait">
          HIMARI's life is not used now (the companion or life is off on this server); it is kept as it is.
        </p>
      )}
      <p className="text-xs text-faint">Story writer: {writer.told}</p>
      {workLine && <p className="text-xs text-faint" aria-label="Work status">{workLine}</p>}
      {world && (
        <p className="text-xs text-faint" aria-label="World">
          Weather: {world.today ?? world.weather} · The day's topics: {world.topics}
        </p>
      )}
      {(mood || now) && (
        <p className="text-sm text-muted" aria-label="Now">
          {mood && <>Mood: <span className="text-ink">{mood.mood}</span>{mood.reason ? ` (${mood.reason})` : ""}
            {` · since ${new Date(mood.set_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}</>}
          {mood && now && " · "}
          {now && (now.doing ? `Doing: ${now.doing}${now.since ? ` (from ${now.since})` : ""}` : "Asleep")}
        </p>
      )}
      <Group title="Facts" note={`${facts.length} of ${limits.facts}`} empty="No facts yet."
        top={<AddFact personaId={persona?.id ?? null} full={full} max={limits.chars} />}>
        {facts}
      </Group>
      <Group title="Arcs" note="the story writer starts, carries on and ends them; at most 2 open"
        empty="No arcs yet.">{rows("arc")}</Group>
      <Group title="Work" note="what HIMARI makes for its projects, a piece at each work plan"
        empty="No projects yet.">{projectBlocks}</Group>
      <Group title="Plans" empty="No plans.">{rows("plan")}</Group>
      <Group title="Diary" empty="No diary yet.">{rows("diary")}</Group>
    </section>
  );
}
