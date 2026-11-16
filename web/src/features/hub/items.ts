// A hub session's events (the hub's user-api-v1 §6, as this server relays them) as the timeline's items
// (V3-P102 §7.4). Pure functions, tested in items.test.ts.
//
// - turn_start: the prompt as the user's item, with its model, effort and mode.
// - text / thought: consecutive pieces join into one answer / one thought (the hub sends them in 0.5 s batches).
// - tool: one step per call id of an attempt; later events of the same id update it (only the fields they carry). Consecutive
//   calls fold into one work block.
// - plan: the latest plan; consecutive plans replace each other.
// - request / answer: a request, and later its outcome. Whether it can still be answered is the pending list's call.
// - turn_end: how the turn ended and how long it took.
// - session, truncated: a short notice. usage, setup and unknown kinds draw nothing.
import type { HubEvent } from "@/api/hub";
import type { Diff, Item, ToolStep } from "@/features/timeline";

const str = (value: unknown) => (typeof value === "string" ? value : "");
const num = (value: unknown) => (typeof value === "number" ? value : null);
const strs = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);

function diffs(value: unknown): Diff[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value
    .filter((d): d is Record<string, unknown> => Boolean(d) && typeof d === "object")
    .map((d) => ({
      path: str(d.path),
      old: typeof d.old === "string" ? d.old : null,
      new: str(d.new),
      cut: d.cut === true,
      more: num(d.more) ?? 0,
    }));
}

const SESSION_ACTIONS: Record<string, string> = {
  new: "A new agent session started",
  resumed: "The agent session was resumed",
  forgot: "The agent had lost this session; a new one started",
};

export interface SessionStats {
  context: { used: number; size: number } | null;
  plan: { content: string; status: string }[] | null;
}

export function toHubItems(events: readonly HubEvent[]): { items: Item[]; stats: SessionStats } {
  const items: Item[] = [];
  const steps = new Map<string, ToolStep>();
  const requests = new Map<string, Extract<Item, { kind: "request" }>>();
  const stats: SessionStats = { context: null, plan: null };
  let started: number | null = null; // the running turn's start, in ms
  const last = () => items[items.length - 1];

  for (const event of events) {
    const body = event.body ?? {};
    const key = String(event.seq);
    switch (event.kind) {
      case "turn_start": {
        started = event.at ? Date.parse(event.at) : null;
        const meta = [str(body.agent), str(body.model), str(body.effort), str(body.mode) && `mode ${str(body.mode)}`]
          .filter(Boolean);
        items.push({ kind: "user", key, text: str(body.prompt), answers: false, meta });
        break;
      }
      case "text":
      case "thought": {
        const kind = event.kind === "text" ? "answer" : "thought";
        const previous = last();
        if (previous?.kind === kind) previous.text += str(body.text);
        else if (kind === "answer") items.push({ kind: "answer", key, text: str(body.text), model: null });
        else items.push({ kind: "thought", key, text: str(body.text) });
        break;
      }
      case "tool": {
        // a call id is the agent's, kept by one attempt: another attempt (another process) may use it again
        const id = `${event.attempt ?? ""}/${str(body.id) || String(body.id ?? key)}`;
        const update: Partial<ToolStep> = {};
        if ("title" in body) update.title = str(body.title);
        if ("kind" in body) update.toolId = str(body.kind) || "tool";
        if ("input" in body) update.input = str(body.input);
        if ("locations" in body) update.locations = strs(body.locations);
        if ("diffs" in body) update.diffs = diffs(body.diffs);
        if ("status" in body) update.status = str(body.status);
        const known = steps.get(id);
        const step: ToolStep = known ?? { key: `tool:${id}`, toolId: "tool", arguments: {}, result: null };
        Object.assign(step, update);
        if ("output" in body || update.status === "completed" || update.status === "failed") {
          step.result = { text: "output" in body ? str(body.output) : (step.result?.text ?? ""),
            isError: step.status === "failed" };
        }
        if (known) break; // an update of a call already drawn
        steps.set(id, step);
        const previous = last();
        if (previous?.kind === "work") previous.steps.push(step);
        else items.push({ kind: "work", key, notes: [], steps: [step], model: null });
        break;
      }
      case "plan": {
        const entries = Array.isArray(body.entries)
          ? body.entries
              .filter((e): e is Record<string, unknown> => Boolean(e) && typeof e === "object")
              .map((e) => ({ content: str(e.content), status: str(e.status) }))
          : [];
        stats.plan = entries;
        const previous = last();
        if (previous?.kind === "plan") previous.entries = entries;
        else items.push({ kind: "plan", key, entries });
        break;
      }
      case "request": {
        const interaction = String(body.interaction_id ?? key);
        const options = Array.isArray(body.options)
          ? body.options.map((o) => (o && typeof o === "object" ? str((o as Record<string, unknown>).name) : "")).filter(Boolean)
          : [];
        const item: Extract<Item, { kind: "request" }> = {
          kind: "request", key, interaction, ask: str(body.title) || str(body.message), options, outcome: null,
        };
        requests.set(interaction, item);
        items.push(item);
        break;
      }
      case "answer": {
        const asked = requests.get(String(body.interaction_id ?? ""));
        if (asked) asked.outcome = str(body.outcome) || "answered";
        break;
      }
      case "turn_end": {
        const ended = event.at ? Date.parse(event.at) : null;
        const seconds = started !== null && ended !== null && !Number.isNaN(started) && !Number.isNaN(ended)
          ? Math.max(0, Math.round((ended - started) / 1000)) : null;
        items.push({ kind: "turn", key, turn: { outcome: str(body.outcome) || "done", why: str(body.why), seconds } });
        started = null;
        // a call still open when its turn ended never reported back (even with some output): it is not running
        for (const step of steps.values()) {
          if (step.status !== "completed" && step.status !== "failed" && !(step.result && !step.status)) {
            step.status = "unknown";
          }
        }
        break;
      }
      case "usage": {
        const used = num(body.used);
        const size = num(body.size);
        if (used !== null && size !== null) stats.context = { used, size };
        break;
      }
      case "session": {
        const text = SESSION_ACTIONS[str(body.action)];
        if (text) items.push({ kind: "notice", key, text });
        break;
      }
      case "truncated":
        items.push({ kind: "notice", key, text: `Some of this session's events were not kept (${str(body.group) || "budget"}).` });
        break;
      default:
        break; // setup, and kinds this page does not know yet
    }
  }
  return { items, stats };
}

/** Events merged by seq (a page read again, a stream that overlaps it), oldest first. */
export function mergeEvents(known: readonly HubEvent[], incoming: readonly HubEvent[]): HubEvent[] {
  if (!incoming.length) return known as HubEvent[];
  const bySeq = new Map<number, HubEvent>();
  for (const event of known) bySeq.set(event.seq, event);
  for (const event of incoming) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}
