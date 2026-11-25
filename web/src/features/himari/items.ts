// V3-P102 §7.4: HIMARI's events as the timeline's items. Pure functions, tested in items.test.ts.
import { fileUrl, type EventView, type FileView, type ReferenceView } from "@/api/himari";
import type { Item, Shown, ToolStep } from "@/features/timeline";

export type { Item, ToolStep } from "@/features/timeline";

/** What the model wrote as it is shown: without its marks (moods, moves), as the server gives it. */
const shown = (event: EventView) => event.display?.text ?? event.text ?? "";

const callKey = (run: string | null | undefined, call: string | null | undefined) => `${run ?? ""}/${call ?? ""}`;

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
 *  page does (V3-P68). ``conversation`` gives the files their links. */
export function toItems(events: readonly EventView[], conversation?: string | null): Item[] {
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
          kind: "user", key, text: event.text ?? "",
          ...(event.attachments?.length ? { files: event.attachments.map((f) => shownFile(f, conversation)) } : {}),
          ...(event.references?.length ? { references: event.references.map(referenceLabel) } : {}),
        });
        break;
      case "assistant_final":
        closeWork();
        items.push({ kind: "answer", key, text: shown(event), model: event.model ?? null,
          ...(event.thought ? { thought: event.thought } : {}) });
        break;
      case "assistant_tool_calls": {
        if (work === null) {
          work = { kind: "work", key, notes: [], steps: [], model: event.model ?? null };
          items.push(work);
        }
        const block: Extract<Item, { kind: "work" }> = work;
        // character-mind §3.3 (review P1-6): a first Run that thought, then called a Tool, keeps its foldout
        if (event.thought) block.thoughts = [...(block.thoughts ?? []), event.thought];
        if (shown(event).trim()) block.notes.push(shown(event));
        for (const call of event.calls ?? []) {
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
        break; // a result without its call is dropped
      }
      case "wake":
        closeWork();
        items.push({ kind: "notice", key, text: event.text ?? "" });
        break;
      case "wake_thought": // character-mind §3.3: a free wake it let pass, with what it thought
        closeWork();
        items.push({ kind: "notice", key, text: `Let pass: ${event.text ?? ""}`,
          ...(event.thought ? { thought: event.thought } : {}) });
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
  for (const field of ["path", "query", "url", "name"]) {
    const value = step.arguments[field];
    if (typeof value === "string" && value) return `${name} · ${value.length > 80 ? `${value.slice(0, 79)}…` : value}`;
  }
  return name;
}
