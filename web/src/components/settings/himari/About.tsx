// Settings → HIMARI → About HIMARI (character-life §6, character-mind §8; character-routine §9.2): the facts it has
// settled about itself (the owner adds them up to the limit, edits and deletes any) and its kept thoughts (what it
// thought and left unsaid: the text, what it is about, when it was kept and when it stops being told), only deleted.
import type { Life } from "@/api/settings";
import { AddFact, EntryItem, Group, useEditing } from "./shared";

export function AboutTab({ life }: { life: Life }) {
  const editing = useEditing();
  const { persona, entries, limits } = life;
  const locked = editing.busy || editing.open !== null;
  const item = (entry: Life["entries"][number]) => (
    <EntryItem key={entry.id} entry={entry} editing={editing} locked={locked} limits={limits} />
  );
  const kept = entries.filter((entry) => entry.kind === "fact");
  // §2: at the limit a new fact retires the oldest one not yours; only when all are yours is there no room
  const full = kept.length >= limits.facts && kept.every((entry) => entry.origin === "user");
  return (
    <div className="space-y-4">
      <Group title="Facts" note={`${kept.length} of ${limits.facts}`} empty="No facts yet."
        top={<AddFact personaId={persona?.id ?? null} full={full} max={limits.chars} />}>
        {kept.map(item)}
      </Group>
      <Group title="Thoughts" note="what HIMARI thought and left unsaid, kept a few days from conversations"
        empty="No kept thoughts.">
        {entries.filter((entry) => entry.kind === "thought").map(item)}
      </Group>
    </div>
  );
}
