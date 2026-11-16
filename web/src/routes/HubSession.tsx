// One of the hub's agent sessions, read only for now (V3-P102 §4.2, W2): its events live, the earlier ones on
// request, the latest plan pinned, and its details on the right. Sending to it comes with W3.
import { useQueryClient } from "@tanstack/react-query";
import { ChevronUp, PanelRight, PanelRightClose, Radio } from "lucide-react";
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router";
import { ApiError } from "@/api/client";
import type { HubSession as Session } from "@/api/hub";
import { hubKeys, useHubPending, useHubProjects, useHubSessionDetail } from "@/app/queries";
import { SessionBadge, sessionName } from "@/components/SessionBadge";
import { PageHeader } from "@/components/Shell";
import { PlanList, Timeline } from "@/components/Timeline";
import { Button, IconButton } from "@/components/ui/button";
import { toHubItems } from "@/features/hub/items";
import { useHubList } from "@/features/hub/list";
import { useHubSessionEvents } from "@/features/hub/session";
import { cn } from "@/lib/cn";

function useStickToBottom(dependency: unknown) {
  const box = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  useLayoutEffect(() => {
    const node = box.current;
    if (node && stuck.current) node.scrollTop = node.scrollHeight;
  }, [dependency]);
  const onScroll = () => {
    const node = box.current;
    if (node) stuck.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  };
  return { box, onScroll };
}

function Fact({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-faint">{term}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}

export function HubSession() {
  const id = Number(useParams().id);
  const client = useQueryClient();
  const hub = useHubProjects();
  const list = useHubList(hub.status === "on");
  const detail = useHubSessionDetail(id);
  const pending = useHubPending(hub.status === "on");
  const onState = useCallback(() => {
    void client.invalidateQueries({ queryKey: hubKeys.session(id) });
    void client.invalidateQueries({ queryKey: hubKeys.pending });
  }, [client, id]);
  const stream = useHubSessionEvents(id, onState);
  const { items, stats } = useMemo(() => toHubItems(stream.events), [stream.events]);
  const [panel, setPanel] = useState(false);
  const { box, onScroll } = useStickToBottom(stream.events.length);

  const listed = list.sessions.find((s) => s.id === id);
  const session: Session | undefined = listed ?? (detail.data ? { ...detail.data, queued: detail.data.queued.length } : undefined);
  const waiting = (pending.data?.pending ?? []).filter((p) => session?.task != null && p.task_id === session.task);
  const running = session?.activity === "running" || session?.activity === "waiting";
  const lastIsTurnEnd = items.at(-1)?.kind === "turn";

  if (!Number.isInteger(id) || id < 1 || (detail.error instanceof ApiError && detail.error.status === 404)) {
    return (
      <>
        <PageHeader />
        <p className="m-auto text-sm text-muted">No such session.</p>
      </>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <PageHeader
          actions={
            <IconButton label={panel ? "Close details" : "Details"} onClick={() => setPanel(!panel)} className="max-md:hidden">
              {panel ? <PanelRightClose className="size-4" /> : <PanelRight className="size-4" />}
            </IconButton>
          }
        >
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="truncate text-sm font-medium">
              {session ? (
                <>
                  <span className="text-muted">{session.project} › </span>
                  {sessionName(session)}
                </>
              ) : (
                "Session"
              )}
            </h1>
            {session && <SessionBadge session={session} pending={waiting.length} />}
          </div>
        </PageHeader>
        <div ref={box} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {stream.earlier && (
              <div className="mb-6 flex flex-col items-center gap-1">
                <Button variant="ghost" onClick={stream.loadEarlier} disabled={stream.loadingEarlier}>
                  <ChevronUp className="size-4" /> {stream.loadingEarlier ? "Loading…" : "Earlier"}
                </Button>
                {stream.earlierError && <p role="alert" className="text-xs text-danger">{stream.earlierError}</p>}
              </div>
            )}
            {stream.error && (
              <p role="alert" className="mb-4 text-sm text-danger">
                {stream.error}{stream.error !== "No such session." && " Trying again…"}
              </p>
            )}
            {stream.loading ? (
              <p className="text-sm text-faint">Loading…</p>
            ) : stream.error && items.length === 0 ? null : items.length === 0 ? (
              <p className="text-sm text-faint">Nothing in this session yet.</p>
            ) : (
              <Timeline items={items} running={running && !lastIsTurnEnd} working="The agent is working…" />
            )}
          </div>
        </div>
        <div className="mx-auto w-full max-w-3xl px-4 pb-4">
          {running && stats.plan && stats.plan.length > 0 && (
            <div className="mb-2 rounded-xl border border-line bg-panel px-4 py-3">
              <PlanList entries={stats.plan} />
            </div>
          )}
          <p className="flex items-center gap-2 rounded-xl border border-dashed border-line px-4 py-3 text-sm text-faint">
            <Radio className={cn("size-4", stream.live ? "text-ok" : "text-faint")} aria-hidden />
            {stream.live ? "Live." : "Reading by asking the hub."} Sending to this session from here comes next.
          </p>
        </div>
      </div>
      <aside
        aria-label="Details"
        className={cn("w-80 shrink-0 overflow-y-auto border-l border-line bg-panel p-4 text-sm max-md:hidden", !panel && "hidden")}
      >
        <h2 className="mb-3 font-medium">Details</h2>
        {session && (
          <dl className="space-y-3 text-muted">
            <Fact term="Project">{session.project}</Fact>
            <Fact term="Agent">{session.agent}{session.profile ? ` · ${session.profile}` : ""}</Fact>
            <Fact term="Device">
              {session.runner} {session.runner_connected === false ? "(not connected)" : ""}
            </Fact>
            <Fact term="Model">{[session.model, session.effort].filter(Boolean).join(" · ") || "Default"}</Fact>
            {(stats.context ?? session.context) && (() => {
              const context = stats.context ?? session.context!;
              const used = context.used ?? 0;
              const size = context.size ?? 0;
              return (
                <Fact term="Context">
                  {used.toLocaleString()} / {size.toLocaleString()} tokens
                  {size > 0 && (
                    <span className="mt-1 block h-1.5 overflow-hidden rounded bg-raised">
                      <span ref={(node) => { if (node) node.style.width = `${Math.min(100, (used / size) * 100)}%`; }}
                        className="block h-full bg-accent" />
                    </span>
                  )}
                </Fact>
              );
            })()}
            {detail.data && detail.data.queued.length > 0 && (
              <Fact term="Queued">
                <ul className="list-disc pl-4">{detail.data.queued.map((q) => <li key={q.id}>{q.prompt}</li>)}</ul>
              </Fact>
            )}
            {detail.data && detail.data.attempts.length > 0 && (
              <Fact term="Recent attempts">
                <ul className="space-y-1">
                  {detail.data.attempts.slice(0, 5).map((a) => (
                    <li key={a.id} className="text-xs">
                      #{a.task_id} {a.runner} · {a.outcome ?? "running"}
                    </li>
                  ))}
                </ul>
              </Fact>
            )}
            <Fact term="Session">#{session.id}{session.current ? " · current of its lane" : ""}</Fact>
          </dl>
        )}
      </aside>
    </div>
  );
}
