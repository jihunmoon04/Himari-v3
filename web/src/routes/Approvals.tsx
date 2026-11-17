// Every request the hub's agents wait on the user for, oldest first (V3-P102 §4.6), answered here or opened in
// its session. The list is the pending list itself: read again after every answer and every 30 s (§7.3).
import { useHubPending, useHubProjects } from "@/app/queries";
import { HubRequest } from "@/components/HubRequest";
import { PageHeader } from "@/components/Shell";
import { useHubList } from "@/features/hub/list";

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
