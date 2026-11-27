// Settings → HIMARI → Schedule (character-day §12, §18.12; character-routine §9.2): HIMARI's slots from today (the
// server's) on, by day, each with its kind, start and length, and a cancelled or missed one with why. The owner adds,
// edits and deletes slots; the sleep slot and (character-diary §10) the diary slot only move (never deleted). A slot of
// a date before today is not here: it is in that date's record (the Records tab).
import { useState } from "react";
import type { Life } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { dayLabel, slotsByDay } from "@/features/schedule";
import { Group, PlanEditor, projectsOf, SlotRow, useEditing } from "./shared";

export function ScheduleTab({ life, today }: { life: Life; today: string }) {
  const editing = useEditing();
  const [adding, setAdding] = useState(false); // a slot being written
  const { persona, entries, limits } = life;
  const { projectOf } = projectsOf(entries);
  // while a slot is open (or one being added), nothing else may change under its unsaved text
  const locked = editing.busy || editing.open !== null || adding;
  const openProjects = entries.filter((entry) => entry.kind === "arc" && entry.status === "open")
    .map((entry) => ({ id: entry.id, title: entry.title ?? `Project ${entry.id}` }));
  const schedule = slotsByDay(entries).filter(({ day }) => day >= today).map(({ day, slots }) => (
    <li key={day} className="space-y-2" aria-label={`Day ${day}`}>
      <p className="text-xs font-medium text-muted">{dayLabel(day, today)}</p>
      <ul className="space-y-2">
        {slots.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-line px-3 py-2 text-sm">
            {editing.open?.id === entry.id ? (
              <PlanEditor entry={entry} revision={editing.open.revision} personaId={persona?.id ?? null}
                projects={openProjects} today={today} max={limits.chars} onDone={() => editing.setOpen(null)} />
            ) : (
              <SlotRow entry={entry} locked={locked} project={projectOf(entry)}
                onEdit={() => editing.setOpen({ id: entry.id, revision: entry.revision })}
                onDelete={() => void editing.remove(entry)} />
            )}
          </li>
        ))}
      </ul>
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
  return (
    <Group title="Schedule" note="HIMARI's slots from today on; the sleep and diary slots only move"
      top={addSlot} empty="Nothing on the schedule from today on.">
      {schedule}
    </Group>
  );
}
