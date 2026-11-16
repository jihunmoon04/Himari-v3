import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, PanelRight, PanelRightClose } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { useParams } from "react-router";
import { ApiError } from "@/api/client";
import { cancelJob, resumeTurn, sendMessage } from "@/api/himari";
import { keys, useDetail, useEvents } from "@/app/queries";
import { Composer } from "@/components/Composer";
import { PageHeader } from "@/components/Shell";
import { Timeline } from "@/components/Timeline";
import { Button, IconButton } from "@/components/ui/button";
import { toItems } from "@/features/himari/items";
import { EMPTY_LIVE, reduceLive } from "@/features/himari/live";
import { cn } from "@/lib/cn";
import { openStream } from "@/lib/sse";

function useLive(id: string, running: boolean) {
  const client = useQueryClient();
  const [live, dispatch] = useReducer(reduceLive, EMPTY_LIVE);
  useEffect(() => {
    dispatch({ event: "idle", data: {} });
    if (!running) return;
    let close = () => undefined as void;
    close = openStream(`/api/conversations/${encodeURIComponent(id)}/live`,
      ["snapshot", "delta", "end", "idle", "done"], (frame) => {
        dispatch(frame);
        if (frame.event === "end") void client.invalidateQueries({ queryKey: keys.events(id) });
        if (frame.event === "done") {
          close(); // the job ended: nothing more comes, and the browser would reconnect otherwise
          void client.invalidateQueries({ queryKey: keys.detail(id) });
        }
      });
    return () => close();
  }, [client, id, running]);
  return live;
}

/** Keeps the view at the bottom while new content comes, unless the reader scrolled up. */
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

export function Conversation() {
  const id = useParams().id ?? "";
  const client = useQueryClient();
  const detail = useDetail(id);
  const events = useEvents(id);
  const running = detail.data?.job.running ?? false;
  const live = useLive(id, running);
  const items = useMemo(() => toItems(events.data?.items ?? []), [events.data]);
  const { box, onScroll } = useStickToBottom(`${items.length}:${live.text.length}:${running}`);
  const [panel, setPanel] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => client.invalidateQueries({ queryKey: keys.detail(id) });
  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : "Something went wrong.");
      throw failure;
    } finally {
      await refresh();
    }
  };

  if (detail.error) {
    const missing = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <>
        <PageHeader />
        <p className="m-auto text-sm text-muted">{missing ? "No such conversation." : "Could not open the conversation."}</p>
      </>
    );
  }

  const conversation = detail.data?.conversation;
  const job = detail.data?.job;
  const waiting = detail.data?.waiting ?? null;
  const QUIET = ["completed", "waiting_for_user", "idle", "compacted", "cancelled"];
  const ended = !running && job?.state && !QUIET.includes(job.state) ? job : null;

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
          <h1 className="truncate text-sm font-medium">{conversation?.title || conversation?.preview || "Conversation"}</h1>
        </PageHeader>
        <div ref={box} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {events.isLoading ? <p className="text-sm text-faint">Loading…</p> : <Timeline items={items} live={live} running={running} />}
            {ended && (
              <div className="mt-6 flex items-center gap-2 rounded-xl border border-line px-4 py-3 text-sm text-muted">
                <CircleAlert className="size-4 shrink-0 text-wait" />
                <span className="flex-1">Stopped ({ended.state}){ended.detail ? `: ${ended.detail}` : ""}</span>
                {detail.data?.continuable && (
                  <Button onClick={() => void act(() => resumeTurn(id)).catch(() => undefined)}>Continue</Button>
                )}
              </div>
            )}
          </div>
        </div>
        <div className="mx-auto w-full max-w-3xl px-4 pb-4">
          {error && <p role="alert" className="mb-2 text-sm text-danger">{error}</p>}
          <Composer
            key={id}
            draftKey={`c:${id}`}
            autoFocus
            running={running}
            placeholder={waiting ? "Answer HIMARI" : undefined}
            onStop={() => void act(() => cancelJob(id)).catch(() => undefined)}
            onSend={(text) => act(() => sendMessage(id, text))}
          />
        </div>
      </div>
      <aside
        aria-label="Details"
        className={cn("w-80 shrink-0 border-l border-line bg-panel p-4 text-sm max-md:hidden", !panel && "hidden")}
      >
        <h2 className="mb-3 font-medium">Details</h2>
        <dl className="space-y-2 text-muted">
          <div><dt className="text-xs text-faint">State</dt><dd>{running ? "Working" : waiting ? "Waiting for you" : job?.state ?? "Idle"}</dd></div>
          <div><dt className="text-xs text-faint">Started</dt><dd>{conversation ? new Date(conversation.created_at).toLocaleString() : "—"}</dd></div>
          <div><dt className="text-xs text-faint">Conversation</dt><dd className="break-all font-mono text-xs">{id}</dd></div>
        </dl>
      </aside>
    </div>
  );
}
