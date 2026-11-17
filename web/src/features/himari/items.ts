// V3-P102 §7.4: HIMARI's events as the timeline's items. The timeline knows only these kinds; hub sessions get
// their own converter into the same kinds (W2). Pure functions, tested in items.test.ts.
import { fileUrl, type EventView, type FileView, type ReferenceView } from "@/api/himari";
import type { Delegation, Item, Shown, ToolStep } from "@/features/timeline";

export type { Item, ToolStep } from "@/features/timeline";

export const ASK_USER = "ask_user";

/** What the model wrote as it is shown: without its marks (moods, moves), as the server gives it. */
const shown = (event: EventView) => event.display?.text ?? event.text ?? "";

const callKey = (run: string | null | undefined, call: string | null | undefined) => `${run ?? ""}/${call ?? ""}`;

function questionText(call: { arguments: Record<string, unknown>; display?: { text: string } }): string {
  if (call.display?.text) return call.display.text;
  const asked = call.arguments.question;
  return typeof asked === "string" ? asked : "";
}

const text = (value: unknown) => (typeof value === "string" ? value : "");
const whole = (value: unknown) => (typeof value === "number" && Number.isInteger(value) ? value : null);

/** A ``hub_delegate`` call and its result as a card (V3-P102 §5.1). The hub's tool answers with the task as JSON
 *  (its id, status, agent, and the session when the call named one); an error result is the hub's reason. */
export function delegationOf(step: ToolStep): Delegation {
  const args = step.arguments;
  const asked: Delegation = {
    taskId: null, project: text(args.project), agent: text(args.agent) || null, prompt: text(args.prompt),
    session: whole(args.session), status: null, error: null,
  };
  if (!step.result) return asked;
  if (step.result.isError) return { ...asked, error: step.result.text || "The hub refused it." };
  try {
    const task: unknown = JSON.parse(step.result.text);
    if (task && typeof task === "object") {
      const found = task as Record<string, unknown>;
      const id = whole(found.id);
      if (id !== null) {
        return { ...asked, taskId: id, agent: text(found.agent) || asked.agent, status: text(found.status) || null,
          session: whole(found.session) ?? asked.session };
      }
    }
  } catch {
    // not the task: said below
  }
  return { ...asked, error: "The hub's answer could not be read." };
}

/** A file as the timeline shows it; ``conversation`` gives its links (none without it, or once it expired). */
export function shownFile(file: FileView, conversation: string | null | undefined): Shown {
  const url = conversation && !file.expired ? fileUrl(conversation, file) : null;
  const ready = file.image?.state === "ready" && url !== null;
  return {
    key: `${file.file_id}/${file.version}`,
    name: file.name ?? "file",
    size: typeof file.size === "number" ? file.size : null,
    url,
    preview: ready ? `${url}/view` : null,
    width: ready ? file.image?.preview?.width ?? null : null,
    height: ready ? file.image?.preview?.height ?? null : null,
  };
}

/** A reference chip's words: the file, and its lines when it points at a part. */
export const referenceLabel = (ref: ReferenceView) =>
  ref.whole || ref.start_line == null ? ref.name : `${ref.name}:${ref.start_line}-${ref.end_line ?? ref.start_line}`;

/** The items for ``events`` (oldest first). Tool calls of consecutive steps fold into one work block, as the old
 *  page does (V3-P68); an ask_user call is a question, and the user's answer to it is a user item. ``hubSource``:
 *  the mcp.json server that is the hub; each result of its ``hub_delegate`` adds a card after its work block. */
export function toItems(events: readonly EventView[], hubSource?: string | null, conversation?: string | null): Item[] {
  const delegate = hubSource ? `${hubSource}/hub_delegate` : null;
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
        items.push({
          kind: "user", key, text: event.text ?? "", answers: Boolean(event.answers_call),
          ...(event.attachments?.length ? { files: event.attachments.map((f) => shownFile(f, conversation)) } : {}),
          ...(event.references?.length ? { references: event.references.map(referenceLabel) } : {}),
        });
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
        const given = [...(event.presented_files ?? []), ...(event.images ?? [])];
        if (given.length) { // what the Tool gave the user, after the calls it came from
          closeWork();
          items.push({ kind: "files", key: `${key}:files`, files: given.map((f) => shownFile(f, conversation)) });
        }
        if (event.memory && event.run_id && event.call_id) {
          closeWork();
          items.push({ kind: "memory", key: `${key}:memory`, run: event.run_id, call: event.call_id,
            action: event.memory.action, text: event.memory.text ?? "", undo: event.memory.undo });
        }
        if (step && delegate !== null && step.toolId === delegate) {
          closeWork(); // the card comes after the calls it followed; later calls start a new block
          items.push({ kind: "delegation", key: `${key}:delegation`, delegation: delegationOf(step) });
        }
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
  if (step.title) return step.title.length > 100 ? `${step.title.slice(0, 99)}…` : step.title; // the agent's own
  const name = step.toolId.includes("/") ? step.toolId.slice(step.toolId.indexOf("/") + 1) : step.toolId;
  for (const field of ["path", "query", "command", "url", "name", "project"]) {
    const value = step.arguments[field];
    if (typeof value === "string" && value) return `${name} · ${value.length > 80 ? `${value.slice(0, 79)}…` : value}`;
  }
  return name;
}
