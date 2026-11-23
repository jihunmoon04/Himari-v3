// V3-P102 §7.4: what the timeline draws, from HIMARI's events (himari/items.ts). The timeline knows only these kinds.

export interface ToolStep {
  key: string;
  toolId: string;
  arguments: Record<string, unknown>;
  result: { text: string; isError: boolean } | null;
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
  | { kind: "user"; key: string; text: string; files?: Shown[]; references?: string[] }
  | { kind: "answer"; key: string; text: string; model: string | null }
  | { kind: "work"; key: string; notes: string[]; steps: ToolStep[]; model: string | null }
  | { kind: "files"; key: string; files: Shown[] }
  | { kind: "memory"; key: string; run: string; call: string; action: string; text: string; undo: boolean }
  | { kind: "notice"; key: string; text: string };
