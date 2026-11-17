// Every request the hub's agents wait on the user for, oldest first (V3-P102 §4.6), answered here or opened in
// its session. The list is the pending list itself: read again after every answer and every 30 s (§7.3).
//
// Above it (V3-P103 §6.7): HIMARI's project requests waiting to be allowed, and requests lost on a device to settle,
// with the requests of this tab whose answer was lost.
import { useEffect, useState } from "react";
import { useHubPending, useHubProjects } from "@/app/queries";
import { HubRequest } from "@/components/HubRequest";
import { ProjectRequestCard } from "@/components/ProjectRequestCard";
import { UnconfirmedRequests } from "@/components/UnconfirmedRequests";
import { PageHeader } from "@/components/Shell";
import { useHubList } from "@/features/hub/list";
import { openRequests, requests, useHubRequests } from "@/features/hub/requests";

function ProjectRequests() {
  const store = useHubRequests();
  const waiting = openRequests(store).filter((r) => r.state === "waiting_user" || r.state === "lost").map((r) => r.id);
  // a card shown on this visit stays (with how it ended) while the store keeps its record
  const [seen, setSeen] = useState<number[]>([]);
  useEffect(() => {
    setSeen((before) => (waiting.every((id) => before.includes(id)) ? before : [...new Set([...before, ...waiting])]));
  }, [waiting]);
  // and the requests this tab follows (kept over a reload, §5): one that ended meanwhile shows how, until put away
  const followed = store.tracked.filter((id) => !waiting.includes(id));
  const shown = [...new Set([...seen, ...waiting, ...followed])].filter((id) => store.entries.has(id))
    .sort((x, y) => x - y);
  return (
    <section aria-label="Project requests" className="space-y-2">
      <h2 className="text-xs font-medium text-faint">Project requests</h2>
      <UnconfirmedRequests />
      {waiting.length === 0 && <p className="text-sm text-faint">No project request waits for you.</p>}
      {shown.map((id) => (
        <ProjectRequestCard key={id} id={id} onClose={followed.includes(id) ? () => requests.untrack(id) : undefined} />
      ))}
    </section>
  );
}

export function Approvals() {
  const hub = useHubProjects();
  const list = useHubList(hub.status === "on");
  const pending = useHubPending(hub.status === "on");
  const items = [...(pending.data?.pending ?? [])].sort((a, b) => a.id - b.id);
  const sessionOf = (task: number) => list.sessions.find((s) => s.task === task)?.id ?? null;

  return (
    <>
      <PageHeader>
        <h1 className="text-sm font-medium">Approvals</h1>
      </PageHeader>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl space-y-3 px-4 py-6">
          {hub.status === "off" && <p className="text-sm text-muted">The hub is not set up on this server.</p>}
          {pending.data && !pending.data.reachable && hub.status !== "off" && (
            <p role="alert" className="text-sm text-danger">The hub is not answering; what is shown may be old.</p>
          )}
          {hub.status === "on" && <ProjectRequests />}
          {hub.status === "on" && <h2 className="pt-3 text-xs font-medium text-faint">Agents</h2>}
          {pending.data?.reachable && items.length === 0 && (
            <p className="text-sm text-faint">Nothing waits for you.</p>
          )}
          {items.map((item) => (
            <HubRequest key={item.id} item={item} where session={sessionOf(item.task_id)} />
          ))}
        </div>
      </div>
    </>
  );
}
