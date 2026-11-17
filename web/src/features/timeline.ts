// V3-P102 §7.4: what the timeline draws, from HIMARI's events (himari/items.ts) or a hub session's (hub/items.ts).
// The timeline knows only these kinds.

export interface Diff {
  path: string;
  old: string | null; // null: a new file
  new: string;
  cut: boolean; // the hub cut a side to fit
  more: number; // further diffs of the call the hub left out
}

export interface ToolStep {
  key: string;
  toolId: string; // HIMARI: the tool's id; hub: its kind
  title?: string; // hub: the agent's own title of the call
  arguments: Record<string, unknown>;
  input?: string; // hub: the call's input as the agent gave it
  locations?: string[];
  diffs?: Diff[];
  status?: string; // hub: pending | in_progress | completed | failed
  result: { text: string; isError: boolean } | null;
}

export interface Turn {
  outcome: string;
  why: string;
  seconds: number | null;
}

/** Work HIMARI handed to the hub (V3-P102 §5.1): what its ``hub_delegate`` call asked and what it answered. The
 *  card follows the task by its id from there. */
export interface Delegation {
  taskId: number | null; // null: the hub refused it (``error`` says why) or the answer could not be read
  project: string;
  agent: string | null;
  prompt: string;
  session: number | null; // the session it was sent to, when HIMARI named one
  status: string | null; // as the hub answered the call (the card reads the task again)
  error: string | null;
}

/** A file shown in the timeline: what a message attached, or what a Tool gave the user (V3-P66, V3-P67). */
export interface Shown {
  key: string;
  name: string;
  size: number | null;
  url: string | null; // its download (null: no conversation to fetch it from, or it expired)
  preview: string | null; // the image copy the server made (null: not an image, or no copy)
  width: number | null; // the copy's box, kept before it loads
  height: number | null;
}

export type Item =
  | { kind: "user"; key: string; text: string; answers: boolean; meta?: string[]; files?: Shown[]; references?: string[] }
  | { kind: "answer"; key: string; text: string; model: string | null }
  | { kind: "thought"; key: string; text: string }
  | { kind: "work"; key: string; notes: string[]; steps: ToolStep[]; model: string | null }
  | { kind: "plan"; key: string; entries: { content: string; status: string }[] }
  | { kind: "question"; key: string; text: string; callKey: string }
  | { kind: "request"; key: string; interaction: string; ask: string; options: string[]; outcome: string | null }
  | { kind: "turn"; key: string; turn: Turn }
  | { kind: "delegation"; key: string; delegation: Delegation }
  | { kind: "files"; key: string; files: Shown[] }
  | { kind: "memory"; key: string; run: string; call: string; action: string; text: string; undo: boolean }
  | { kind: "notice"; key: string; text: string };
