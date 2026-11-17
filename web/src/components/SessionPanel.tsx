// A hub session's right-hand panel (V3-P102 §4.4): what it changed (by file, one turn or all, as one column or side
// by side), what it planned (the latest plan and every earlier one), and what it is (agent, device, model, context,
// queue, attempts, closing it). Every text here came from an agent: shown as text only.
import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ApiError } from "@/api/client";
import { renameHubSession, type HubEvent, type HubSession, type HubSessionDetail } from "@/api/hub";
import { hubKeys } from "@/app/queries";
import { byFile, changesByTurn, planHistory } from "@/features/hub/changes";
import { cn } from "@/lib/cn";
import { DiffView } from "./DiffView";
import { PlanList } from "./Timeline";
import { Button } from "./ui/button";
import type { PanelTab } from "./SidePanel";

const SPLIT_KEY = "himari.next.diff.split";

function loadSplit(): boolean {
  try {
    return window.localStorage.getItem(SPLIT_KEY) === "1";
  } catch {
    return false;
  }
}

function Changes({ events, earlier }: { events: HubEvent[]; earlier: boolean }) {
  const turns = useMemo(() => changesByTurn(events), [events]);
  const [turn, setTurn] = useState<number | "all">("all");
  const [split, setSplit] = useState(loadSplit);
  const chosen = turn === "all" ? turns : turns.filter((t) => t.id === turn);
  const files = useMemo(() => byFile(chosen), [chosen]);
  const flip = (value: boolean) => {
    setSplit(value);
    try {
      window.localStorage.setItem(SPLIT_KEY, value ? "1" : "0");
    } catch {
      // kept for this page only
    }
  };
  if (!turns.length) {
    return <p className="text-faint">No file changes{earlier ? " in the events read so far" : ""}.</p>;
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Turn" value={String(turn)}
          onChange={(event) => setTurn(event.target.value === "all" ? "all" : Number(event.target.value))}
          className="min-w-0 max-w-full truncate rounded-md border border-line bg-transparent px-1.5 py-1 text-xs">
          <option value="all">All turns ({turns.length})</option>
          {turns.map((t) => (
            <option key={t.id} value={t.id}>Turn {t.turn}{t.prompt ? `: ${t.prompt.slice(0, 40)}` : ""}</option>
          ))}
        </select>
        <div className="ml-auto inline-flex overflow-hidden rounded-md border border-line text-xs" role="group" aria-label="Diff view">
          <button type="button" aria-pressed={!split} onClick={() => flip(false)}
            className={cn("px-2 py-1", !split ? "bg-raised text-ink" : "text-muted")}>Unified</button>
          <button type="button" aria-pressed={split} onClick={() => flip(true)}
            className={cn("px-2 py-1", split ? "bg-raised text-ink" : "text-muted")}>Split</button>
        </div>
      </div>
      {earlier && <p className="text-xs text-faint">Only the events read so far; read earlier ones in the session.</p>}
      <ul className="space-y-1 text-xs" aria-label="Changed files">
        {files.map((file) => (
          <li key={file.path} className="flex items-center gap-2 font-mono">
            <a href={`#diff-${encodeURIComponent(file.path)}`} className="min-w-0 flex-1 truncate hover:underline">{file.path}</a>
            {file.created && <span className="text-ok">new</span>}
            <span className="text-ok">+{file.added}</span>
            <span className="text-danger">−{file.removed}</span>
          </li>
        ))}
      </ul>
      {files.map((file) => (
        <section key={file.path} id={`diff-${encodeURIComponent(file.path)}`} className="space-y-2">
          {file.diffs.map((diff, index) => <DiffView key={index} diff={diff} split={split} tall />)}
        </section>
      ))}
    </div>
  );
}

function Plans({ events }: { events: HubEvent[] }) {
  const plans = useMemo(() => planHistory(events), [events]);
  const latest = plans.at(-1);
  if (!latest) return <p className="text-faint">No plan yet.</p>;
  return (
    <div className="space-y-4">
      <section>
        <h3 className="mb-2 text-xs font-medium text-faint">
          Latest plan · {latest.done} of {latest.entries.length} done
        </h3>
        <PlanList entries={latest.entries} />
      </section>
      {plans.length > 1 && (
        <section>
          <h3 className="mb-2 text-xs font-medium text-faint">How it changed</h3>
          <ol className="space-y-1 text-xs text-muted" aria-label="Plan history">
            {plans.map((plan) => (
              <li key={plan.key}>
                {plan.at ? new Date(plan.at).toLocaleTimeString() : "—"} · {plan.done}/{plan.entries.length} done
                {plan.entries.find((e) => e.status === "in_progress") &&
                  ` · ${plan.entries.find((e) => e.status === "in_progress")!.content}`}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function Fact({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-faint">{term}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}

/** H2: the session's name, given, changed or cleared here (one line, 80 characters; the hub checks it). */
function NameField({ session }: { session: HubSession }) {
  const client = useQueryClient();
  const [value, setValue] = useState(session.title ?? "");
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const save = async (title: string | null) => {
    setSaving(true);
    setProblem(null);
    try {
      const saved = await renameHubSession(session.id, title);
      setValue(saved.title ?? "");
    } catch (failure) {
      setProblem(failure instanceof ApiError ? failure.message : "Could not rename it.");
    } finally {
      setSaving(false);
      void client.invalidateQueries({ queryKey: hubKeys.session(session.id) });
    }
  };
  const changed = value.trim() !== (session.title ?? "");
  return (
    <form className="space-y-1" onSubmit={(event) => { event.preventDefault(); void save(value); }}>
      <label className="text-xs text-faint" htmlFor={`session-name-${session.id}`}>Name</label>
      <div className="flex gap-1.5">
        <input id={`session-name-${session.id}`} value={value} maxLength={80} placeholder="No name"
          onChange={(event) => setValue(event.target.value)}
          className="min-w-0 flex-1 rounded-md border border-line bg-transparent px-2 py-1 text-sm text-ink" />
        <Button type="submit" variant="outline" disabled={saving || !changed}>Save</Button>
        {session.title && (
          <Button type="button" variant="ghost" disabled={saving} onClick={() => void save(null)}>Clear</Button>
        )}
      </div>
      {problem && <p role="alert" className="text-xs text-danger">{problem}</p>}
    </form>
  );
}

function Info({ session, detail, context, running, closing, onClose }: {
  session: HubSession | undefined;
  detail: HubSessionDetail | undefined;
  context: { used?: number; size?: number } | null | undefined;
  running: boolean;
  closing: boolean;
  onClose: () => void;
}) {
  if (!session) return <p className="text-faint">Loading…</p>;
  const used = context?.used ?? 0;
  const size = context?.size ?? 0;
  return (
    <>
      <NameField key={`${session.id}:${session.title ?? ""}`} session={session} />
      <dl className="mt-4 space-y-3 text-muted">
        <Fact term="Project">{session.project}</Fact>
        <Fact term="Agent">{session.agent}{session.profile ? ` · ${session.profile}` : ""}</Fact>
        <Fact term="Device">{session.runner} {session.runner_connected === false ? "(not connected)" : ""}</Fact>
        <Fact term="Model">{[session.model, session.effort].filter(Boolean).join(" · ") || "Default"}</Fact>
        {context && (
          <Fact term="Context">
            {used.toLocaleString()} / {size.toLocaleString()} tokens
            {size > 0 && (
              <span className="mt-1 block h-1.5 overflow-hidden rounded bg-raised">
                <span ref={(node) => { if (node) node.style.width = `${Math.min(100, (used / size) * 100)}%`; }}
                  className="block h-full bg-accent" />
              </span>
            )}
          </Fact>
        )}
        {detail && detail.queued.length > 0 && (
          <Fact term="Queued">
            <ul className="list-disc pl-4">{detail.queued.map((q) => <li key={q.id}>{q.prompt}</li>)}</ul>
          </Fact>
        )}
        {detail && detail.attempts.length > 0 && (
          <Fact term="Recent attempts">
            <ul className="space-y-1">
              {detail.attempts.slice(0, 8).map((a) => (
                <li key={a.id} className="text-xs">
                  #{a.task_id} {a.runner} · {a.outcome ?? "running"}{a.why ? ` · ${a.why.slice(0, 120)}` : ""}
                </li>
              ))}
            </ul>
          </Fact>
        )}
        <Fact term="Session">#{session.id}{session.current ? " · current of its lane" : ""}</Fact>
        {session.updated_at && <Fact term="Last activity">{new Date(session.updated_at).toLocaleString()}</Fact>}
      </dl>
      {session.state !== "closed" && (
        <Button variant="outline" className="mt-4 w-full" disabled={running || closing} onClick={onClose}
          title={running ? "Stop the running turn first" : undefined}>
          Close session
        </Button>
      )}
    </>
  );
}

export function sessionTabs(props: {
  events: HubEvent[];
  earlier: boolean;
  session: HubSession | undefined;
  detail: HubSessionDetail | undefined;
  context: { used?: number; size?: number } | null | undefined;
  running: boolean;
  closing: boolean;
  onClose: () => void;
}): PanelTab[] {
  return [
    { id: "changes", label: "Changes", content: <Changes events={props.events} earlier={props.earlier} /> },
    { id: "plan", label: "Plan", content: <Plans events={props.events} /> },
    { id: "info", label: "Info", content: <Info {...props} /> },
  ];
}
