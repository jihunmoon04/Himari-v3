// A project request as a card (V3-P103 §6.3), the same everywhere: the new-project dialog, a project's page, the
// approvals, a conversation. What it shows comes from the page's one store (features/hub/requests.ts), so every
// card of the same request agrees. Its actions:
//
// - allow / refuse (HIMARI's request), cancel (while a device has it): after a lost answer the request is read again
//   by its id and can be tried again while it still waits.
// - settle a ``lost`` one (§6.4): the hub asks the device (up to 30 s); when it cannot, "If the device is lost" lets
//   the user say what happened there. After a lost answer of a hub without H17 the button rests SETTLE_REST_MS (the
//   first fence may still run).
// - ask again (§6.3), by kind, always with a new client key: a folder added through the dialog (the projects read
//   first; a ``user`` path picked again), a removal only while the device still reports the folder.
//
// Paths and the pairing code are shown here: this is the operator's page. ``why`` was written by HIMARI: plain text.
import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, FolderPlus, LoaderCircle, ShieldQuestion, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { ApiError } from "@/api/client";
import {
  answerProjectRequest,
  cancelProjectRequest,
  newClientKey,
  readHubProjects,
  resolveProjectRequest,
  type HubProjectRequest,
} from "@/api/hub";
import { hubKeys, useHubProjects } from "@/app/queries";
import { openProjectDialog } from "@/features/hub/dialog";
import { cardView, heldFor, hubHasRequestKeys, runnerOf, secondsLeft } from "@/features/hub/project_view";
import { requests, useHubRequests } from "@/features/hub/requests";
import { answerLost, requestOf, sendKeyed } from "@/features/hub/unconfirmed";
import { cn } from "@/lib/cn";
import { Button, IconButton } from "./ui/button";

export const SETTLE_REST_MS = 60_000; // a fence's 30 s and some (a hub without H17)
const resting = new Map<number, number>(); // request id: until when its settle button rests (shared by its cards)

/** The words of a failure, by what it means for the user. */
export function failureText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Could not do it.";
  if (error.code === "hub_unknown") return "The hub did not answer; it may have done it.";
  if (error.code === "hub_unavailable") return "The hub could not be reached; nothing was sent.";
  if (error.code === "hub_misconfigured") return "The hub refused this server.";
  return error.message;
}

function Countdown({ expires }: { expires: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const left = secondsLeft(expires, now);
  if (left === null) return null;
  return <span className="text-xs text-faint">{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</span>;
}

const LOST_CHOICES = {
  adding: {
    not_applied: "Not registered. If that device comes back and it was in fact registered, its folder becomes a "
      + "project without a default agent (for a new project).",
    applied: "Registered. If the device does not have it, a project without a folder stays (for a new project).",
  },
  removing: {
    applied: "Removed. If the device still has it, the folder is reported as this project's again when it next "
      + "connects.",
    not_applied: "Not removed. If the device has removed it, it leaves the list when it next connects.",
  },
} as const;

function LostDevice({ request, busy, onSettle }: {
  request: HubProjectRequest; busy: boolean; onSettle: (outcome: "applied" | "not_applied") => void;
}) {
  const [outcome, setOutcome] = useState<"applied" | "not_applied" | null>(null);
  const texts = request.kind === "remove_folder" ? LOST_CHOICES.removing : LOST_CHOICES.adding;
  const order = ["applied", "not_applied"] as const;
  return (
    <details className="mt-2 rounded-lg border border-line px-3 py-2 text-xs">
      <summary className="cursor-pointer text-muted">If the device is lost</summary>
      <p className="mt-1 text-faint">Say what happened on it. {request.kind === "remove_folder"
        ? "Nothing changes in the hub's records either way; the device's next report shows how it is."
        : ""}</p>
      <div role="radiogroup" aria-label="What happened on the device" className="mt-2 space-y-1.5">
        {order.map((value) => (
          <label key={value} className="flex items-start gap-2">
            <input type="radio" name={`lost-${request.id}`} className="mt-0.5" checked={outcome === value}
              onChange={() => setOutcome(value)} />
            <span>{texts[value]}</span>
          </label>
        ))}
      </div>
      <Button variant="danger" className="mt-2" disabled={busy || outcome === null}
        onClick={() => outcome && onSettle(outcome)}>
        Settle as this
      </Button>
    </details>
  );
}

/** ``onClose``: the card can be put away (it stops being followed). */
export function ProjectRequestCard({ id, onClose }: { id: number; onClose?: () => void }) {
  const store = useHubRequests();
  const hub = useHubProjects();
  const client = useQueryClient();
  const navigate = useNavigate();
  const entry = store.entries.get(id) ?? { request: null, unknown: false, endedSeen: null };
  const request = entry.request;
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [settleFailed, setSettleFailed] = useState(false);
  const [, setTick] = useState(0);
  const restUntil = resting.get(id) ?? 0;
  useEffect(() => {
    if (restUntil <= Date.now()) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), restUntil - Date.now() + 50);
    return () => window.clearTimeout(timer);
  }, [restUntil]);
  useEffect(() => {
    if (!entry.request && !entry.unknown) void requests.track(id); // a card shows it: follow it
  }, [entry.request, entry.unknown, id]);

  const view = cardView(entry, asking);
  const unsure = store.doubted.has(id);
  const stopped = busy || unsure;
  const keys = hubHasRequestKeys(hub.data);
  const rests = restUntil > Date.now();

  /** The request as the hub has it now; until that read goes through no card of it can be pressed (the store keeps
   *  it in doubt, for every card and over leaving the page). */
  const readAgain = async () => {
    setBusy(true);
    await requests.readIds([id]);
    setBusy(false);
  };

  const act = async (run: () => Promise<{ request: HubProjectRequest }>, lostAnswer?: () => void) => {
    setBusy(true);
    setProblem(null);
    try {
      requests.merge(requestOf(await run()));
      setBusy(false);
      return true;
    } catch (error) {
      if (answerLost(error)) { // it may have been done: read it first, then try again only if it still waits
        setProblem("The hub did not answer; it is read again. Try again if it still waits.");
        lostAnswer?.();
        requests.doubt(id);
        await readAgain();
      } else {
        setProblem(failureText(error));
        void requests.readIds([id]); // as the hub has it now
        setBusy(false);
      }
      return false;
    }
  };

  const settle = async (outcome?: "applied" | "not_applied") => {
    if (!outcome) setAsking(true);
    const done = await act(() => resolveProjectRequest(id, outcome ? { outcome } : undefined), () => {
      if (!keys) resting.set(id, Date.now() + SETTLE_REST_MS); // without H17 the first fence may still run
    });
    setAsking(false);
    if (!done && !outcome) setSettleFailed(true);
  };

  const rereadProjects = () => {
    void client.invalidateQueries({ queryKey: hubKeys.projects });
    void requests.refresh();
  };

  const askAgain = async () => {
    if (!request) return;
    setProblem(null);
    if (request.kind !== "remove_folder") {
      openProjectDialog({
        kind: request.kind === "create" ? "create" : "add_folder", project: request.project, runner: request.runner,
        path: request.result_path ?? request.path ?? null, createDir: request.create_dir, agent: request.agent,
        again: true,
      });
      return;
    }
    setBusy(true);
    try { // a removal: only while the device still reports this project's folder
      const now = await client.fetchQuery({ queryKey: hubKeys.projects, queryFn: readHubProjects, staleTime: 0 });
      const runner = runnerOf(now, request.runner);
      if (!runner || runner.connected === false) {
        setProblem("The device is not connected.");
      } else if (!request.project || runner.folders?.[request.project] === undefined) {
        setProblem("It is already removed.");
      } else {
        const key = await newClientKey();
        const sent = await sendKeyed({ ...key, route: "remove", at: Date.now(),
          body: { project: request.project, runner: runner.name ?? request.runner ?? "" } }, false);
        if ("error" in sent) setProblem(failureText(sent.error));
        else onClose?.();
      }
    } catch (error) {
      setProblem(failureText(error));
    } finally {
      setBusy(false);
    }
  };

  const held = request?.kind === "create" && request.project && request.state !== "done"
    ? heldFor(hub.data, request.project).filter((h) => h.runner !== request.runner) : [];
  const Icon = view.tone === "good" ? CircleCheck : view.tone === "bad" ? CircleAlert
    : view.tone === "run" ? LoaderCircle : view.tone === "wait" ? ShieldQuestion : FolderPlus;

  return (
    <section role="group" aria-label={`Project request ${id}`} data-state={request?.state ?? "unknown"}
      className={cn("rounded-xl border px-4 py-3 text-sm", view.tone === "wait" ? "border-wait/40 bg-wait/5"
        : view.tone === "bad" ? "border-danger/40" : "border-line")}>
      <div className="flex items-start gap-2">
        <Icon className={cn("mt-0.5 size-4 shrink-0", view.tone === "run" && "animate-spin",
          view.tone === "good" && "text-ok", view.tone === "bad" && "text-danger", view.tone === "wait" && "text-wait")}
          aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium">{view.headline}</p>
          {request?.origin === "model" && request.state !== "waiting_user" && (
            <p className="text-xs text-faint">HIMARI asked for it</p>
          )}
          {view.lines.map((line, n) => (
            <p key={n} className={cn("whitespace-pre-wrap break-words", n === 0 ? "text-xs text-muted" : "text-muted")}>
              {line}
            </p>
          ))}
          {view.code && (
            <p className="mt-2 flex items-baseline gap-3">
              <span aria-label="Pairing code" className="font-mono text-2xl font-semibold tracking-widest">{view.code}</span>
              <Countdown expires={view.expires} />
            </p>
          )}
          {held.length > 0 && (
            <p className="mt-1 text-xs text-wait">
              The same name is held on {held.map((h) => `${h.runner} (${h.connected ? "connected" : "not connected"})`)
                .join(", ")}.
            </p>
          )}
        </div>
        {onClose && (
          <IconButton label="Put the card away" onClick={onClose} className="-mr-2 -mt-1 size-7">
            <X className="size-3.5" />
          </IconButton>
        )}
      </div>
      {view.actions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {view.actions.includes("approve") && (
            <Button variant="primary" disabled={stopped} onClick={() => void act(() => answerProjectRequest(id, true))}>
              Allow
            </Button>
          )}
          {view.actions.includes("refuse") && (
            <Button disabled={stopped} onClick={() => void act(() => answerProjectRequest(id, false))}>Refuse</Button>
          )}
          {view.actions.includes("cancel") && (
            <Button variant="ghost" disabled={stopped} onClick={() => void act(() => cancelProjectRequest(id))}>Cancel</Button>
          )}
          {view.actions.includes("resolve") && (
            <Button variant="primary" disabled={stopped || rests} onClick={() => void settle()}>
              {rests ? "Wait a minute" : "Settle"}
            </Button>
          )}
          {view.actions.includes("open_project") && request?.project && (
            <Button variant="primary" onClick={() => navigate(`/project?name=${encodeURIComponent(request.project!)}`)}>
              Open project
            </Button>
          )}
          {view.actions.includes("retry") && (
            <Button disabled={busy} onClick={() => void askAgain()}>Ask again</Button>
          )}
          {view.actions.includes("reread") && <Button onClick={rereadProjects}>Read the projects again</Button>}
        </div>
      )}
      {unsure && (
        <div className="mt-2 flex items-center gap-2 text-xs text-danger">
          <span className="flex-1">The request could not be read again: whether it was done is not known yet.</span>
          <Button className="text-xs" disabled={busy} onClick={() => void readAgain()}>Read it again</Button>
        </div>
      )}
      {request && view.actions.includes("resolve") && settleFailed && !rests && (
        <LostDevice request={request} busy={stopped} onSettle={(outcome) => void settle(outcome)} />
      )}
      {problem && <p role="alert" className="mt-2 text-xs text-danger">{problem}</p>}
    </section>
  );
}
