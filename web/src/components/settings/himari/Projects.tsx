// Settings → HIMARI → Projects (character-world §4, character-work §8; character-routine §9.2): the open projects
// (arcs: title, goal, so far, the day each started, why last changed), each with its pieces newest first (date and
// time, title, summary, the full text when opened), then the closed ones with how each ended. Projects and pieces are
// edited (a project's title and goal; a piece's title, summary and text) and deleted like any entry; character-routine
// §11.4: an open project is ended with a one-line ending (from the revision opened; no reopening). The work line says
// whether the worker runs and the next work time.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { endProject, type Life, type LifeEntry } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";
import {
  EntryItem, field, Group, KEY, NEXT_TURN, projectsOf, refused, useEditing, useUnsaved, why, workLine, type Editing,
} from "./shared";

const ARC_CHARS = 200; // as the server's, when it does not say

/** character-routine §11.4: the ending of an open project, from the revision it was opened with; after a change made
 *  elsewhere (409), from the project as it is now shown (review P2-4: the ending typed is kept for that). */
function EndForm({ project, revision, max, onDone }: {
  project: LifeEntry; revision: number; max: number; onDone: () => void;
}) {
  const client = useQueryClient();
  const [ending, setEnding] = useState("");
  const [saving, setSaving] = useState(false);
  const [base, setBase] = useState<number | null>(revision); // null: the revision shown now
  useUnsaved(ending.trim() !== "");
  const end = async () => {
    if (!window.confirm(`End "${project.title ?? ""}"? It cannot be opened again.`)) return;
    setSaving(true);
    try {
      await endProject(project.id, ending.trim(), base ?? project.revision);
      toast(`Ended the project. ${NEXT_TURN}`);
      onDone();
    } catch (failure) {
      const conflict = refused(failure, "revision_conflict");
      if (conflict) setBase(null);
      toast(conflict
        ? "The project was changed elsewhere since you opened it; it was not ended. Your ending is still here: look "
          + "at the project as it is now, and end it again to end it as it is."
        : why(failure, "Could not end the project."), "error");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  return (
    <form className="space-y-2 rounded-lg border border-line px-3 py-2 text-sm" aria-label="End the project"
      onSubmit={(event) => { event.preventDefault(); void end(); }}>
      <input aria-label="Ending" placeholder="How it ended, in one line" value={ending} maxLength={max} autoFocus
        readOnly={saving} onChange={(event) => setEnding(event.target.value)} className={field} />
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{ending.length.toLocaleString()} / {max.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={saving} onClick={onDone}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={saving || !ending.trim()}>End project</Button>
        </div>
      </div>
    </form>
  );
}

function ProjectBlock({ project, pieces, editing, locked, limits, ending, onEnd, onEnded }: {
  project: LifeEntry; pieces: LifeEntry[]; editing: Editing; locked: boolean; limits: Life["limits"];
  ending: number | null; onEnd: () => void; onEnded: () => void;
}) {
  const open = project.status === "open";
  return (
    <li className="space-y-2" aria-label={`Project ${project.title ?? ""}`}>
      <ul className="space-y-2">
        <EntryItem entry={project} editing={editing} locked={locked} limits={limits} />
      </ul>
      {open && ending !== null && (
        <EndForm project={project} revision={ending} max={limits.arc_chars ?? ARC_CHARS} onDone={onEnded} />
      )}
      {open && ending === null && (
        <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={onEnd}
          aria-label={`End project ${project.title ?? ""}`}>
          End project
        </Button>
      )}
      {!project.so_far && open && <p className="text-sm text-muted">Nothing made yet.</p>}
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
  // character-routine §11.4: the project being ended (one at a time, as an editor) and the revision it was opened at
  const [chosen, setEnding] = useState<{ id: number; revision: number } | null>(null);
  const { entries, limits, work } = life;
  // review P2-3: closed or gone meanwhile (by the diary, a piece, elsewhere): the form is gone and nothing stays locked
  const ending = chosen && entries.some((entry) => entry.id === chosen.id && entry.status === "open") ? chosen : null;
  const { titles, projectOf } = projectsOf(entries);
  const locked = editing.busy || editing.open !== null || ending !== null;
  // the pieces as listed (newest first); one of a project not listed goes last
  const pieces = entries.filter((entry) => entry.kind === "work");
  const block = (project: LifeEntry) => (
    <ProjectBlock key={project.id} project={project} pieces={pieces.filter((made) => made.arc_id === project.id)}
      editing={editing} locked={locked} limits={limits}
      ending={ending?.id === project.id ? ending.revision : null}
      onEnd={() => setEnding({ id: project.id, revision: project.revision })} onEnded={() => setEnding(null)} />
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
        note="HIMARI starts, changes and ends them as it talks or in its diary, at most 2 open; a piece at each work time"
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
