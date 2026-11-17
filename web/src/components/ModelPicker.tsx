// A HIMARI conversation's model and effort (V3-P64 §7, the classic page's model picker): saved at once, used from
// its next turn. "Default" follows the server's default model (or the model's default effort).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { readModelChoice, saveModelChoice } from "@/api/himari";
import { useServer } from "@/app/queries";
import { toast } from "@/features/toast";

// a save under way, per conversation: a message sent meanwhile waits for it, so it runs with the choice just made
const pending = new Map<string, Promise<unknown>>();

/** Resolves once the conversation's model choice under way (if any) is saved or refused. */
export const modelSettled = (conversation: string): Promise<unknown> => pending.get(conversation) ?? Promise.resolve();

const pick = "max-w-44 truncate rounded-md border border-line bg-transparent px-1.5 py-1 text-xs text-muted outline-none focus:border-faint";

export function ModelPicker({ conversation }: { conversation: string }) {
  const client = useQueryClient();
  const server = useServer(true).data;
  const key = ["conversation", conversation, "model"] as const;
  const choice = useQuery({ queryKey: key, queryFn: () => readModelChoice(conversation) });
  const [saving, setSaving] = useState(false); // one save at a time: the next choice is made from the saved one
  const models = server?.models ?? [];
  if (!choice.data || models.length === 0) return null;
  const chosenModel = choice.data.chosen?.model ?? null;
  const chosenEffort = choice.data.chosen?.effort ?? null;
  const using = models.find((m) => m.id === (chosenModel ?? choice.data.model));
  const efforts = using?.efforts ?? [];
  if (models.length < 2 && efforts.length === 0) return null; // nothing to choose
  const save = async (model: string | null, effort: string | null) => {
    setSaving(true);
    const saved = saveModelChoice(conversation, model, effort);
    const settled = saved.catch(() => undefined);
    pending.set(conversation, settled);
    void settled.then(() => { if (pending.get(conversation) === settled) pending.delete(conversation); });
    try {
      client.setQueryData(key, await saved);
    } catch (failure) {
      toast(failure instanceof ApiError ? failure.message : "Could not change the model.", "error");
    } finally {
      setSaving(false);
    }
  };
  const fallback = models.find((m) => m.default)?.id ?? server?.model ?? "default";
  return (
    <>
      {models.length > 1 && (
        <select aria-label="Model" title="Model (from the next turn)" className={pick} value={chosenModel ?? ""} disabled={saving}
          onChange={(event) => void save(event.target.value || null, null)}>
          <option value="">Default ({fallback})</option>
          {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
        </select>
      )}
      {efforts.length > 0 && (
        <select aria-label="Effort" title="Reasoning effort (from the next turn)" className={pick} value={chosenEffort ?? ""}
          disabled={saving}
          onChange={(event) => void save(chosenModel, event.target.value || null)}>
          <option value="">Effort: default{using?.default_effort ? ` (${using.default_effort})` : ""}</option>
          {efforts.map((e) => <option key={e} value={e}>{e}</option>)}
        </select>
      )}
    </>
  );
}
