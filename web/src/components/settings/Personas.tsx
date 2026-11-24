// The companion personas (V3-P108 §6.2): keep several, use one in every conversation (or none), edit one from the
// revision opened (a save over a change made elsewhere is refused with 409 and says so), duplicate, delete.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import {
  addPersona, choosePersona, deletePersona, editPersona, readPersonas, type Persona, type Personas,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const KEY = ["settings", "personas"];
const field = "block w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);
const NEXT_TURN = "Used from each conversation's next turn.";

// §6.3, §14: what a first persona opens with; only a guide, sent to HIMARI as the user leaves it. A line that starts
// with [familiar] or [close] is told only once the bond reaches that stage (character-world §5); here it shows as written.
export const STARTER = [
  "이름:",
  "나와의 관계: (예: 오래 알고 지낸 친구)",
  "어디서 왔고 지금 어디에 있는지: (예: 다른 세계에서 왔고, 지금은 내 컴퓨터 화면에 산다)",
  "나를 부르는 말:",
  "말투: (반말/존댓말, 문장 길이, 자주 쓰는 표현)",
  "대사 예시: (이 캐릭터가 할 법한 말 서너 개)",
  "- ",
  "- ",
  "- ",
  "성격:",
  "습관:",
  "좋아하는 것 / 싫어하는 것:",
  "하지 않는 것: (예: 지나친 칭찬, 이모지)",
  "숨긴 이야기: 줄 앞에 [familiar] 또는 [close]를 붙이면 그만큼 가까워진 뒤에 드러납니다", // character-world §5
].join("\n");

const nameKey = (name: string) => name.trim().normalize("NFC").toLowerCase();

/** "<name> copy", or "<name> copy 2", … — the first one no persona has. Cut in code points, as the server counts;
 * names compare as near the server's case folding as the browser can (a rare miss is refused as duplicate_name). */
export function copyName(name: string, taken: string[], max: number): string {
  const names = new Set(taken.map(nameKey));
  const points = Array.from(name.trim().normalize("NFC"));
  for (let n = 1; ; n += 1) {
    const suffix = n === 1 ? " copy" : ` copy ${n}`;
    const made = points.slice(0, Math.max(1, max - suffix.length)).join("") + suffix;
    if (!names.has(nameKey(made))) return made;
  }
}

type Draft = { id: number | null; name: string; text: string; revision: number };

function Editor({ draft, limits, onDone }: {
  draft: Draft; limits: Personas["limits"]; onDone: (saved: boolean) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(draft.name);
  const [text, setText] = useState(draft.text);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const dirty = name !== draft.name || text !== draft.text || draft.id === null;
  const save = async () => {
    setSaving(true);
    setConflict(false);
    try {
      if (draft.id === null) await addPersona(name.trim(), text);
      else await editPersona(draft.id, name.trim(), text, draft.revision);
      toast(`“${name.trim()}” saved. ${NEXT_TURN}`);
      onDone(true);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 && failure.code === "revision_conflict") setConflict(true);
      else toast(why(failure, "Could not save the persona."), "error");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  return (
    <form className="space-y-2 rounded-lg border border-line p-3" aria-label="Persona editor"
      onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label className="block text-xs text-muted">
        Name <span className="text-faint">(only for this list; HIMARI's own name goes in the text)</span>
        <input aria-label="Persona name" value={name} maxLength={limits.name_chars} autoFocus readOnly={saving}
          onChange={(event) => setName(event.target.value)} className={cn(field, "mt-1")} />
      </label>
      <label className="block text-xs text-muted">
        Who HIMARI is with you: name, voice, how it addresses you, your relationship
        <textarea aria-label="Persona text" value={text} rows={12} readOnly={saving}
          onChange={(event) => setText(event.target.value)} className={cn(field, "mt-1 resize-y font-mono")} />
      </label>
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{text.length.toLocaleString()} / {limits.text_chars.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={saving} onClick={() => onDone(false)}>Cancel</Button>
          <Button type="submit" variant="primary"
            disabled={!dirty || saving || !name.trim() || text.length > limits.text_chars}>Save</Button>
        </div>
      </div>
      {conflict && (
        <p role="alert" className="rounded-lg border border-danger/40 px-3 py-2 text-sm text-danger">
          It was changed elsewhere since you opened it. Your text is still here (copy it if you want it); cancel and
          edit again to see theirs.
        </p>
      )}
    </form>
  );
}

function Row({ persona, inUse, busy, full, onUse, onEdit, onDuplicate, onDelete }: {
  persona: Persona; inUse: boolean; busy: boolean; full: boolean;
  onUse: () => void; onEdit: () => void; onDuplicate: () => void; onDelete: () => void;
}) {
  const first = persona.text.split("\n").find((line) => line.trim()) ?? "";
  return (
    <li className={cn("flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-sm",
      inUse ? "border-accent/50 bg-accent/5" : "border-line")}>
      <div className="min-w-0 flex-1">
        <span className="font-medium">{persona.name}</span>
        {inUse && <span className="ml-2 rounded bg-accent px-1.5 py-0.5 text-xs text-accent-ink">In use</span>}
        <span className="ml-2 text-xs text-faint">edited {new Date(persona.updated_at).toLocaleString()}</span>
        <p className="truncate text-xs text-muted">{first || "(no text yet)"}</p>
      </div>
      {!inUse && <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={busy} onClick={onUse}>Use</Button>}
      <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={busy} onClick={onEdit}>Edit</Button>
      <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={busy || full} onClick={onDuplicate}>Duplicate</Button>
      <Button variant="ghost" className="px-2 py-0.5 text-xs text-danger" disabled={busy} onClick={onDelete}>Delete</Button>
    </li>
  );
}

export function PersonaSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: KEY, queryFn: readPersonas });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data) return <p role="alert" className="text-sm text-danger">Could not read the personas.</p>;
  const { personas, choice, companion, limits } = query.data;
  const act = async (run: () => Promise<unknown>, done: string, fallback: string) => {
    setBusy(true);
    try {
      await run();
      toast(done);
    } catch (failure) {
      toast(why(failure, fallback), "error");
    } finally {
      setBusy(false);
      void client.invalidateQueries({ queryKey: KEY });
    }
  };
  const use = (persona: Persona | null) => act(() => choosePersona(persona?.id ?? null, choice.revision),
    persona ? `Using “${persona.name}”. ${NEXT_TURN}` : `No persona. ${NEXT_TURN}`, "Could not change the persona.");
  const duplicate = (persona: Persona) => {
    const name = copyName(persona.name, personas.map((p) => p.name), limits.name_chars);
    return act(() => addPersona(name, persona.text), `Made “${name}”.`, "Could not duplicate the persona.");
  };
  const remove = (persona: Persona) => {
    const chosen = persona.id === choice.persona_id;
    const message = `Delete “${persona.name}”?${chosen ? " HIMARI will use no persona until you pick another." : ""}`;
    if (window.confirm(message)) {
      void act(() => deletePersona(persona.id, persona.revision), `Deleted “${persona.name}”.`,
        "Could not delete the persona.");
    }
  };
  const full = personas.length >= limits.count;
  const locked = busy || draft !== null; // while a persona is open, nothing else may replace its unsaved text
  return (
    <section className="space-y-4" aria-label="Personas">
      <h2 className="text-base font-semibold">Personas</h2>
      <p className="text-sm text-muted">
        Who HIMARI is as your companion. Keep several and pick the one HIMARI uses in every conversation and on every
        device.
      </p>
      {!companion && <p className="text-sm text-wait">The companion is off on this server: personas are not used now.</p>}
      <ul className="space-y-2" aria-label="Persona list">
        {personas.map((persona) => draft?.id === persona.id ? (
          <li key={persona.id}>
            <Editor draft={draft} limits={limits} onDone={() => setDraft(null)} />
          </li>
        ) : (
          <Row key={persona.id} persona={persona} inUse={persona.id === choice.persona_id} busy={locked} full={full}
            onUse={() => void use(persona)} onDuplicate={() => void duplicate(persona)} onDelete={() => remove(persona)}
            onEdit={() => setDraft({ id: persona.id, name: persona.name, text: persona.text, revision: persona.revision })} />
        ))}
        <li className={cn("flex items-center gap-2 rounded-lg border px-3 py-2 text-sm",
          choice.persona_id === null ? "border-accent/50 bg-accent/5" : "border-line")}>
          <div className="min-w-0 flex-1">
            <span className="font-medium">No persona</span>
            {choice.persona_id === null && (
              <span className="ml-2 rounded bg-accent px-1.5 py-0.5 text-xs text-accent-ink">In use</span>
            )}
            <p className="text-xs text-muted">HIMARI speaks warmly and plainly, in its own default voice.</p>
          </div>
          {choice.persona_id !== null && (
            <Button variant="ghost" className="px-2 py-0.5 text-xs" disabled={locked} onClick={() => void use(null)}>Use</Button>
          )}
        </li>
      </ul>
      {draft?.id === null ? (
        <Editor draft={draft} limits={limits} onDone={() => setDraft(null)} />
      ) : (
        <div className="flex items-center gap-3">
          <Button variant="primary" disabled={full || locked}
            onClick={() => setDraft({ id: null, name: "", text: personas.length === 0 ? STARTER : "", revision: 0 })}>
            New persona
          </Button>
          {full && <span className="text-xs text-faint">At most {limits.count} personas; delete one first.</span>}
        </div>
      )}
    </section>
  );
}
