// An agent's request that waits for the user (V3-P102 §4.2, §4.6): a permission with the agent's own choices, or a
// question with its fields. What can be answered always comes from the latest pending list, never from the event
// that placed the request. After any answer (taken or refused) the pending list is read again (§7.3).
//
// Every text here was written by an agent: it is shown as plain text only.
import { useQueryClient } from "@tanstack/react-query";
import { CircleHelp, ShieldQuestion } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "@/api/client";
import { answerHubPending, cancelHubTask, type HubAnswer, type HubPending } from "@/api/hub";
import { hubKeys } from "@/app/queries";
import { allows, answerContent, hasOwnReject, whereFrom, type Draft } from "@/features/hub/answer";
import { cn } from "@/lib/cn";
import { Button } from "./ui/button";

const field = "w-full rounded-md border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-faint";

function Fields({ item, draft, onChange }: { item: HubPending; draft: Draft; onChange: (draft: Draft) => void }) {
  return (
    <div className="mt-3 space-y-3">
      {(item.fields ?? []).map((spec) => {
        const id = `hub-${item.id}-${spec.name}`;
        const value = draft[spec.name];
        return (
          <div key={spec.name}>
            <label htmlFor={id} className="block text-xs font-medium text-muted">{spec.title || spec.name}</label>
            {spec.description && <p className="text-xs text-faint">{spec.description}</p>}
            {(spec.choices ?? []).length > 0 ? (
              <select
                id={id}
                multiple={spec.multiple}
                className={cn(field, "mt-1")}
                value={spec.multiple ? (Array.isArray(value) ? value : []) : typeof value === "string" ? value : ""}
                onChange={(event) => onChange({
                  ...draft,
                  [spec.name]: spec.multiple ? [...event.target.selectedOptions].map((o) => o.value) : event.target.value,
                })}
              >
                {!spec.multiple && <option value="">Choose…</option>}
                {(spec.choices ?? []).map((choice) => (
                  <option key={String(choice.value)} value={String(choice.value)} title={choice.description ?? ""}>
                    {choice.title || String(choice.value)}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type="text"
                className={cn(field, "mt-1")}
                value={typeof value === "string" ? value : ""}
                onChange={(event) => onChange({ ...draft, [spec.name]: event.target.value })}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** ``where``: show the project, agent and device and the task's prompt (the inbox; a session's own page knows them).
 *  ``session``: link to the session the request belongs to, when it is known. */
export function HubRequest({ item, where, session }: { item: HubPending; where?: boolean; session?: number | null }) {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({});

  const settle = () => {
    void client.invalidateQueries({ queryKey: hubKeys.pending });
    void client.invalidateQueries({ queryKey: ["hub", "session"] });
  };
  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    setProblem(null);
    try {
      await run();
    } catch (failure) {
      setProblem(failure instanceof ApiError && failure.code === "hub_refused"
        ? "This request is no longer waiting (answered elsewhere, or it expired)."
        : failure instanceof ApiError ? failure.message : "Could not answer.");
    } finally {
      setBusy(false);
      settle(); // taken or refused: what waits is read again
    }
  };
  const answer = (body: HubAnswer) => act(() => answerHubPending(item.id, body));
  const stop = () => {
    if (!window.confirm("Stop this agent's task? What it has done so far stays.")) return;
    void act(() => cancelHubTask(item.task_id));
  };

  const permission = item.kind === "permission";
  return (
    <section
      role="group"
      aria-label={permission ? "Permission request" : "Question"}
      className="rounded-xl border border-wait/40 bg-wait/5 px-4 py-3"
    >
      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-wait">
        {permission ? <ShieldQuestion className="size-3.5" /> : <CircleHelp className="size-3.5" />}
        {permission ? "Permission" : "Question"} · Waiting for you
      </div>
      {where && (
        <div className="mb-1 text-xs text-muted">
          {whereFrom(item)}
          {session != null && (
            <>
              {" · "}
              <Link to={`/s/${session}`} className="underline hover:text-ink">Open session</Link>
            </>
          )}
        </div>
      )}
      {where && item.task?.prompt && <p className="mb-1 line-clamp-2 text-xs text-faint">{item.task.prompt}</p>}
      <p className="whitespace-pre-wrap break-words text-sm">{item.asks || (permission ? "Permission" : "Question")}</p>
      {!permission && <Fields item={item} draft={draft} onChange={setDraft} />}
      <div className="mt-3 flex flex-wrap gap-2">
        {permission ? (
          (item.options ?? []).map((option) => (
            <Button
              key={option.option_id}
              variant={allows(option.kind) ? "primary" : "outline"}
              disabled={busy}
              onClick={() => void answer({ option_id: option.option_id })}
            >
              {option.name || option.option_id}
            </Button>
          ))
        ) : (
          <Button variant="primary" disabled={busy}
            onClick={() => void answer({ content: answerContent(item.fields ?? [], draft) })}>
            Answer
          </Button>
        )}
        {!hasOwnReject(item) && (
          <Button variant="outline" disabled={busy} onClick={() => void answer({ refuse: true })}>
            {permission ? "Refuse" : "Decline"}
          </Button>
        )}
        <Button variant="ghost" disabled={busy} onClick={stop}>Stop task</Button>
      </div>
      {problem && <p role="alert" className="mt-2 text-xs text-danger">{problem}</p>}
    </section>
  );
}
