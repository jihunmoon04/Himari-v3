// Settings → HIMARI → Projects (character-world §4, character-work §8; character-routine §9.2): the open projects
// (arcs: title, goal, so far, the day each started), each with its pieces newest first (date and time, title, summary,
// the full text when opened), then the closed ones with how each ended. Projects and pieces are edited (a project's
// title and goal; a piece's title, summary and text) and deleted like any entry. The work line says whether the
// worker runs and the next work time.
import type { Life, LifeEntry } from "@/api/settings";
import { EntryItem, Group, projectsOf, useEditing, workLine, type Editing } from "./shared";

function ProjectBlock({ project, pieces, editing, locked, limits }: {
  project: LifeEntry; pieces: LifeEntry[]; editing: Editing; locked: boolean; limits: Life["limits"];
}) {
  return (
    <li className="space-y-2" aria-label={`Project ${project.title ?? ""}`}>
      <ul className="space-y-2">
        <EntryItem entry={project} editing={editing} locked={locked} limits={limits} />
      </ul>
      {!project.so_far && project.status === "open" && <p className="text-sm text-muted">Nothing made yet.</p>}
      {pieces.length > 0 && (
        <ul className="space-y-2 border-l border-line pl-3">
          {pieces.map((entry) => (
            <EntryItem key={entry.id} entry={entry} editing={editing} locked={locked} limits={limits} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function ProjectsTab({ life }: { life: Life }) {
  const editing = useEditing();
  const { entries, limits, work } = life;
  const { titles, projectOf } = projectsOf(entries);
  const locked = editing.busy || editing.open !== null;
  // the pieces as listed (newest first); one of a project not listed goes last
  const pieces = entries.filter((entry) => entry.kind === "work");
  const block = (project: LifeEntry) => (
    <ProjectBlock key={project.id} project={project} pieces={pieces.filter((made) => made.arc_id === project.id)}
      editing={editing} locked={locked} limits={limits} />
  );
  const open = entries.filter((entry) => entry.kind === "arc" && entry.status === "open").map(block);
  const closed = entries.filter((entry) => entry.kind === "arc" && entry.status === "closed").map(block);
  const loose = pieces.filter((made) => made.arc_id == null || !titles.has(made.arc_id))
    .map((entry) => <EntryItem key={entry.id} entry={entry} editing={editing} locked={locked} limits={limits} />);
  const working = workLine(work, projectOf);
  return (
    <div className="space-y-4">
      {working && <p className="text-xs text-faint" aria-label="Work status">{working}</p>}
      <Group title="Open projects"
        note="HIMARI starts, carries on and ends them in its day's review, at most 2 open; a piece at each work time"
        empty="No open projects.">
        {open}
      </Group>
      <Group title="Closed projects" note="ended in the last 30 days, with how each ended"
        empty="No project ended lately.">
        {closed}
      </Group>
      {loose.length > 0 && <Group title="Other pieces" note="of a project no longer listed" empty="">{loose}</Group>}
    </div>
  );
}
