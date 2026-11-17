// Files a conversation's next message points at (V3-P76 §3): added from the Files panel ("Ask about this file"),
// shown as chips above its composer, sent with the message as path and identity (the server reads the file; the
// page never sends its text). Per conversation, for this page only.
import { useSyncExternalStore } from "react";

export const MAX_REFERENCES = 8;

export interface Pointed {
  path: string;
  identity: string[];
  name: string;
}

let byConversation = new Map<string, Pointed[]>();
const listeners = new Set<() => void>();
const EMPTY: Pointed[] = [];

function set(next: Map<string, Pointed[]>) {
  byConversation = next;
  for (const listener of listeners) listener();
}

/** False when the message points at as many files as it may already. */
export function pointAt(conversation: string, file: Pointed): boolean {
  const now = byConversation.get(conversation) ?? [];
  if (now.some((p) => p.path === file.path)) { // chosen again: as the file is now (its identity may have changed)
    set(new Map(byConversation).set(conversation, now.map((p) => (p.path === file.path ? file : p))));
    return true;
  }
  if (now.length >= MAX_REFERENCES) return false;
  set(new Map(byConversation).set(conversation, [...now, file]));
  return true;
}

export function unpoint(conversation: string, paths: string[]) {
  const now = byConversation.get(conversation) ?? [];
  set(new Map(byConversation).set(conversation, now.filter((p) => !paths.includes(p.path))));
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export function usePointed(conversation: string): Pointed[] {
  return useSyncExternalStore(subscribe, () => byConversation.get(conversation) ?? EMPTY);
}
