// One of the hub's agent sessions (V3-P102 §4.2-§4.4, W2-W3): its events live, the earlier ones on request, the
// latest plan pinned, its details on the right; sending to it with a model, effort and mode for that message, what
// waits its turn (each can be cancelled), stopping the running turn, answering what the agent asks, closing it.
// What is sent is followed by its task (§5.2): when the agent had lost this session the page moves to the one the
// message really went on in.
import { useQueryClient } from "@tanstack/react-query";
import { ChevronUp, PanelRight, PanelRightClose, Radio, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "@/api/client";
import {
  cancelHubTask, closeHubSession, sendToHubSession, type HubSession as Session, type MessageChoices,
} from "@/api/hub";
import { hubKeys, useHubPending, useHubProjects, useHubSessionDetail } from "@/app/queries";
import { Composer } from "@/components/Composer";
import { HubRequest } from "@/components/HubRequest";
import { MessageOptions, chosen } from "@/components/MessageOptions";
import { SessionBadge, sessionName } from "@/components/SessionBadge";
import { PageHeader } from "@/components/Shell";
import { PlanList, Timeline } from "@/components/Timeline";
import { Button, IconButton } from "@/components/ui/button";
import { dismiss, failedBeforeRunning, follow, runsIn, useFollowed } from "@/features/hub/follow";
import { toHubItems } from "@/features/hub/items";
import { useHubList } from "@/features/hub/list";
import { useHubSessionEvents } from "@/features/hub/session";
import { cn } from "@/lib/cn";

// tasks whose move to another session this page has already followed (it moves once, not on every render)
const movedAlready = new Set<number>();

/** Why a send, a stop or a close did not go through, in words. The answer lost on the way (``hub_unknown`` from the
 *  hub, or the connection to this server broke after the request left: ``offline``) means it may have been done:
 *  nothing is retried, and the user is told to check first (the hub keeps no key to drop a duplicate). */
export function sendProblem(failure: unknown): string {
  if (failure instanceof ApiError) {
    if (failure.code === "hub_unknown" || failure.code === "offline") {
      return "The answer was lost on the way: it may have been done. Check the session before trying again.";
    }
    if (failure.code === "hub_refused") return `The hub refused it: ${failure.message}`;
    return failure.message;
  }
  return "Could not send.";
}

function Notice({ children, onClose, tone = "muted" }: {
  children: React.ReactNode;
  onClose?: () => void;
  tone?: "muted" | "danger";
}) {
  return (
    <div role={tone === "danger" ? "alert" : "status"}
      className={cn("mb-2 flex items-start gap-2 rounded-xl border px-3 py-2 text-sm",
        tone === "danger" ? "border-danger/40 text-danger" : "border-line text-muted")}>
      <div className="min-w-0 flex-1 break-words">{children}</div>
      {onClose && (
        <button type="button" aria-label="Dismiss" onClick={onClose} className="shrink-0 text-faint hover:text-ink">
          <X className="size-4" />
        </button>
      )}
    </div>
  );
}

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
  return <SessionPage key={id} id={id} />; // each session starts with its own choices and notices
}

function SessionPage({ id }: { id: number }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const hub = useHubProjects();
  const list = useHubList(hub.status === "on");
  const detail = useHubSessionDetail(id);
  const pending = useHubPending(hub.status === "on");
  const [streamed, setStreamed] = useState<Session | null>(null); // the session stream's latest listing of it
  const onState = useCallback((shown: Session | null) => {
    if (shown) setStreamed(shown);
    void client.invalidateQueries({ queryKey: hubKeys.session(id) });
    void client.invalidateQueries({ queryKey: hubKeys.pending });
  }, [client, id]);
  const stream = useHubSessionEvents(id, onState);
  const { items, stats } = useMemo(() => toHubItems(stream.events), [stream.events]);
  const [panel, setPanel] = useState(false);
  const { box, onScroll } = useStickToBottom(stream.events.length);

  const listed = list.sessions.find((s) => s.id === id);
  // the freshest listing of this session: its own live stream's, else the shared list's while that one is live,
  // else its detail (read again on every signal, and every 10 s without a live stream)
  const fromDetail = detail.data ? { ...detail.data, queued: detail.data.queued.length } : undefined;
  const session: Session | undefined = (stream.live && streamed) || (list.reachable ? listed : undefined)
    || fromDetail || listed;
  const waiting = useMemo(
    () => (pending.data?.pending ?? []).filter((p) => session?.task != null && p.task_id === session.task),
    [pending.data, session?.task],
  );
  const running = session?.activity === "running" || session?.activity === "waiting";
  const closed = session?.state === "closed";
  const lastIsTurnEnd = items.at(-1)?.kind === "turn";
  // what waits its turn is in the session's detail; the listing's count says when it changed (another browser, a
  // turn that took the next one), and without a live stream the detail is read again now and then
  useEffect(() => {
    if (listed && detail.data && (listed.queued ?? 0) !== detail.data.queued.length) {
      void client.invalidateQueries({ queryKey: hubKeys.session(id) });
    }
  }, [client, id, listed?.queued, detail.data?.queued.length]);
  useEffect(() => {
    if (stream.live) return;
    const timer = window.setInterval(() => void client.invalidateQueries({ queryKey: hubKeys.session(id) }), 10_000);
    return () => window.clearInterval(timer);
  }, [client, id, stream.live]);
  const [choices, setChoices] = useState<MessageChoices>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);

  // what this page sent, followed to the session it really runs in (§5.2)
  const followed = useFollowed();
  useEffect(() => {
    for (const f of followed) {
      const where = runsIn(f);
      if (f.from !== id || where === null) continue;
      if (where !== id && !movedAlready.has(f.task.id)) {
        movedAlready.add(f.task.id); // the agent had lost this session: follow the message to where it went on
        navigate(`/s/${where}`);
        return;
      }
      if (where === id && f.done) dismiss(f.task.id); // it ran here: the timeline shows it
    }
  }, [followed, id, navigate]);
  const movedHere = followed.filter((f) => runsIn(f) === id && f.from !== null && f.from !== id);
  const notSent = followed.filter((f) => f.from === id && failedBeforeRunning(f));

  // requests whose event is not (yet) in the timeline still get a card above the composer
  const placed = new Set(items.flatMap((i) => (i.kind === "request" ? [i.interaction] : [])));
  const unplaced = waiting.filter((p) => !placed.has(String(p.id)));
  const renderRequest = useCallback((item: { interaction: string }) => {
    const asked = waiting.find((p) => String(p.id) === item.interaction);
    return asked ? <HubRequest item={asked} /> : null;
  }, [waiting]);

  const refresh = () => {
    void client.invalidateQueries({ queryKey: hubKeys.session(id) });
    void client.invalidateQueries({ queryKey: hubKeys.pending });
  };
  const send = async (text: string) => {
    setProblem(null);
    try {
      const { task } = await sendToHubSession(id, text, chosen(choices));
      follow(task, id, session?.project ?? null);
      setChoices((now) => ({ ...now, mode: undefined })); // a mode is for that one message
      refresh();
    } catch (failure) {
      setProblem(sendProblem(failure));
      throw failure; // the draft stays
    }
  };
  const cancel = async (task: number | null | undefined) => {
    if (task == null) return;
    setProblem(null);
    try {
      await cancelHubTask(task);
    } catch (failure) {
      setProblem(sendProblem(failure));
    } finally {
      refresh();
    }
  };
  const close = async () => {
    if (!window.confirm("Close this session? Its record stays; what waits for it is cancelled.")) return;
    setClosing(true);
    setProblem(null);
    try {
      await closeHubSession(id);
    } catch (failure) {
      setProblem(sendProblem(failure));
    } finally {
      setClosing(false);
      refresh();
    }
  };

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
              <Timeline items={items} running={running && !lastIsTurnEnd} working="The agent is working…"
                renderRequest={renderRequest} />
            )}
          </div>
        </div>
        <div className="mx-auto w-full max-w-3xl px-4 pb-4">
          {running && stats.plan && stats.plan.length > 0 && (
            <div className="mb-2 rounded-xl border border-line bg-panel px-4 py-3">
              <PlanList entries={stats.plan} />
            </div>
          )}
          {movedHere.map((f) => (
            <Notice key={f.task.id} onClose={() => dismiss(f.task.id)}>
              The agent had lost <Link to={`/s/${f.from}`} className="underline">session #{f.from}</Link>; your
              message went on in this new one.
            </Notice>
          ))}
          {notSent.map((f) => (
            <Notice key={f.task.id} tone="danger" onClose={() => dismiss(f.task.id)}>
              Not run: {f.task.prompt} ({f.lost ?? f.task.result ?? "the task failed before it started"})
            </Notice>
          ))}
          {unplaced.map((p) => <div key={p.id} className="mb-2"><HubRequest item={p} /></div>)}
          {detail.data && detail.data.queued.length > 0 && (
            <div className="mb-2 rounded-xl border border-line px-3 py-2" aria-label="Queued messages">
              <div className="mb-1 text-xs font-medium text-faint">
                Waiting for the running turn ({detail.data.queued.length})
              </div>
              <ul className="space-y-1">
                {detail.data.queued.map((q) => (
                  <li key={q.id} className="flex items-center gap-2 text-sm">
                    <span className="min-w-0 flex-1 truncate">{q.prompt}</span>
                    {q.id != null && (
                      <Button variant="ghost" className="px-2 py-0.5 text-xs" onClick={() => void cancel(q.id)}>
                        Cancel
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {problem && <Notice tone="danger" onClose={() => setProblem(null)}>{problem}</Notice>}
          {closed ? (
            <p className="rounded-xl border border-dashed border-line px-4 py-3 text-sm text-faint">
              This session is closed{session?.closed_reason ? ` (${session.closed_reason})` : ""}. Start a new one
              from New task.
            </p>
          ) : (
            <Composer
              draftKey={`hub-session-${id}`}
              placeholder={session ? `Message ${sessionName(session)}` : "Message the agent"}
              onSend={send}
              onStop={() => void cancel(session?.task)}
              running={running}
              queues
              disabled={!session}
              tools={
                <>
                  <MessageOptions projects={hub.data} agent={session?.agent} value={choices} onChange={setChoices} />
                  <span className="inline-flex items-center gap-1 text-xs text-faint"
                    title={stream.live ? "Live" : "Reading by asking the hub"}>
                    <Radio className={cn("size-3.5", stream.live ? "text-ok" : "text-faint")} aria-hidden />
                    {stream.live ? "Live" : "Polling"}
                  </span>
                </>
              }
            />
          )}
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
            {session.updated_at && <Fact term="Last activity">{new Date(session.updated_at).toLocaleString()}</Fact>}
          </dl>
        )}
        {session && !closed && (
          <Button variant="outline" className="mt-4 w-full" disabled={running || closing} onClick={() => void close()}
            title={running ? "Stop the running turn first" : undefined}>
            Close session
          </Button>
        )}
      </aside>
    </div>
  );
}
