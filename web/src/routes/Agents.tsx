// Every open hub session at a glance (V3-P102 §4.5): project, agent and device, state, queue, context. Live from
// the shared list stream; what waits for the user from the pending list. Below, the latest closed ones (H2), to open
// one again.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { listClosedHubSessions } from "@/api/hub";
import { hubSetUp, useHubPending, useHubProjects } from "@/app/queries";
import { SessionBadge, activityOf, sessionLane, sessionName } from "@/components/SessionBadge";
import { PageHeader } from "@/components/Shell";
import { useHubList } from "@/features/hub/list";
import { cn } from "@/lib/cn";

export function Agents() {
  const hub = useHubProjects();
  const list = useHubList(hubSetUp(hub.status));
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
          {hub.status === "on" && <ClosedSessions changed={list.sessions.map((s) => `${s.id}:${s.state}:${s.title ?? ""}`).join()} />}
        </div>
      </div>
    </>
  );
}

/** H2: the latest closed sessions, read when the list is opened and again when a session closes, opens or is
 *  renamed (``changed``: what the list stream says of each session). */
function ClosedSessions({ changed }: { changed: string }) {
  const [open, setOpen] = useState(false);
  const closed = useQuery({
    queryKey: ["hub", "closed", changed],
    queryFn: () => listClosedHubSessions(50),
    enabled: open,
    placeholderData: (previous) => previous,
  });
  return (
    <details className="mt-8" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-sm text-muted">Closed sessions</summary>
      {closed.isLoading ? (
        <p className="mt-2 text-sm text-faint">Loading…</p>
      ) : closed.error ? (
        <p role="alert" className="mt-2 text-sm text-danger">Could not read the closed sessions.</p>
      ) : (closed.data?.sessions.length ?? 0) === 0 ? (
        <p className="mt-2 text-sm text-faint">No closed sessions.</p>
      ) : (
        <ul className="mt-2 divide-y divide-line rounded-xl border border-line" aria-label="Closed sessions">
          {closed.data?.sessions.map((session) => (
            <li key={session.id}>
              <Link to={`/s/${session.id}`} className="flex items-baseline gap-3 px-4 py-2 text-sm hover:bg-raised">
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-faint">{session.project} › </span>{sessionName(session)}
                  {session.title && <span className="text-faint"> · {sessionLane(session)}</span>}
                </span>
                <span className="shrink-0 text-xs text-faint">
                  {session.closed_reason ?? "closed"}
                  {session.closed_at ? ` · ${new Date(session.closed_at).toLocaleString()}` : ""}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
