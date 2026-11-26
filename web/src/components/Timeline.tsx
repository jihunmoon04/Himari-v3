import { Check, ChevronRight, Copy, CircleAlert, CircleCheck, LoaderCircle, Wrench } from "lucide-react";
import { useState } from "react";
import { stepTitle } from "@/features/himari/items";
import type { LiveState } from "@/features/himari/live";
import type { Item, Shown, ToolStep } from "@/features/timeline";
import { cn } from "@/lib/cn";
import { AssistantText } from "./AssistantText";
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

// character-day §5.5, §5.6: how HIMARI, asleep, took the message (the owner's view only)
const ASLEEP: Record<"none" | "mumble" | "wake", string> = {
  none: "While asleep · no answer",
  mumble: "While asleep · mumbled",
  wake: "While asleep · woke HIMARI",
};

function UserBubble({ text, files, references, asleep }: {
  text: string; files?: Shown[]; references?: string[]; asleep?: keyof typeof ASLEEP;
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
      {asleep && <p className="text-xs text-faint" aria-label="While asleep">{ASLEEP[asleep]}</p>}
    </div>
  );
}

/** character-day §7: the diary HIMARI wrote from this day's conversation, folded; the owner opens it. */
function DiaryLine({ at, text }: { at: string; text: string }) {
  const time = new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <details className="text-center text-xs text-faint" aria-label="Diary">
      <summary className="cursor-pointer select-none">Wrote the diary · {time}</summary>
      {text && <p className="mx-auto mt-1 max-w-[85%] whitespace-pre-wrap text-left">{text}</p>}
    </details>
  );
}

type StepState = "running" | "ok" | "error" | "none";

function stepState(step: ToolStep, running: boolean): StepState {
  if (step.result) return step.result.isError ? "error" : "ok";
  return running ? "running" : "none";
}

function StepRow({ step, running }: { step: ToolStep; running: boolean }) {
  const [open, setOpen] = useState(false);
  const state = stepState(step, running);
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
        {state === "none" && <Wrench className="size-3.5 shrink-0" aria-hidden />}
        <span className="min-w-0 flex-1 truncate font-mono text-[0.82rem]">{stepTitle(step)}</span>
      </button>
      {open && (
        <div className="ml-7 mt-1 space-y-2 text-xs">
          <pre className="max-h-64 overflow-auto rounded-md bg-code p-2 font-mono text-muted">
            {JSON.stringify(step.arguments, null, 2)}
          </pre>
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

/** character-mind §3.2: HIMARI's thought, folded; the owner opens it. */
function Thought({ text }: { text: string }) {
  return (
    <details className="mb-1 text-xs text-faint">
      <summary className="cursor-pointer select-none">Thought</summary>
      <p className="mt-1 whitespace-pre-wrap">{text}</p>
    </details>
  );
}

function WorkBlock({ item, running }: { item: Extract<Item, { kind: "work" }>; running: boolean }) {
  const [open, setOpen] = useState(running);
  const busy = running && item.steps.some((step) => stepState(step, true) === "running");
  return (
    <div>
      {item.thoughts?.map((thought, index) => <Thought key={index} text={thought} />)}
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
    </div>
  );
}

export function Timeline({ items, live, running, conversation }: {
  items: Item[];
  live: LiveState;
  running: boolean;
  conversation: string; // what its memory lines undo through
}) {
  const lastWork = [...items].reverse().find((item) => item.kind === "work")?.key;
  return (
    <div className="space-y-6">
      {items.map((item) => {
        switch (item.kind) {
          case "user":
            return <UserBubble key={item.key} text={item.text} files={item.files} references={item.references}
              asleep={item.asleep} />;
          case "diary":
            return <DiaryLine key={item.key} at={item.at} text={item.text} />;
          case "answer":
            return (
              <div key={item.key} className="group">
                {item.thought && <Thought text={item.thought} />}
                <AssistantText text={item.text} />
                <CopyButton text={item.text} />
              </div>
            );
          case "work":
            return <WorkBlock key={item.key} item={item} running={running && item.key === lastWork} />;
          case "files":
            return <Files key={item.key} files={item.files} />;
          case "memory":
            return <MemoryLine key={item.key} conversation={conversation} run={item.run} call={item.call}
              action={item.action} text={item.text} undo={item.undo} />;
          case "notice":
            return (
              <div key={item.key} className="text-center text-xs text-faint">
                <p>{item.text}</p>
                {item.thought && <Thought text={item.thought} />}
              </div>
            );
        }
      })}
      {running &&
        (live.text ? (
          <AssistantText text={live.text} className="opacity-80" />
        ) : (
          <div className="flex items-center gap-2 text-sm text-muted" role="status">
            <span className="pulse inline-block size-2 rounded-full bg-accent" />
            {live.tools.length ? `Preparing ${live.tools[live.tools.length - 1]}…` : "Thinking…"}
          </div>
        ))}
    </div>
  );
}
