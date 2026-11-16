// Every open hub session at a glance (V3-P102 §4.5): project, agent and device, state, queue, context. Live from
// the shared list stream; what waits for the user from the pending list.
import { Link } from "react-router";
import { useHubPending, useHubProjects } from "@/app/queries";
import { SessionBadge, activityOf, sessionName } from "@/components/SessionBadge";
import { PageHeader } from "@/components/Shell";
import { useHubList } from "@/features/hub/list";
import { cn } from "@/lib/cn";

export function Agents() {
  const hub = useHubProjects();
  const list = useHubList(hub.status === "on");
  const pending = useHubPending(hub.status === "on");
  const waitingByTask = new Map<number, number>();
  for (const p of pending.data?.pending ?? []) waitingByTask.set(p.task_id, (waitingByTask.get(p.task_id) ?? 0) + 1);
  const open = list.sessions.filter((s) => s.state !== "closed");
  const order = { wait: 0, run: 1, idle: 2, off: 3 } as const;
  const sorted = [...open].sort((a, b) => {
    const tone = (s: typeof a) => order[activityOf(s, s.task != null ? waitingByTask.get(s.task) ?? 0 : 0).tone];
    return tone(a) - tone(b) || b.id - a.id;
  });

  return (
    <>
      <PageHeader>
        <h1 className="text-sm font-medium">Work overview</h1>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-5xl px-4 py-6">
          {hub.status === "off" ? (
            <p className="text-sm text-muted">The hub is not set up on this server.</p>
          ) : hub.status === "down" || !list.reachable ? (
            <p role="alert" className="mb-4 text-sm text-danger">The hub is not answering; what is shown may be old.</p>
          ) : null}
          {hub.status !== "off" && list.ready && sorted.length === 0 && (
            <p className="text-sm text-faint">No open agent sessions.</p>
          )}
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Open sessions">
            {sorted.map((session) => {
              const waits = session.task != null ? waitingByTask.get(session.task) ?? 0 : 0;
              const context = session.context;
              return (
                <li key={session.id}>
                  <Link
                    to={`/s/${session.id}`}
                    className={cn("block rounded-xl border bg-panel p-4 transition-colors hover:border-faint",
                      waits > 0 ? "border-wait/50" : "border-line")}
                  >
                    <div className="text-xs text-faint">{session.project}</div>
                    <div className="mt-0.5 truncate font-medium">{sessionName(session)}</div>
                    <div className="mt-2">
                      <SessionBadge session={session} pending={waits} />
                    </div>
                    <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                      {waits > 0 && <span className="text-wait">{waits} waiting for you</span>}
                      {(session.queued ?? 0) > 0 && <span>{session.queued} queued</span>}
                      {context?.used != null && context.size ? (
                        <span>context {Math.round((context.used / context.size) * 100)}%</span>
                      ) : null}
                      {session.current && <span>current</span>}
                    </div>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </>
  );
}
