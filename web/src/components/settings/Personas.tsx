// The companion personas (V3-P108 §6.2): keep several, use one in every conversation (or none), edit one from the
// revision opened (a save over a change made elsewhere is refused with 409 and says so), duplicate, delete.
// character-persona-fields §5: a persona is written in fields; what HIMARI is told is made from them and shown folded.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import {
  addPersona, choosePersona, deletePersona, editPersona, readPersonas, type Persona, type PersonaField,
  type PersonaFields, type Personas,
} from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";
import { cn } from "@/lib/cn";

const KEY = ["settings", "personas"];
const field = "block w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-faint";
const why = (failure: unknown, fallback: string) => (failure instanceof ApiError ? failure.message : fallback);
const NEXT_TURN = "Used from each conversation's next turn.";

// character-persona-fields §2: the fields in the order HIMARI's text is made from them. The label is how the field
// is named in that text (as the first persona's guide named it, §6.3); the hint is the guide, now a placeholder.
type FieldSpec = { key: PersonaField; label: string; hint: string; lines?: number };
export const FIELDS: FieldSpec[] = [
  { key: "name", label: "이름", hint: "HIMARI의 이름 (목록 이름과 따로)" },
  { key: "relation", label: "나와의 관계", hint: "예: 오래 알고 지낸 친구" },
  { key: "world", label: "어디서 왔고 지금 어디에 있는지", hint: "예: 다른 세계에서 왔고, 지금은 내 컴퓨터 화면에 산다" },
  { key: "calls_user", label: "나를 부르는 말", hint: "예: 대장" },
  { key: "personality", label: "성격", hint: "", lines: 2 },
  { key: "speech", label: "말투", hint: "반말/존댓말, 문장 길이, 자주 쓰는 표현", lines: 2 },
  { key: "examples", label: "대사 예시", hint: "한 줄에 하나, 3~5개. 말투에 가장 잘 듣습니다", lines: 4 },
  { key: "habits", label: "습관", hint: "", lines: 2 },
  { key: "likes", label: "좋아하는 것 / 싫어하는 것", hint: "", lines: 2 },
  { key: "makes", label: "하는 일과 만드는 것",
    hint: "혼자 있을 때 하는 일, 만드는 것 (예: 블랙홀 무기 설정집, 짧은 소설, 작곡). HIMARI의 프로젝트가 여기서 나옵니다",
    lines: 2 },
  { key: "never", label: "하지 않는 것", hint: "예: 지나친 칭찬, 이모지", lines: 2 },
  { key: "familiar", label: "가까워지면 드러나는 이야기", hint: "관계가 Familiar가 된 뒤에 HIMARI에게 전해집니다", lines: 2 },
  { key: "close", label: "아주 가까워지면 드러나는 이야기", hint: "관계가 Close가 된 뒤에 전해집니다", lines: 2 },
  { key: "free", label: "자유 글",
    hint: "칸에 맞지 않는 것 모두. 쓴 그대로 전해집니다. 줄 앞에 [familiar] 또는 [close]를 붙이면 그만큼 가까워진 뒤에 드러납니다",
    lines: 6 },
];
const HIDDEN = new Set<PersonaField>(["familiar", "close"]);
const MARK = /^\[(familiar|close)\][ \t]?/i;
const LINE_BREAKS = /\r\n?/g;
// a value that starts as a hidden line does goes under its label, so its mark stays at the line's start (review)
const MARKED_START = /^(?:[-*•][ \t]*)?\[(familiar|close)\]/i;
// Python's str.strip(): JavaScript's trim() also takes U+FEFF, which Python keeps (review)
const EDGES = /^[^\S﻿]+|[^\S﻿]+$/g;
const strip = (value: string) => value.replace(EDGES, "");

/** What HIMARI is told, made as the server makes it (data/rules.py persona_text): ``label: value`` (several lines
 * under the label), each hidden story line marked for its stage, free last as written; empty fields left out. */
export function personaText(fields: PersonaFields): string {
  const parts: string[] = [];
  for (const { key, label } of FIELDS) {
    const raw = (fields[key] ?? "").replace(LINE_BREAKS, "\n");
    if (!strip(raw)) continue;
    const value = key === "free" ? raw : strip(raw);
    if (key === "free") parts.push(value);
    else if (HIDDEN.has(key)) {
      parts.push(value.split("\n").filter((line) => strip(line))
        .map((line) => `[${key}] ${strip(line).replace(MARK, "")}`).join("\n"));
    } else parts.push(value.includes("\n") || MARKED_START.test(value) ? `${label}:\n${value}` : `${label}: ${value}`);
  }
  return parts.join("\n");
}

/** Characters as the server counts them (code points). */
const length = (value: string) => Array.from(value).length;

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

type Draft = { id: number | null; name: string; fields: PersonaFields; revision: number };
const same = (a: PersonaFields, b: PersonaFields) => FIELDS.every(({ key }) => (a[key] ?? "") === (b[key] ?? ""));

function Editor({ draft, limits, onDone }: {
  draft: Draft; limits: Personas["limits"]; onDone: (saved: boolean) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(draft.name);
  const [fields, setFields] = useState<PersonaFields>(draft.fields);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const dirty = name !== draft.name || !same(fields, draft.fields) || draft.id === null;
  const told = personaText(fields);
  // the server checks each field as written, before trimming, against the same limit (review)
  const overLong = FIELDS.some(({ key }) => {
    const raw = (fields[key] ?? "").replace(LINE_BREAKS, "\n");
    return strip(raw) !== "" && length(raw) > limits.text_chars; // only whitespace is no text, whatever its length
  });
  const set = (key: PersonaField, value: string) => setFields((now) => ({ ...now, [key]: value }));
  const save = async () => {
    setSaving(true);
    setConflict(false);
    try {
      if (draft.id === null) await addPersona(name.trim(), fields);
      else await editPersona(draft.id, name.trim(), fields, draft.revision);
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
      <p className="text-xs text-muted">Who HIMARI is with you. Every field may stay empty.</p>
      {FIELDS.map(({ key, label, hint, lines }) => (
        <label key={key} className="block text-xs text-muted">
          {label}
          {lines ? (
            <textarea aria-label={label} value={fields[key] ?? ""} rows={lines} placeholder={hint} readOnly={saving}
              onChange={(event) => set(key, event.target.value)} className={cn(field, "mt-1 resize-y")} />
          ) : (
            <input aria-label={label} value={fields[key] ?? ""} placeholder={hint} readOnly={saving}
              onChange={(event) => set(key, event.target.value)} className={cn(field, "mt-1")} />
          )}
        </label>
      ))}
      <details>
        <summary className="cursor-pointer text-xs text-muted">What HIMARI is told</summary>
        <pre aria-label="What HIMARI is told"
          className="mt-1 whitespace-pre-wrap break-words rounded-lg border border-line bg-bg p-2 font-mono text-xs">
          {told || "(nothing yet)"}
        </pre>
      </details>
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{length(told).toLocaleString()} / {limits.text_chars.toLocaleString()}</span>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={saving} onClick={() => onDone(false)}>Cancel</Button>
          <Button type="submit" variant="primary"
            disabled={!dirty || saving || !name.trim() || length(told) > limits.text_chars || overLong}>Save</Button>
        </div>
      </div>
      {conflict && (
        <p role="alert" className="rounded-lg border border-danger/40 px-3 py-2 text-sm text-danger">
          It was changed elsewhere since you opened it. Your fields are still here (copy what you want to keep);
          cancel and edit again to see theirs.
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
    return act(() => addPersona(name, persona.fields), `Made “${name}”.`, "Could not duplicate the persona.");
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
            onEdit={() => setDraft({ id: persona.id, name: persona.name, fields: persona.fields,
              revision: persona.revision })} />
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
            onClick={() => setDraft({ id: null, name: "", fields: {}, revision: 0 })}>
            New persona
          </Button>
          {full && <span className="text-xs text-faint">At most {limits.count} personas; delete one first.</span>}
        </div>
      )}
    </section>
  );
}
