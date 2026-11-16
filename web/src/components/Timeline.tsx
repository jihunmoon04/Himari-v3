import { ChevronRight, CircleAlert, CircleCheck, LoaderCircle, MessageCircleQuestion, Wrench } from "lucide-react";
import { useState } from "react";
import type { Item, ToolStep } from "@/features/himari/items";
import { stepTitle } from "@/features/himari/items";
import type { LiveState } from "@/features/himari/live";
import { cn } from "@/lib/cn";
import { AssistantText } from "./AssistantText";

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div
        role="article"
        aria-label="Your message"
        className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-mine px-4 py-2.5 leading-relaxed"
      >
        {text}
      </div>
    </div>
  );
}

function StepRow({ step, running }: { step: ToolStep; running: boolean }) {
  const [open, setOpen] = useState(false);
  const state = step.result === null ? (running ? "running" : "none") : step.result.isError ? "error" : "ok";
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
        <span className="truncate font-mono text-[0.82rem]">{stepTitle(step)}</span>
      </button>
      {open && (
        <div className="ml-7 mt-1 space-y-2 text-xs">
          <pre className="max-h-64 overflow-auto rounded-md bg-code p-2 font-mono text-muted">
            {JSON.stringify(step.arguments, null, 2)}
          </pre>
          {step.result && (
            <pre
              className={cn(
                "max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-code p-2 font-mono",
                step.result.isError ? "text-danger" : "text-muted",
              )}
            >
              {step.result.text || "(empty)"}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

function WorkBlock({ item, running }: { item: Extract<Item, { kind: "work" }>; running: boolean }) {
  const [open, setOpen] = useState(running);
  const done = item.steps.filter((step) => step.result !== null).length;
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
        {running && done < item.steps.length && <LoaderCircle className="size-3.5 animate-spin" />}
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

export function Timeline({ items, live, running }: { items: Item[]; live: LiveState; running: boolean }) {
  const lastWork = [...items].reverse().find((item) => item.kind === "work")?.key;
  return (
    <div className="space-y-6">
      {items.map((item) => {
        switch (item.kind) {
          case "user":
            return <UserBubble key={item.key} text={item.text} />;
          case "answer":
            return <AssistantText key={item.key} text={item.text} />;
          case "work":
            return <WorkBlock key={item.key} item={item} running={running && item.key === lastWork} />;
          case "question":
            return (
              <div key={item.key} className="rounded-xl border border-wait/40 bg-wait/5 px-4 py-3">
                <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-wait">
                  <MessageCircleQuestion className="size-3.5" /> HIMARI asks
                </div>
                <AssistantText text={item.text} />
              </div>
            );
          case "notice":
            return (
              <p key={item.key} className="text-center text-xs text-faint">
                {item.text}
              </p>
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
