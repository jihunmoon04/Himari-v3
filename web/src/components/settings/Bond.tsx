// You and HIMARI (character-settings §2): what HIMARI remembers from talking with the user. The bond
// (character-bond §1), for the persona in use (or the default): what is really settled between the user and HIMARI —
// names, promises, jokes, firsts and days to mark — and how close the two are (the stage, which the owner may set up
// or down). Entries are added, edited from the revision opened (a save over a change made elsewhere is refused with
// 409 and says so) and deleted (they retire: no undo). Then what HIMARI remembers about the user (the notes, shared by
// every persona), and the moments together: the conversations' episodes (edited and deleted here, S3) with the bond's
// entries, newest first. The app never shows any of it.
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import {
  addBond, BOND_STAGES, BOND_TAGS, deleteBond, editBond, readBond, readEpisodes, setBondStage, type Bond,
  type BondEntry, type BondStage, type BondTag, type Episode,
} from "@/api/settings";
import { EpisodeRow, RememberedAboutYou } from "@/components/settings/Memory";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const KEY = ["settings", "bond"];
const field = "block w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);
const refused = (failure: unknown, code: string) =>
  failure instanceof ApiError && failure.status === 409 && failure.code === code;
const NEXT_TURN = "Used from each conversation's next turn.";
const PERSONA_CHANGED = "The persona in use changed since this page was read; nothing was changed. The page now shows "
  + "the bond of the one in use.";

export const TAG_NAMES: Record<BondTag, string> = {
  name: "Name", promise: "Promise", joke: "Joke", first: "First", date: "Day to mark",
};
export const STAGE_NAMES: Record<BondStage, string> = {
  new: "New — still getting to know each other",
  familiar: "Familiar — at ease with each other",
  close: "Close — at home with each other",
};
const MONTH_DAY = /^\d{2}-\d{2}$/;

/** The local date (YYYY-MM-DD) of an instant the API wrote. */
export function localDay(at: string): string {
  const d = new Date(at);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function EntryForm({ initial, max, busy, submitLabel, onSubmit, onCancel }: {
  initial: { tag: BondTag; text: string; monthDay: string };
  max: number;
  busy: boolean;
  submitLabel: string;
  onSubmit: (tag: BondTag, text: string, monthDay: string | null) => Promise<boolean>;
  onCancel?: () => void;
}) {
  const [tag, setTag] = useState<BondTag>(initial.tag);
  const [text, setText] = useState(initial.text);
  const [monthDay, setMonthDay] = useState(initial.monthDay);
  const dated = tag === "date";
  const ready = !!text.trim() && (!dated || MONTH_DAY.test(monthDay.trim()));
  const unchanged = tag === initial.tag && text === initial.text && monthDay === initial.monthDay;
  const submit = async () => {
    if (await onSubmit(tag, text.trim(), dated ? monthDay.trim() : null) && !onCancel) {
      setText("");
      setMonthDay("");
    }
  };
  return (
    <form className="space-y-2" aria-label={onCancel ? "Bond entry editor" : "Add to the bond"}
      onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div className="flex flex-wrap gap-2">
        <select aria-label="Kind" value={tag} disabled={busy} onChange={(event) => setTag(event.target.value as BondTag)}
          className={cn(field, "w-auto")}>
          {BOND_TAGS.map((t) => <option key={t} value={t}>{TAG_NAMES[t]}</option>)}
        </select>
        {dated && (
          <input aria-label="Month and day" value={monthDay} placeholder="MM-DD" maxLength={5} readOnly={busy}
            onChange={(event) => setMonthDay(event.target.value)} className={cn(field, "w-24")} />
        )}
      </div>
      <textarea aria-label={onCancel ? "Bond entry text" : "New bond entry"} value={text} rows={2} maxLength={max}
        readOnly={busy} autoFocus={!!onCancel}
        placeholder="What the two of you settled, in a sentence (a day to mark: what the day is)."
        onChange={(event) => setText(event.target.value)} className={cn(field, "resize-y")} />
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{text.length.toLocaleString()} / {max.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          {onCancel && <Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>}
          <Button type="submit" variant="primary" disabled={busy || !ready || (!!onCancel && unchanged)}>
            {submitLabel}
          </Button>
        </div>
      </div>
    </form>
  );
}

function Row({ entry, locked, onEdit, onDelete }: {
  entry: BondEntry; locked: boolean; onEdit: () => void; onDelete: () => void;
}) {
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <p className="whitespace-pre-wrap break-words">{entry.text}</p>
        <p className="mt-1 text-xs text-faint">
          {TAG_NAMES[entry.tag]}{entry.month_day ? ` · ${entry.month_day}` : ""}
          {" · "}{entry.origin === "user" ? "yours" : "from a conversation"}
        </p>
      </div>
      <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEdit}>Edit</Button>
      <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={locked} onClick={onDelete}>
        Delete
      </Button>
    </div>
  );
}

function Stage({ bond, onChanged }: { bond: Bond; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const choose = async (stage: BondStage) => {
    setBusy(true);
    try {
      await setBondStage(stage, bond.persona?.id ?? null, bond.stage.revision);
      toast(`Stage set to ${stage}. It moves up by itself again only for what happens from now on. ${NEXT_TURN}`);
    } catch (failure) {
      toast(refused(failure, "persona_changed") ? PERSONA_CHANGED
        : refused(failure, "revision_conflict") ? "The stage moved since this page was read; nothing was changed. "
          + "See it as it is now."
          : why(failure, "Could not set the stage."), "error");
    } finally {
      setBusy(false);
      onChanged();
    }
  };
  return (
    <section className="space-y-2" aria-label="Stage">
      <h3 className="text-sm font-semibold">Stage</h3>
      <select aria-label="Stage" value={bond.stage.stage} disabled={busy}
        onChange={(event) => void choose(event.target.value as BondStage)} className={cn(field, "w-auto")}>
        {BOND_STAGES.map((s) => <option key={s} value={s}>{STAGE_NAMES[s]}</option>)}
      </select>
      <p className="text-xs text-faint">
        It moves up as things happen (a name or a joke, 30 days together; then a promise or a first with 90 days) and
        never down by itself. HIMARI is told it in words, never as a number; a relationship the persona sets comes first.
      </p>
    </section>
  );
}

type Moment = { at: number; episode: Episode; entry?: undefined } | { at: number; entry: BondEntry; episode?: undefined };

/** The moments together, newest first: the conversations' episodes, read a page at a time (edited and deleted here,
 * character-settings S3), with the bond's entries (edited above) as far back as the episodes read so far go. */
export function momentsOf(episodes: Episode[], entries: BondEntry[], more: boolean): Moment[] {
  // the API reads episodes newest first by their end: the moments go by it too, so a page's edge is a time
  const oldest = episodes.length ? Date.parse(episodes[episodes.length - 1]!.ended_at) : null;
  const moments: Moment[] = [
    ...episodes.map((episode) => ({ at: Date.parse(episode.ended_at), episode })),
    ...entries.map((entry) => ({ at: Date.parse(entry.created_at), entry }))
      .filter((m) => !more || oldest === null || m.at >= oldest),
  ];
  return moments.sort((a, b) => b.at - a.at);
}

function Moments({ entries, onChanged }: { entries: BondEntry[]; onChanged: () => void }) {
  const client = useQueryClient();
  const pages = useInfiniteQuery({
    queryKey: ["settings", "episodes"],
    queryFn: ({ pageParam }) => readEpisodes(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next ?? undefined,
  });
  const episodes = pages.data?.pages.flatMap((p) => p.episodes) ?? [];
  const moments = momentsOf(episodes, entries, pages.hasNextPage);
  const changed = () => { void client.invalidateQueries({ queryKey: ["settings", "episodes"] }); onChanged(); };
  return (
    <section className="space-y-2" aria-label="Moments together">
      <h3 className="text-sm font-semibold">Moments together</h3>
      <p className="text-xs text-faint">What you did together in each conversation, with what the two of you settled.</p>
      {pages.isLoading && <p className="text-sm text-faint">Loading…</p>}
      {pages.error && <p role="alert" className="text-sm text-danger">Could not read the conversations' moments.</p>}
      {moments.length ? (
        <ul className="space-y-2" aria-label="Moments">
          {moments.map((m) => m.episode ? <EpisodeRow key={`e${m.episode.id}`} episode={m.episode} onChanged={changed} /> : (
            <li key={`b${m.entry.id}`} className="rounded-lg border border-line px-3 py-2 text-sm">
              <p className="text-xs text-faint">{localDay(m.entry.created_at)} · {TAG_NAMES[m.entry.tag]}</p>
              <p className="mt-1 whitespace-pre-wrap break-words">{m.entry.text}</p>
            </li>
          ))}
        </ul>
      ) : !pages.isLoading && <p className="text-sm text-faint">Nothing yet.</p>}
      {pages.hasNextPage && (
        <Button variant="ghost" disabled={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}>
          {pages.isFetchingNextPage ? "Loading…" : "Show more"}
        </Button>
      )}
    </section>
  );
}

export function BondSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: KEY, queryFn: readBond });
  const refresh = () => void client.invalidateQueries({ queryKey: KEY });
  // the notes and the conversations' moments stay where they are whatever the bond's read does (an open draft in them
  // survives a failed refresh, review)
  return (
    <section className="space-y-4" aria-label="You and HIMARI">
      <h2 className="text-base font-semibold">You and HIMARI</h2>
      {query.isLoading ? <p className="text-sm text-faint">Loading…</p>
        : query.error || !query.data ? <p role="alert" className="text-sm text-danger">Could not read the bond.</p>
          : <TheBond bond={query.data} refresh={refresh} />}
      <RememberedAboutYou />
      <Moments entries={query.data?.entries ?? []} onChanged={refresh} />
    </section>
  );
}

function TheBond({ bond, refresh }: { bond: Bond; refresh: () => void }) {
  const [open, setOpen] = useState<{ id: number; revision: number } | null>(null); // the entry being edited
  const [busy, setBusy] = useState(false);
  const { persona, enabled, entries, limits } = bond;
  const add = async (tag: BondTag, text: string, monthDay: string | null) => {
    setBusy(true);
    try {
      await addBond(tag, text, monthDay, persona?.id ?? null);
      toast(`Added. ${NEXT_TURN}`);
      return true;
    } catch (failure) {
      toast(refused(failure, "persona_changed") ? PERSONA_CHANGED : why(failure, "Could not add it."), "error");
      return false; // a failed add keeps what was written
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const save = (entry: BondEntry, revision: number) => async (tag: BondTag, text: string, monthDay: string | null) => {
    setBusy(true);
    try {
      await editBond(entry.id, tag, text, monthDay, revision); // from the revision it was opened with
      toast(`Saved. ${NEXT_TURN}`);
      setOpen(null);
      return true;
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? "It was changed elsewhere since you opened it; nothing was saved. Your text is still here; cancel to see it "
          + "as it is now."
        : why(failure, "Could not save it."), "error");
      return false;
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const remove = async (entry: BondEntry) => {
    if (!window.confirm("Delete this from the bond? HIMARI will not know it any more; there is no undo.")) return;
    setBusy(true);
    try {
      await deleteBond(entry.id, entry.revision);
      toast(`Deleted. ${NEXT_TURN}`);
    } catch (failure) {
      toast(refused(failure, "revision_conflict")
        ? "It was changed elsewhere since you opened it; nothing was deleted. See it as it is now."
        : why(failure, "Could not delete it."), "error");
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const locked = busy || open !== null; // while an entry is open, nothing else may change under its unsaved text
  const full = entries.length >= limits.entries && entries.every((e) => e.origin === "user");
  return (
    <>
      <p className="text-sm text-muted">
        <span className="font-medium text-ink">{persona ? `${persona.name} and you` : "Default (no persona) and you"}</span>.
        {" "}What HIMARI remembers from talking with you. What the two of you have really settled — what you call each
        other, promises, jokes, firsts and days to mark — and how close you are is each persona's own. The app never
        shows it. {NEXT_TURN}
      </p>
      {bond.since && (
        <p className="text-sm">Together {bond.together_days.toLocaleString()} days, since {bond.since}.</p>
      )}
      {bond.today.length > 0 && <p className="text-sm text-ink">Today: {bond.today.join(", ")}</p>}
      {!enabled && (
        <p className="text-sm text-wait">
          The bond is not used now (the companion or life is off on this server); it is kept as it is.
        </p>
      )}
      <Stage bond={bond} onChanged={refresh} />
      <section className="space-y-2" aria-label="Settled between you">
        <h3 className="text-sm font-semibold">
          Settled between you
          <span className="ml-2 text-xs font-normal text-faint">{entries.length} of {limits.entries}</span>
        </h3>
        {full ? <p className="text-xs text-faint">Full of your own entries; delete one first.</p> : (
          <EntryForm initial={{ tag: "name", text: "", monthDay: "" }} max={limits.chars} busy={busy}
            submitLabel="Add" onSubmit={add} />
        )}
        {entries.length ? (
          <ul className="space-y-2">
            {entries.map((entry) => (
              <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
                {open?.id === entry.id ? (
                  <EntryForm initial={{ tag: entry.tag, text: entry.text, monthDay: entry.month_day ?? "" }}
                    max={limits.chars} busy={busy} submitLabel="Save" onSubmit={save(entry, open.revision)}
                    onCancel={() => setOpen(null)} />
                ) : (
                  <Row entry={entry} locked={locked} onEdit={() => setOpen({ id: entry.id, revision: entry.revision })}
                    onDelete={() => void remove(entry)} />
                )}
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-faint">Nothing settled yet.</p>}
      </section>
    </>
  );
}
