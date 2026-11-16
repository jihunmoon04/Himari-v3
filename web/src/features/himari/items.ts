// V3-P102 §7.4: HIMARI's events as the timeline's items. The timeline knows only these kinds; hub sessions get
// their own converter into the same kinds (W2). Pure functions, tested in items.test.ts.
import type { EventView } from "@/api/himari";

export const ASK_USER = "ask_user";

export interface ToolStep {
  key: string; // run_id/call_id: call ids repeat across Runs
  toolId: string;
  arguments: Record<string, unknown>;
  result: { text: string; isError: boolean } | null;
}

export type Item =
  | { kind: "user"; key: string; text: string; answers: boolean }
  | { kind: "answer"; key: string; text: string; model: string | null }
  | { kind: "work"; key: string; notes: string[]; steps: ToolStep[]; model: string | null }
  | { kind: "question"; key: string; text: string; callKey: string }
  | { kind: "notice"; key: string; text: string };

/** What the model wrote as it is shown: without its marks (moods, moves), as the server gives it. */
const shown = (event: EventView) => event.display?.text ?? event.text ?? "";

const callKey = (run: string | null | undefined, call: string | null | undefined) => `${run ?? ""}/${call ?? ""}`;

function questionText(call: { arguments: Record<string, unknown>; display?: { text: string } }): string {
  if (call.display?.text) return call.display.text;
  const asked = call.arguments.question;
  return typeof asked === "string" ? asked : "";
}

/** The items for ``events`` (oldest first). Tool calls of consecutive steps fold into one work block, as the old
 *  page does (V3-P68); an ask_user call is a question, and the user's answer to it is a user item. */
export function toItems(events: readonly EventView[]): Item[] {
  const items: Item[] = [];
  const steps = new Map<string, ToolStep>();
  let work: Extract<Item, { kind: "work" }> | null = null;

  const closeWork = () => {
    work = null;
  };

  for (const event of events) {
    const key = String(event.sequence);
    switch (event.kind) {
      case "user_message":
        closeWork();
        items.push({ kind: "user", key, text: event.text ?? "", answers: Boolean(event.answers_call) });
        break;
      case "assistant_final":
        closeWork();
        items.push({ kind: "answer", key, text: shown(event), model: event.model ?? null });
        break;
      case "assistant_tool_calls": {
        const calls = event.calls ?? [];
        const asked = calls.find((call) => call.tool_id === ASK_USER);
        if (asked) {
          closeWork();
          if (shown(event).trim()) items.push({ kind: "answer", key: `${key}:lead`, text: shown(event), model: event.model ?? null });
          items.push({ kind: "question", key, text: questionText(asked), callKey: callKey(event.run_id, asked.call_id) });
          break;
        }
        if (work === null) {
          work = { kind: "work", key, notes: [], steps: [], model: event.model ?? null };
          items.push(work);
        }
        const block: Extract<Item, { kind: "work" }> = work;
        if (shown(event).trim()) block.notes.push(shown(event));
        for (const call of calls) {
          const step: ToolStep = {
            key: callKey(event.run_id, call.call_id),
            toolId: call.tool_id,
            arguments: call.arguments,
            result: null,
          };
          steps.set(step.key, step);
          block.steps.push(step);
        }
        break;
      }
      case "tool_result": {
        const step = steps.get(callKey(event.run_id, event.call_id));
        if (step) step.result = { text: event.text ?? "", isError: Boolean(event.is_error) };
        break; // an ask_user answer comes back as the user's message; a result without its call is dropped
      }
      case "wake":
        closeWork();
        items.push({ kind: "notice", key, text: event.text ?? "" });
        break;
      default:
        break; // a kind this page does not know yet
    }
  }
  return items;
}

/** A short line for a tool step: its tool and the argument a person looks for first. */
export function stepTitle(step: ToolStep): string {
  const name = step.toolId.includes("/") ? step.toolId.slice(step.toolId.indexOf("/") + 1) : step.toolId;
  for (const field of ["path", "query", "command", "url", "name", "project"]) {
    const value = step.arguments[field];
    if (typeof value === "string" && value) return `${name} · ${value.length > 80 ? `${value.slice(0, 79)}…` : value}`;
  }
  return name;
}
