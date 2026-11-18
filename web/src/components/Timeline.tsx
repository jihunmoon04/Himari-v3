import {
  Brain, Check, ChevronRight, Copy, CircleAlert, CircleCheck, CircleDashed, CircleHelp, LoaderCircle, MessageCircleQuestion,
  ShieldQuestion, Wrench,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { stepTitle } from "@/features/himari/items";
import type { LiveState } from "@/features/himari/live";
import type { Item, Shown, ToolStep, Turn } from "@/features/timeline";
import { cn } from "@/lib/cn";
import { AssistantText } from "./AssistantText";
import { DelegationCard } from "./DelegationCard";
import { AskedProjectCard } from "./AskedProjectCard";
import { DiffView, diffStats } from "./DiffView";
import { Files, MemoryLine, References } from "./TimelineFiles";

/** Copies an answer's text (as written, Markdown and all). */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false); // the browser refused (not a secure page, no permission)
    }
  };
  return (
    <button type="button" onClick={() => void copy()} aria-label={copied ? "Copied" : "Copy answer"}
      className="mt-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-faint opacity-0 transition-opacity
        hover:bg-raised hover:text-ink focus-visible:opacity-100 group-hover:opacity-100 max-md:opacity-100">
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} {copied ? "Copied" : "Copy"}
    </button>
  );
}

function UserBubble({ text, meta, files, references }: {
  text: string; meta?: string[]; files?: Shown[]; references?: string[];
}) {
  return (
    <div className="flex flex-col items-end gap-1">
      {files && files.length > 0 && <Files files={files} align="end" />}
      {references && references.length > 0 && <References labels={references} />}
      <div
        role="article"
        aria-label="Your message"
        className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-mine px-4 py-2.5 leading-relaxed"
      >
        {text}
      </div>
      {meta && meta.length > 0 && <div className="text-xs text-faint">{meta.join(" · ")}</div>}
    </div>
  );
}

type StepState = "running" | "ok" | "error" | "unknown" | "none";

function stepState(step: ToolStep, running: boolean): StepState {
  if (step.status === "failed" || step.result?.isError) return "error";
  if (step.status === "completed" || (step.result && !step.status)) return "ok";
  if (step.status === "unknown") return "unknown";
  if (step.status === "pending" || step.status === "in_progress") return running ? "running" : "unknown";
  return running ? "running" : "none";
}

function StepRow({ step, running }: { step: ToolStep; running: boolean }) {
  const [open, setOpen] = useState(false);
  const state = stepState(step, running);
  const changed = step.diffs?.reduce(
    (sum, diff) => {
      const stats = diffStats(diff);
      return { added: sum.added + stats.added, removed: sum.removed + stats.removed };
    },
    { added: 0, removed: 0 },
  );
  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-muted hover:bg-raised"
      >
        <ChevronRight className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")} />
        {state === "running" && <LoaderCircle className="size-3.5 shrink-0 animate-spin" aria-label="Running" />}
        {state === "ok" && <CircleCheck className="size-3.5 shrink-0 text-ok" aria-label="Done" />}
        {state === "error" && <CircleAlert className="size-3.5 shrink-0 text-danger" aria-label="Failed" />}
        {state === "unknown" && <CircleDashed className="size-3.5 shrink-0" aria-label="Did not report back" />}
        {state === "none" && <Wrench className="size-3.5 shrink-0" aria-hidden />}
        <span className="min-w-0 flex-1 truncate font-mono text-[0.82rem]">{stepTitle(step)}</span>
        {changed && (changed.added > 0 || changed.removed > 0) && (
          <span className="shrink-0 font-mono text-xs">
            <span className="text-ok">+{changed.added}</span> <span className="text-danger">−{changed.removed}</span>
          </span>
        )}
      </button>
      {open && (
        <div className="ml-7 mt-1 space-y-2 text-xs">
          {step.locations && step.locations.length > 0 && (
            <p className="font-mono text-faint">{step.locations.join(", ")}</p>
          )}
          {step.input !== undefined ? (
            step.input && (
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-code p-2 font-mono text-muted">
                {step.input}
              </pre>
            )
          ) : (
            <pre className="max-h-64 overflow-auto rounded-md bg-code p-2 font-mono text-muted">
              {JSON.stringify(step.arguments, null, 2)}
            </pre>
          )}
          {step.diffs?.map((diff, index) => <DiffView key={index} diff={diff} />)}
          {step.result && step.result.text && (
            <pre
              className={cn(
                "max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-code p-2 font-mono",
                step.result.isError ? "text-danger" : "text-muted",
              )}
            >
              {step.result.text}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

function WorkBlock({ item, running }: { item: Extract<Item, { kind: "work" }>; running: boolean }) {
  const [open, setOpen] = useState(running);
  const busy = running && item.steps.some((step) => stepState(step, true) === "running");
  return (
    <div className="rounded-xl border border-line">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-muted hover:text-ink"
      >
        <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
        <span>
          Activity · {item.steps.length} {item.steps.length === 1 ? "step" : "steps"}
        </span>
        {busy && <LoaderCircle className="size-3.5 animate-spin" />}
      </button>
      {open && (
        <div className="space-y-2 border-t border-line px-2 py-2">
          {item.notes.map((note, index) => (
            <AssistantText key={index} text={note} className="px-2 text-sm text-muted" />
          ))}
          <ul className="space-y-0.5">
            {item.steps.map((step) => (
              <StepRow key={step.key} step={step} running={running} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Thought({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-sm text-faint hover:text-muted"
      >
        <Brain className="size-3.5" /> Thinking
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
      </button>
      {open && <AssistantText text={text} className="mt-2 border-l-2 border-line pl-3 text-sm text-muted" />}
    </div>
  );
}

export function PlanList({ entries }: { entries: { content: string; status: string }[] }) {
  return (
    <ul className="space-y-1 text-sm" aria-label="Plan">
      {entries.map((entry, index) => (
        <li key={index} className="flex items-start gap-2">
          {entry.status === "completed" ? (
            <CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" aria-label="Done" />
          ) : entry.status === "in_progress" ? (
            <LoaderCircle className="mt-0.5 size-4 shrink-0 text-accent" aria-label="In progress" />
          ) : (
            <CircleDashed className="mt-0.5 size-4 shrink-0 text-faint" aria-label="To do" />
          )}
          <span className={cn(entry.status === "completed" && "text-muted line-through decoration-faint")}>
            {entry.content}
          </span>
        </li>
      ))}
    </ul>
  );
}

const OUTCOMES: Record<string, string> = {
  answered: "Answered", refused: "Refused", expired: "Expired: nobody answered in time", cancelled: "Cancelled",
};

function TurnLine({ turn }: { turn: Turn }) {
  const good = turn.outcome === "done";
  const time = turn.seconds === null ? "" : turn.seconds < 60 ? ` · ${turn.seconds}s`
    : ` · ${Math.floor(turn.seconds / 60)}m ${turn.seconds % 60}s`;
  return (
    <div className="flex items-center gap-3 text-xs text-faint" role="separator">
      <span className="h-px flex-1 bg-line" />
      <span className={cn(!good && "text-wait")}>
        {good ? "Done" : turn.outcome}
        {time}
        {!good && turn.why ? ` · ${turn.why}` : ""}
      </span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

export function Timeline({ items, live, running, working, renderRequest, conversation }: {
  items: Item[];
  live?: LiveState;
  running: boolean;
  working?: string; // a hub session at work: the line shown while nothing new comes
  // a request that can still be answered, drawn by the page (it knows the pending list); null: drawn as a record
  renderRequest?: (item: Extract<Item, { kind: "request" }>) => ReactNode | null;
  conversation?: string; // a HIMARI conversation's id: what its memory lines undo through
}) {
  const lastWork = [...items].reverse().find((item) => item.kind === "work")?.key;
  return (
    <div className="space-y-6">
      {items.map((item) => {
        switch (item.kind) {
          case "user":
            return <UserBubble key={item.key} text={item.text} meta={item.meta} files={item.files} references={item.references} />;
          case "answer":
            return (
              <div key={item.key} className="group">
                <AssistantText text={item.text} />
                <CopyButton text={item.text} />
              </div>
            );
          case "thought":
            return <Thought key={item.key} text={item.text} />;
          case "work":
            return <WorkBlock key={item.key} item={item} running={running && item.key === lastWork} />;
          case "plan":
            return (
              <div key={item.key} className="rounded-xl border border-line px-4 py-3">
                <div className="mb-2 text-xs font-medium text-faint">Plan</div>
                <PlanList entries={item.entries} />
              </div>
            );
          case "question":
            return (
              <div key={item.key} className="rounded-xl border border-wait/40 bg-wait/5 px-4 py-3">
                <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-wait">
                  <MessageCircleQuestion className="size-3.5" /> HIMARI asks
                </div>
                <AssistantText text={item.text} />
              </div>
            );
          case "request": {
            const answerable = item.outcome ? null : renderRequest?.(item);
            if (answerable) return <div key={item.key}>{answerable}</div>;
            return (
              <div
                key={item.key}
                role="group"
                aria-label="Agent request"
                className={cn("rounded-xl border px-4 py-3",
                  item.outcome ? "border-line" : "border-wait/40 bg-wait/5")}
              >
                <div className={cn("mb-1 flex items-center gap-1.5 text-xs font-medium",
                  item.outcome ? "text-faint" : "text-wait")}>
                  {item.options.length ? <ShieldQuestion className="size-3.5" /> : <CircleHelp className="size-3.5" />}
                  {item.options.length ? "Permission" : "Question"}
                  {" · "}
                  {item.outcome ? OUTCOMES[item.outcome] ?? item.outcome : "Waiting for you"}
                </div>
                <p className="whitespace-pre-wrap break-words text-sm">{item.ask}</p>
                {item.options.length > 0 && !item.outcome && (
                  <p className="mt-1 text-xs text-faint">Choices: {item.options.join(" · ")}</p>
                )}
              </div>
            );
          }
          case "turn":
            return <TurnLine key={item.key} turn={item.turn} />;
          case "delegation":
            return <DelegationCard key={item.key} delegation={item.delegation} />;
          case "project_request":
            return <AskedProjectCard key={item.key} asked={item.asked} />;
          case "files":
            return <Files key={item.key} files={item.files} />;
          case "memory":
            return <MemoryLine key={item.key} conversation={conversation} run={item.run} call={item.call}
              action={item.action} text={item.text} undo={item.undo} />;
          case "notice":
            return (
              <p key={item.key} className="text-center text-xs text-faint">
                {item.text}
              </p>
            );
        }
      })}
      {running && live &&
        (live.text ? (
          <AssistantText text={live.text} className="opacity-80" />
        ) : (
          <div className="flex items-center gap-2 text-sm text-muted" role="status">
            <span className="pulse inline-block size-2 rounded-full bg-accent" />
            {live.tools.length ? `Preparing ${live.tools[live.tools.length - 1]}…` : "Thinking…"}
          </div>
        ))}
      {running && !live && working && (
        <div className="flex items-center gap-2 text-sm text-muted" role="status">
          <span className="pulse inline-block size-2 rounded-full bg-accent" />
          {working}
        </div>
      )}
    </div>
  );
}
