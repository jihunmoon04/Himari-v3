// Settings → HIMARI → Records (character-routine §9.2): HIMARI's days, a line a date, newest first (today too), each
// saying what the date holds. Opened, a date shows its diary and events (edited like any entry), its slots (only read:
// a cancelled or missed one with why), the sleeps that fell then (when and how it fell asleep and woke) and the work
// pieces made then (edited like any entry). First the last 30 days the life sends (from its ``since``: a piece of an
// open project older than that is under Projects, and here only once the older dates are read); "Show older" reads on
// 30 days at a time, past dates with nothing (/api/life/history). The older pages are read again after the owner's
// own changes and a persona change, never on focus or HIMARI's sleep (character-routine §9.2, review 1).
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { readLifeHistory, type Life, type LifeEntry } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { recordDays, recordLine, type DayRecord } from "@/features/schedule";
import { EntryItem, Group, HISTORY_KEY, projectsOf, SleepItem, SlotRow, useEditing, type Editing } from "./shared";

function Part({ title, children }: { title: string; children: ReactNode[] }) {
  if (!children.length) return null;
  return (
    <div className="space-y-1" aria-label={title}>
      <h4 className="text-xs font-medium text-muted">{title}</h4>
      <ul className="space-y-2">{children}</ul>
    </div>
  );
}

function DayRow({ record, today, open, onToggle, editing, locked, limits, projectOf }: {
  record: DayRecord; today: string; open: boolean; onToggle: () => void; editing: Editing; locked: boolean;
  limits: Life["limits"]; projectOf: (entry: LifeEntry) => string | null;
}) {
  const item = (entry: LifeEntry) => (
    <EntryItem key={entry.id} entry={entry} editing={editing} locked={locked} limits={limits}
      project={projectOf(entry)} />
  );
  const body = `record-${record.day}`;
  return (
    <li className="rounded-lg border border-line text-sm" aria-label={`Record ${record.day}`}>
      <button type="button" aria-expanded={open} aria-controls={open ? body : undefined} onClick={onToggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-raised">
        <span className="min-w-0 flex-1 break-words">{recordLine(record, today)}</span>
        <span className="text-xs text-faint" aria-hidden>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div id={body} className="space-y-3 border-t border-line px-3 py-2">
          <Part title="Diary">{record.diary.map(item)}</Part>
          <Part title="Events">{record.events.map(item)}</Part>
          <Part title="Slots">
            {record.slots.map((entry) => (
              <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
                <SlotRow entry={entry} project={projectOf(entry)} />
              </li>
            ))}
          </Part>
          <Part title="Sleep">
            {record.sleeps.map((sleep) => <SleepItem key={sleep.id ?? sleep.fell_at} sleep={sleep} />)}
          </Part>
          <Part title="Pieces">{record.pieces.map(item)}</Part>
        </div>
      )}
    </li>
  );
}

export function RecordsTab({ life, today, since }: { life: Life; today: string; since: string }) {
  const client = useQueryClient();
  const editing = useEditing();
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  // the older pages are read only once asked for (kept while the page is open, read again with the life)
  const [asked, setAsked] = useState(() => client.getQueryData(HISTORY_KEY) !== undefined);
  const history = useInfiniteQuery({
    queryKey: HISTORY_KEY,
    queryFn: ({ pageParam }) => readLifeHistory(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.more ? last.from : undefined),
    enabled: asked,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const pages = history.data?.pages ?? [];
  const { titles, projectOf: planProject } = projectsOf(life.entries);
  // a work slot's project, and a piece's
  const projectOf = (entry: LifeEntry) => entry.kind === "work" && entry.arc_id != null
    ? titles.get(entry.arc_id) ?? null : planProject(entry);
  const shown = life.entries.filter((entry) => !entry.day || entry.day >= since);
  const records = recordDays([...shown, ...pages.flatMap((page) => page.entries)],
    [...(life.sleeps ?? []), ...pages.flatMap((page) => page.sleeps)], today);
  const locked = editing.busy || editing.open !== null;
  const toggle = (day: string) => setOpened((was) => {
    const next = new Set(was);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    return next;
  });
  const older = () => {
    if (!asked) setAsked(true);
    else if (!pages.length) void history.refetch(); // the first read failed
    else void history.fetchNextPage();
  };
  const reading = history.isFetching;
  const done = pages.length > 0 && !history.hasNextPage;
  const rows = records.map((record) => (
    <DayRow key={record.day} record={record} today={today} open={opened.has(record.day)}
      onToggle={() => toggle(record.day)} editing={editing} locked={locked} limits={life.limits}
      projectOf={projectOf} />
  ));
  return (
    <div className="space-y-3">
      <Group title="Records" note="a line a date, newest first; open one for its diary, events, slots, sleep and pieces"
        empty={pages.length ? "Nothing recorded." : "Nothing recorded in the last 30 days."}>
        {rows}
      </Group>
      <div className="flex items-center justify-end gap-3">
        {history.isError && <span role="alert" className="text-xs text-danger">Could not read the older records.</span>}
        {done ? <span className="text-xs text-faint">Nothing older.</span> : (
          <Button disabled={reading} onClick={older}>{reading ? "Reading…" : "Show older"}</Button>
        )}
      </div>
    </div>
  );
}
