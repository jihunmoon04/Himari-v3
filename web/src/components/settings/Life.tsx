// HIMARI's life (character-life §6): for the persona in use (or the default), the facts it has settled about itself,
// its plans and its diary. The owner adds facts, edits any entry from the revision opened (a save over a change made
// elsewhere is refused with 409 and says so) and deletes them (they retire: no undo). The app never shows them.
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
const NAMES: Record<LifeKind, string> = { fact: "fact", plan: "plan", diary: "diary entry" };

function Editor({ entry, revision, limits, onDone }: {
  entry: LifeEntry; revision: number; limits: Life["limits"]; onDone: () => void;
}) {
  const client = useQueryClient();
  const [text, setText] = useState(entry.text);
  const [saving, setSaving] = useState(false);
  const max = entry.kind === "diary" ? limits.diary_chars : limits.chars;
  const save = async () => {
    setSaving(true);
    try {
      await editLife(entry.id, text.trim(), revision); // from the revision it was opened with, whatever is read later
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
      <textarea aria-label="Entry text" value={text} rows={entry.kind === "diary" ? 6 : 2} maxLength={max} autoFocus
        readOnly={saving} onChange={(event) => setText(event.target.value)} className={cn(field, "resize-y")} />
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{text.length.toLocaleString()} / {max.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={saving} onClick={onDone}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={saving || !text.trim() || text === entry.text}>Save</Button>
        </div>
      </div>
    </form>
  );
}

function Row({ entry, locked, onEdit, onDelete }: {
  entry: LifeEntry; locked: boolean; onEdit: () => void; onDelete: () => void;
}) {
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <p className="whitespace-pre-wrap break-words">{entry.text}</p>
        <p className="mt-1 text-xs text-faint">{entry.day ? `${entry.day} · ` : ""}{ORIGINS[entry.origin]}</p>
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
  const { persona, enabled, writer, entries, limits } = query.data;
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
  const rows = (kind: LifeKind) => entries.filter((entry) => entry.kind === kind).map((entry) => (
    <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
      {open?.id === entry.id ? (
        <Editor entry={entry} revision={open.revision} limits={limits} onDone={() => setOpen(null)} />
      ) : (
        <Row entry={entry} locked={locked} onEdit={() => setOpen({ id: entry.id, revision: entry.revision })}
          onDelete={() => void remove(entry)} />
      )}
    </li>
  ));
  const facts = rows("fact");
  // §2: at the limit a new fact retires the oldest one not yours; only when all are yours is there no room
  const kept = entries.filter((entry) => entry.kind === "fact");
  const full = kept.length >= limits.facts && kept.every((entry) => entry.origin === "user");
  return (
    <section className="space-y-4" aria-label="Life">
      <h2 className="text-base font-semibold">Life</h2>
      <p className="text-sm text-muted">
        <span className="font-medium text-ink">{persona ? `${persona.name}'s life` : "Default (no persona)"}</span>.
        HIMARI lives it apart from you: the facts it has settled about itself, its plans and its diary. Each persona
        has its own; this is the one in use. The app never shows it. {NEXT_TURN}
      </p>
      {!enabled && (
        <p className="text-sm text-wait">
          HIMARI's life is not used now (the companion or life is off on this server); it is kept as it is.
        </p>
      )}
      <p className="text-xs text-faint">Story writer: {writer.told}</p>
      <Group title="Facts" note={`${facts.length} of ${limits.facts}`} empty="No facts yet."
        top={<AddFact personaId={persona?.id ?? null} full={full} max={limits.chars} />}>
        {facts}
      </Group>
      <Group title="Plans" empty="No plans.">{rows("plan")}</Group>
      <Group title="Diary" empty="No diary yet.">{rows("diary")}</Group>
    </section>
  );
}
