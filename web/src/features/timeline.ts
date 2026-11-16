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

export type Item =
  | { kind: "user"; key: string; text: string; answers: boolean; meta?: string[] }
  | { kind: "answer"; key: string; text: string; model: string | null }
  | { kind: "thought"; key: string; text: string }
  | { kind: "work"; key: string; notes: string[]; steps: ToolStep[]; model: string | null }
  | { kind: "plan"; key: string; entries: { content: string; status: string }[] }
  | { kind: "question"; key: string; text: string; callKey: string }
  | { kind: "request"; key: string; interaction: string; ask: string; options: string[]; outcome: string | null }
  | { kind: "turn"; key: string; turn: Turn }
  | { kind: "notice"; key: string; text: string };
