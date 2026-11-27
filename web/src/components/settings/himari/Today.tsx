// Settings → HIMARI → Today (character-routine §9.2): HIMARI now — whether its life runs, the model its self runs
// use, its work, the world it is told and its mood, what it does and its sleep (character-state, character-day §18.12)
// — then its slots for today and tomorrow, with last night's sleep slot while HIMARI is still in it (only read here:
// they are moved and changed on the Schedule tab), and what the world made happen to it today (edited like any entry).
import { Fragment, type ReactNode } from "react";
import { Link } from "react-router";
import type { Life } from "@/api/settings";
import { addDays, dayLabel, sleepText, slotsByDay } from "@/features/schedule";
import { clock, EntryItem, Group, projectsOf, SlotRow, useEditing, workLine } from "./shared";

export function TodayTab({ life, today }: { life: Life; today: string }) {
  const editing = useEditing();
  const { writer, entries, limits, mood, now, world, work, mind, sleep } = life;
  const { projectOf } = projectsOf(entries);
  const locked = editing.busy || editing.open !== null;
  // HIMARI's mood, what it does, and (character-day §18.12) its sleep, which tells asleep from its other states
  const nowParts: ReactNode[] = [];
  if (mood) {
    nowParts.push(<>Mood: <span className="text-ink">{mood.mood}</span>{mood.reason ? ` (${mood.reason})` : ""}
      {` · since ${new Date(mood.set_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}</>);
  }
  if (now?.doing) nowParts.push(`Doing: ${now.doing}${now.since ? ` (from ${now.since})` : ""}`);
  else if (now?.asleep && !sleep) nowParts.push("Asleep");
  if (sleep) nowParts.push(`Sleep: ${sleepText(sleep)}`);
  const working = workLine(work, projectOf);
  const tomorrow = addDays(today, 1);
  const yesterday = addDays(today, -1);
  const running = sleep?.tonight?.id; // the sleep slot under way (or tonight's)
  const shown = entries.filter((entry) => entry.kind === "plan"
    && (entry.day === today || entry.day === tomorrow || (entry.day === yesterday && entry.id === running)));
  const days = slotsByDay(shown).map(({ day, slots }) => (
    <li key={day} className="space-y-2" aria-label={`Day ${day}`}>
      <p className="text-xs font-medium text-muted">{dayLabel(day, today)}</p>
      <ul className="space-y-2">
        {slots.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
            <SlotRow entry={entry} project={projectOf(entry)} />
          </li>
        ))}
      </ul>
    </li>
  ));
  const events = entries.filter((entry) => entry.kind === "event" && entry.day === today)
    .sort((a, b) => (a.time ?? "").localeCompare(b.time ?? ""))
    .map((entry) => <EntryItem key={entry.id} entry={entry} editing={editing} locked={locked} limits={limits} />);
  return (
    <div className="space-y-4">
      <div className="space-y-1" aria-label="State">
        <p className="text-xs text-faint">Life runs: {writer.told}</p>
        {mind && <p className="text-xs text-faint" aria-label="Mind">Mind: {mind.model} ({mind.reason})</p>}
        {working && <p className="text-xs text-faint" aria-label="Work status">{working}</p>}
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
      </div>
      <Group title="Today and tomorrow" note="HIMARI's slots, only read here"
        top={<p className="text-xs"><Link to="/settings/himari/schedule" className="underline">
          Move, change or add slots on the Schedule tab</Link></p>}
        empty="Nothing on the schedule today or tomorrow.">
        {days}
      </Group>
      <Group title="Today's events" note="what the world made happen to HIMARI today, at most 2 a day"
        empty="Nothing happened today yet.">
        {events}
      </Group>
    </div>
  );
}
