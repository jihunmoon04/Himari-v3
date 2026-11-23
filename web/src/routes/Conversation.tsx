import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, PanelRight, PanelRightClose } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { useParams } from "react-router";
import { ApiError } from "@/api/client";
import { cancelJob, resumeTurn } from "@/api/himari";
import { keys, useDetail, useEvents } from "@/app/queries";
import { FilesPanel } from "@/components/FilesPanel";
import { SidePanel } from "@/components/SidePanel";
import { ConversationMenu } from "@/components/ConversationMenu";
import { JumpToLatest } from "@/components/JumpToLatest";
import { HimariComposer } from "@/components/HimariComposer";
import { PageHeader } from "@/components/Shell";
import { Timeline } from "@/components/Timeline";
import { Button, IconButton } from "@/components/ui/button";
import { toItems } from "@/features/himari/items";
import { EMPTY_LIVE, reduceLive } from "@/features/himari/live";
import { openStream } from "@/lib/sse";

/** The live output of the running job ``job`` (null: none runs). One stream per job: a job that starts right
 *  after another ended gets its own, and a frame of another job is not shown. */
function useLive(id: string, job: string | null) {
  const client = useQueryClient();
  const [live, dispatch] = useReducer(reduceLive, EMPTY_LIVE);
  useEffect(() => {
    dispatch({ event: "idle", data: {} });
    if (job === null) return;
    let close = () => undefined as void;
    close = openStream(`/api/conversations/${encodeURIComponent(id)}/live`,
      ["snapshot", "delta", "end", "idle", "done"], (frame) => {
        const of = frame.data.job_id;
        if (typeof of === "string" && job !== "" && of !== job) return; // another job's
        dispatch(frame);
        if (frame.event === "end") void client.invalidateQueries({ queryKey: keys.events(id) });
        if (frame.event === "done") {
          close(); // this job ended: nothing more comes, and the browser would reconnect otherwise
          void client.invalidateQueries({ queryKey: keys.detail(id) });
        }
      });
    return () => close();
  }, [client, id, job]);
  return live;
}

import { useStickToBottom } from "@/lib/stick";

export function Conversation() {
  const id = useParams().id ?? "";
  const client = useQueryClient();
  const detail = useDetail(id);
  const events = useEvents(id);
  const running = detail.data?.job.running ?? false;
  const live = useLive(id, running ? (detail.data?.job.job_id ?? "") : null);
  const items = useMemo(() => toItems(events.data?.items ?? [], id), [events.data, id]);
  const { box, onScroll, atBottom, jump } = useStickToBottom(`${items.length}:${live.text.length}:${running}`);
  const [panel, setPanel] = useState(false);
  const closePanel = useCallback(() => setPanel(false), []);
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
  const QUIET = ["completed", "idle", "compacted", "cancelled"];
  // why the last job stopped, when it did not end well; Continue whenever the server says a turn can go on
  // (also after a restart or a stop, when there is no such job to show)
  const stopped = !running && job ? job.error ?? (job.state && !QUIET.includes(job.state)
    ? `Stopped (${job.state})${job.detail ? `: ${job.detail}` : ""}` : null) : null;
  const continuable = !running && Boolean(detail.data?.continuable);

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <PageHeader
          actions={
            <>
              <ConversationMenu key={id} id={id} title={conversation?.title || conversation?.preview || ""}
                unfinished={continuable} running={running} />
              <IconButton label={panel ? "Close details" : "Details"} onClick={() => setPanel(!panel)}>
                {panel ? <PanelRightClose className="size-4" /> : <PanelRight className="size-4" />}
              </IconButton>
            </>
          }
        >
          <h1 className="truncate text-sm font-medium">{conversation?.title || conversation?.preview || "Conversation"}</h1>
        </PageHeader>
        <div ref={box} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-4 py-6">
            {events.isLoading ? <p className="text-sm text-faint">Loading…</p> : <Timeline items={items} live={live} running={running} conversation={id} />}
            {(stopped || continuable) && (
              <div className="mt-6 flex items-center gap-2 rounded-xl border border-line px-4 py-3 text-sm text-muted">
                <CircleAlert className="size-4 shrink-0 text-wait" />
                <span className="flex-1">{stopped ?? "This turn did not finish."}</span>
                {continuable && (
                  <Button onClick={() => void act(() => resumeTurn(id)).catch(() => undefined)}>Continue</Button>
                )}
              </div>
            )}
          </div>
        </div>
        <div className="relative mx-auto w-full max-w-3xl px-4 pb-4">
          {!atBottom && <JumpToLatest onClick={jump} />}
          {error && <p role="alert" className="mb-2 text-sm text-danger">{error}</p>}
          <HimariComposer
            key={id}
            conversation={id}
            running={running}
            onStop={() => void act(() => cancelJob(id)).catch(() => undefined)}
            onSent={refresh}
            onError={setError}
          />
        </div>
      </div>
      <SidePanel
        open={panel}
        onClose={closePanel}
        label="Details"
        storageKey="conversation"
        tabs={[
          { id: "files", label: "Files", content: <FilesPanel key={id} conversation={id} /> },
          {
            id: "info", label: "Info", content: (
              <dl className="space-y-2 text-muted">
                <div><dt className="text-xs text-faint">State</dt><dd>{running ? "Working" : job?.state ?? "Idle"}</dd></div>
                <div><dt className="text-xs text-faint">Started</dt><dd>{conversation ? new Date(conversation.created_at).toLocaleString() : "—"}</dd></div>
                <div><dt className="text-xs text-faint">Conversation</dt><dd className="break-all font-mono text-xs">{id}</dd></div>
              </dl>
            ),
          },
        ]}
      />
    </div>
  );
}
