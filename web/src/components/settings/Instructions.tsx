// Instructions for every conversation (V3-P69); the companion persona moved to Personas (V3-P108). Edited from the
// revision opened,
// so a save over a change made elsewhere is refused (409) and says so instead of overwriting it.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { readInstructions, saveInstructions, type InstructionsText } from "@/api/settings";
import { Button } from "@/components/ui/button";
import { toast } from "@/features/toast";

function Editor({ title, help, queryKey, read, save }: {
  title: string;
  help: string;
  queryKey: string;
  read: () => Promise<InstructionsText>;
  save: (text: string, revision: number) => Promise<InstructionsText>;
}) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", queryKey], queryFn: read });
  // the version the draft was opened from: what a save is checked against (a later read never takes its place
  // while there is a draft, so a change made elsewhere is refused, not overwritten)
  const [base, setBase] = useState<InstructionsText | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!query.data) return;
    if (base === null || (text === base.text && query.data.revision !== base.revision)) { // nothing written yet
      setBase(query.data);
      setText(query.data.text);
    }
  }, [query.data, base, text]);
  if (query.isLoading) return <p className="text-sm text-faint">Loading…</p>;
  if (query.error || !query.data || !base) return <p role="alert" className="text-sm text-danger">Could not read them.</p>;
  const data = base;
  const value = text ?? data.text;
  const dirty = value !== data.text;
  const reloadTheirs = async () => {
    setConflict(false);
    const fresh = await query.refetch();
    if (fresh.data) {
      setBase(fresh.data);
      setText(fresh.data.text);
    }
  };
  const submit = async () => {
    setSaving(true);
    setConflict(false);
    try {
      const saved = await save(value, data.revision);
      const kept = { ...data, ...saved };
      client.setQueryData(["settings", queryKey], kept);
      setBase(kept);
      setText(saved.text);
      toast(`${title} saved; used from each conversation's next turn.`);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) setConflict(true);
      else toast(failure instanceof ApiError ? failure.message : "Could not save them.", "error");
    } finally {
      setSaving(false);
    }
  };
  return (
    <section className="space-y-2" aria-label={title}>
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="text-sm text-muted">{help}</p>
      <textarea aria-label={title} value={value} readOnly={saving /* what is saved is what was written */}
        onChange={(event) => setText(event.target.value)} rows={10}
        className="block w-full resize-y rounded-lg border border-line bg-bg p-3 font-mono text-sm outline-none focus:border-faint" />
      <div className="flex items-center gap-3 text-xs text-faint">
        <span>{value.length.toLocaleString()} / {data.max_chars.toLocaleString()}</span>
        {data.updated_at && <span>saved {new Date(data.updated_at).toLocaleString()}</span>}
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" disabled={!dirty || saving} onClick={() => setText(data.text)}>Discard</Button>
          <Button variant="primary" disabled={!dirty || saving || value.length > data.max_chars} onClick={() => void submit()}>Save</Button>
        </div>
      </div>
      {conflict && (
        <div role="alert" className="flex items-center gap-2 rounded-lg border border-danger/40 px-3 py-2 text-sm text-danger">
          <span className="flex-1">They were changed elsewhere since you opened them. Your text is still here (copy it if you
            want it); reloading shows theirs in its place.</span>
          <Button onClick={() => void reloadTheirs()}>Reload theirs</Button>
        </div>
      )}
    </section>
  );
}

export function InstructionsSettings() {
  return (
    <div className="space-y-10">
      <Editor title="Instructions" queryKey="instructions" read={readInstructions} save={saveInstructions}
        help="Told to HIMARI in every conversation: how you like answers, what to keep in mind. Who HIMARI is lives in Personas." />
    </div>
  );
}
