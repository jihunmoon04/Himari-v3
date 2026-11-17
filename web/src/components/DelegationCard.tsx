// A task HIMARI handed to the hub (V3-P102 §5.1), followed by its id to the end: the hub's ``task`` is read every
// 15 s while it runs, and again whenever the session it runs in changes on the list stream (a hint to read, never
// the task's state: a session is idle after success and failure alike). What it waits on, where it runs, and how it
// ended come from the task; "Open session" goes there.
//
// The prompt and the result were written by HIMARI and an agent: shown as plain text only.
import { useQueryClient } from "@tanstack/react-query";
import { Bot, CircleAlert, CircleCheck, LoaderCircle, ShieldQuestion } from "lucide-react";
import { useEffect } from "react";
import { Link } from "react-router";
import { hubKeys, useHubProjects, useHubTask } from "@/app/queries";
import { useHubList } from "@/features/hub/list";
import type { Delegation } from "@/features/timeline";
import { cn } from "@/lib/cn";

const LABELS: Record<string, string> = {
  queued: "Queued", running: "Working", waiting: "Waiting for you", done: "Done", failed: "Failed",
};

export function DelegationCard({ delegation, compact }: { delegation: Delegation; compact?: boolean }) {
  const client = useQueryClient();
  const hub = useHubProjects();
  const on = hub.status === "on";
  const task = useHubTask(delegation.taskId, on);
  const list = useHubList(on);
  const now = task.data?.task;
  const session = now?.session ?? delegation.session;
  const listed = session != null ? list.sessions.find((s) => s.id === session) : undefined;

  // the session it runs in changed (it started, stopped, asked something): read the task again now
  const hint = listed ? `${listed.activity}/${listed.task}/${listed.state}` : "";
  useEffect(() => {
    if (hint && delegation.taskId !== null) void client.invalidateQueries({ queryKey: hubKeys.task(delegation.taskId) });
  }, [client, delegation.taskId, hint]);

  const status = delegation.error ? "refused" : now?.status ?? delegation.status ?? "queued";
  const waiting = now?.pending?.length ?? 0;
  const tone = status === "failed" || status === "refused" ? "bad" : status === "done" ? "good"
    : status === "waiting" || waiting > 0 ? "wait" : "run";

  return (
    <section
      role="group"
      aria-label="Handed to the hub"
      className={cn("rounded-xl border px-4 py-3 text-sm", tone === "wait" ? "border-wait/40 bg-wait/5"
        : tone === "bad" ? "border-danger/40" : "border-line")}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <Bot className="size-3.5" aria-hidden />
        <span className="font-medium text-ink">{delegation.project || "hub"}</span>
        <span>{[now?.agent ?? delegation.agent, now?.runner].filter(Boolean).join("@") || "agent"}</span>
        <span className={cn("inline-flex items-center gap-1",
          tone === "bad" && "text-danger", tone === "good" && "text-ok", tone === "wait" && "text-wait")}>
          {tone === "run" && <LoaderCircle className="size-3 animate-spin" aria-hidden />}
          {tone === "good" && <CircleCheck className="size-3" aria-hidden />}
          {tone === "bad" && <CircleAlert className="size-3" aria-hidden />}
          {tone === "wait" && <ShieldQuestion className="size-3" aria-hidden />}
          {status === "refused" ? "Not handed over" : LABELS[status] ?? status}
        </span>
        {delegation.taskId !== null && <span className="text-faint">task #{delegation.taskId}</span>}
        {session != null && (
          <Link to={`/s/${session}`} className="ml-auto underline hover:text-ink">Open session</Link>
        )}
      </div>
      {!compact && delegation.prompt && (
        <p className="mt-1.5 line-clamp-3 whitespace-pre-wrap break-words text-muted">{delegation.prompt}</p>
      )}
      {delegation.error && <p className="mt-1.5 whitespace-pre-wrap break-words text-danger">{delegation.error}</p>}
      {waiting > 0 && (
        <p className="mt-1.5 text-xs text-wait">
          {waiting === 1 ? "The agent waits for your answer" : `The agent waits for ${waiting} answers`}
          {" · "}
          {session != null
            ? <Link to={`/s/${session}`} className="underline">answer in the session</Link>
            : <Link to="/approvals" className="underline">answer in Approvals</Link>}
        </p>
      )}
      {!compact && now?.result && (status === "done" || status === "failed") && (
        <p className={cn("mt-1.5 line-clamp-4 whitespace-pre-wrap break-words",
          status === "failed" ? "text-danger" : "text-muted")}>
          {now.result}
        </p>
      )}
      {on && task.error && delegation.taskId !== null && (
        <p className="mt-1.5 text-xs text-faint">Could not read the task from the hub; trying again.</p>
      )}
    </section>
  );
}
