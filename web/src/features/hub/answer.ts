// Answering what an agent asks (V3-P102 §4.2, §4.6; as the classic page's hub.js did). Pure, tested in
// answer.test.ts.
import type { HubField, HubPending } from "@/api/hub";

/** What the user filled in a question's form so far: a field's text, or the values of the choices picked. */
export type Draft = Record<string, string | string[]>;

/** The answer as the agent's form asks for it: only the fields that were filled, a choice as its own value (a
 *  number stays a number). */
export function answerContent(fields: readonly HubField[], draft: Draft): Record<string, unknown> {
  const content: Record<string, unknown> = {};
  for (const field of fields) {
    const value = draft[field.name];
    const real = (text: string) => {
      const choice = (field.choices ?? []).find((c) => String(c.value) === text);
      return choice ? choice.value : text;
    };
    if (Array.isArray(value)) {
      if (value.length) content[field.name] = value.map(real);
    } else if (typeof value === "string" && value !== "") {
      content[field.name] = real(value);
    }
  }
  return content;
}

/** The agent's own "reject" choice is the refusal when it gave one: a second button would say the same. */
export const hasOwnReject = (item: HubPending) =>
  (item.options ?? []).some((option) => String(option.kind ?? "").startsWith("reject"));

/** An option that lets the agent go on (drawn as the main button). */
export const allows = (kind: string | null | undefined) => String(kind ?? "").startsWith("allow");

/** Where a request comes from, in a line: project · agent · on device (the server's own device is not named). */
export function whereFrom(item: HubPending): string {
  const task = item.task ?? {};
  return [task.project, task.agent, task.runner && task.runner !== "local" ? `on ${task.runner}` : null]
    .filter(Boolean).join(" · ") || `task ${item.task_id}`;
}
