// What HIMARI remembers (V3-P71, V3-P85): its notes (add, edit, delete; each from the revision shown), the notes it
// suggests and waits on the user for (keep, discard), and its timeline of conversations (edit, delete).
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "@/api/client";
import {
  addNote, CATEGORIES, decideProposal, deleteEpisode, deleteNote, editEpisode, editNote, readEpisodes, readMemories,
  readProposals, type Category, type Episode, type Note, type NoteInput,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const field = "rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);

function NoteForm({ start, onSave, onCancel, label }: {
  start?: Note;
  onSave: (note: NoteInput, revision: number | null) => Promise<boolean>; // true: saved
  onCancel?: () => void;
  label: string;
}) {
  const [revision] = useState(start?.revision ?? null); // the version it was opened from, whatever is read later
  const [category, setCategory] = useState<Category>(start?.category ?? "preference");
  const [text, setText] = useState(start?.text ?? "");
  const [due, setDue] = useState(start?.due ?? "");
  const [valid, setValid] = useState(start?.valid_until ?? "");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const saved = await onSave({ category, text: text.trim(), due: due.trim() || null, valid_until: valid.trim() || null },
        revision);
      if (saved && !start) { setText(""); setDue(""); setValid(""); } // a failed save keeps what was written
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="space-y-2" aria-label={label} onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div className="flex flex-wrap gap-2">
        <select aria-label="Category" value={category} onChange={(event) => setCategory(event.target.value as Category)} className={field}>
          {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        {category === "loop" && (
          <input aria-label="Due" placeholder="Due (YYYY-MM-DD)" value={due} onChange={(event) => setDue(event.target.value)} className={cn(field, "w-40")} />
        )}
        <input aria-label="Valid until" placeholder="Valid until (YYYY-MM-DD)" value={valid}
          onChange={(event) => setValid(event.target.value)} className={cn(field, "w-48")} />
      </div>
      <textarea aria-label="Note" value={text} rows={2} onChange={(event) => setText(event.target.value)}
        placeholder="One or two sentences about you." className={cn(field, "block w-full resize-y")} />
      <div className="flex justify-end gap-2">
        {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
        <Button type="submit" variant="primary" disabled={busy || !text.trim()}>{start ? "Save" : "Add note"}</Button>
      </div>
    </form>
  );
}

function Notes() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "memories"], queryFn: readMemories });
  const [editing, setEditing] = useState<number | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["settings", "memories"] });
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the notes.</p>;
  const { memories, enabled, auto, max_count } = query.data;
  const save = async (run: () => Promise<unknown>, done: string): Promise<boolean> => {
    try {
      await run();
      toast(done);
      setEditing(null);
      return true;
    } catch (failure) {
      toast(failure instanceof ApiError && failure.status === 409
        ? "The note was changed elsewhere since you opened it; nothing was saved. Cancel to see it as it is now."
        : why(failure, "Could not save the note."), "error");
      return false;
    } finally {
      void refresh();
    }
  };
  return (
    <div className="space-y-4">
      {!enabled && <p className="text-sm text-wait">Memory is off on this server: notes are kept but not used.</p>}
      <p className="text-xs text-faint">
        {memories.length} of {max_count} notes · automatic memory {auto.running ? "on" : "off"}
        {auto.model ? ` (${auto.model})` : ""}{auto.last_applied_at ? ` · last ${new Date(auto.last_applied_at).toLocaleString()}` : ""}
      </p>
      <NoteForm label="New note" onSave={(note) => save(() => addNote(note), "Note added.")} />
      <ul className="space-y-2" aria-label="Notes">
        {memories.map((note) => (
          <li key={note.id} className="rounded-lg border border-line px-3 py-2 text-sm">
            {editing === note.id ? (
              <NoteForm label="Edit note" start={note} onCancel={() => setEditing(null)}
                onSave={(input, opened) => save(() => editNote(note.id, opened ?? note.revision, input), "Note saved.")} />
            ) : (
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <p className="whitespace-pre-wrap break-words">{note.text}</p>
                  <p className="mt-1 text-xs text-faint">
                    {note.category}
                    {note.due ? ` · due ${note.due}${note.overdue ? " (overdue)" : ""}` : ""}
                    {note.valid_until ? ` · until ${note.valid_until}${note.expired ? " (passed)" : ""}` : ""}
                    {note.source && !note.source.in_trash && (
                      <> · from <Link to={`/c/${encodeURIComponent(note.source.conversation)}`} className="underline">
                        {note.source.title || note.source.preview || "a conversation"}</Link></>
                    )}
                  </p>
                </div>
                <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => setEditing(note.id)}>Edit</Button>
                <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" aria-label={`Delete note ${note.id}`}
                  onClick={() => void save(() => deleteNote(note.id, note.revision), "Note deleted.")}>
                  Delete
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {memories.length === 0 && <p className="text-sm text-faint">No notes yet.</p>}
    </div>
  );
}

function Suggestions() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "proposals"], queryFn: readProposals });
  const [stale, setStale] = useState<string | null>(null); // the suggestion whose dates have passed
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the suggestions.</p>;
  const decide = async (p: (typeof query.data.proposals)[number], decision: "keep" | "discard", withoutDates = false) => {
    try {
      await decideProposal(p.pass_id, p.item, decision, withoutDates);
      toast(decision === "keep" ? "Kept as a note." : "Discarded.");
      setStale(null);
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "stale_date") {
        setStale(`${p.pass_id}/${p.item}`);
        toast("Its date has passed: keep it without dates, or discard it.", "info");
      } else {
        toast(why(failure, "Could not decide it."), "error");
      }
    } finally {
      void client.invalidateQueries({ queryKey: ["settings"] });
    }
  };
  if (!query.data.proposals.length) return <p className="text-sm text-faint">No suggestions wait for you.</p>;
  return (
    <ul className="space-y-2" aria-label="Suggestions">
      {query.data.proposals.map((p) => (
        <li key={`${p.pass_id}/${p.item}`} className="rounded-lg border border-line px-3 py-2 text-sm">
          <p className="whitespace-pre-wrap break-words">{p.text}</p>
          <p className="mt-1 text-xs text-faint">{p.category} · {p.kind === "sensitive" ? "sensitive" : "from the past"} ·
            from {p.conversation.title || p.conversation.preview || "a conversation"}</p>
          <div className="mt-2 flex gap-2">
            {stale === `${p.pass_id}/${p.item}`
              ? <Button variant="primary" onClick={() => void decide(p, "keep", true)}>Keep without dates</Button>
              : <Button variant="primary" onClick={() => void decide(p, "keep")}>Keep</Button>}
            <Button variant="ghost" onClick={() => void decide(p, "discard")}>Discard</Button>
          </div>
        </li>
      ))}
    </ul>
  );
}

function EpisodeRow({ episode, onChanged }: { episode: Episode; onChanged: () => void }) {
  const [editing, setEditing] = useState<number | null>(null); // the revision the draft was opened from
  const [text, setText] = useState(episode.text);
  const act = async (run: () => Promise<unknown>, done: string) => {
    try {
      await run();
      toast(done);
      setEditing(null);
    } catch (failure) {
      toast(failure instanceof ApiError && failure.status === 409
        ? "It was changed elsewhere since you opened it; your text is still here." : why(failure, "Could not change it."), "error");
    } finally {
      onChanged();
    }
  };
  return (
    <li className="rounded-lg border border-line px-3 py-2 text-sm">
      <p className="text-xs text-faint">
        {new Date(episode.started_at).toLocaleString()} ·{" "}
        <Link to={`/c/${encodeURIComponent(episode.conversation.id)}`} className="underline">
          {episode.conversation.title || episode.conversation.preview || "a conversation"}
        </Link>
      </p>
      {editing !== null ? (
        <>
          <textarea aria-label="Timeline entry" value={text} rows={3} onChange={(event) => setText(event.target.value)}
            className={cn(field, "mt-1 block w-full resize-y")} />
          <div className="mt-1 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            <Button variant="primary" onClick={() => void act(() => editEpisode(episode.id, text, editing), "Saved.")}>Save</Button>
          </div>
        </>
      ) : (
        <div className="mt-1 flex items-start gap-2">
          <p className="min-w-0 flex-1 whitespace-pre-wrap break-words">{episode.text}</p>
          <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => { setText(episode.text); setEditing(episode.revision); }}>Edit</Button>
          <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger"
            onClick={() => void act(() => deleteEpisode(episode.id, episode.revision), "Deleted.")}>Delete</Button>
        </div>
      )}
    </li>
  );
}

function Timeline() {
  const client = useQueryClient();
  const pages = useInfiniteQuery({
    queryKey: ["settings", "episodes"],
    queryFn: ({ pageParam }) => readEpisodes(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next ?? undefined,
  });
  if (pages.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (pages.error) return <p role="alert" className="text-sm text-danger">Could not read the timeline.</p>;
  const episodes = pages.data?.pages.flatMap((p) => p.episodes) ?? [];
  if (!episodes.length) return <p className="text-sm text-faint">Nothing on the timeline yet.</p>;
  return (
    <>
      <ul className="space-y-2" aria-label="Timeline">
        {episodes.map((e) => (
          <EpisodeRow key={e.id} episode={e} onChanged={() => void client.invalidateQueries({ queryKey: ["settings", "episodes"] })} />
        ))}
      </ul>
      {pages.hasNextPage && (
        <Button variant="ghost" className="mt-2" disabled={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}>
          {pages.isFetchingNextPage ? "Loading…" : "Show more"}
        </Button>
      )}
    </>
  );
}

export function MemorySettings() {
  const [tab, setTab] = useState<"notes" | "suggestions" | "timeline">("notes");
  return (
    <section className="space-y-4" aria-label="Memory">
      <h2 className="text-base font-semibold">Memory</h2>
      <div className="flex gap-1 border-b border-line" role="tablist" aria-label="Memory">
        {(["notes", "suggestions", "timeline"] as const).map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={cn("border-b-2 px-3 py-1.5 text-sm capitalize", tab === t ? "border-accent text-ink" : "border-transparent text-muted")}>
            {t}
          </button>
        ))}
      </div>
      {tab === "notes" ? <Notes /> : tab === "suggestions" ? <Suggestions /> : <Timeline />}
    </section>
  );
}
