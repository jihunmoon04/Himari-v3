// Short notices for the whole page (moved to the trash, restored, could not do it), each with an optional action
// such as Undo. They go on their own after a while.
import { useSyncExternalStore } from "react";

export interface Toast {
  id: number;
  text: string;
  tone: "ok" | "error" | "info";
  action?: { label: string; run: () => void };
}

let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();
const LIFE_MS = 8_000;

function set(list: Toast[]) {
  toasts = list;
  for (const listener of listeners) listener();
}

export function dismissToast(id: number) {
  set(toasts.filter((t) => t.id !== id));
}

export function toast(text: string, tone: Toast["tone"] = "ok", action?: Toast["action"]) {
  const id = next++;
  set([...toasts.slice(-3), { id, text, tone, action }]);
  window.setTimeout(() => dismissToast(id), action ? LIFE_MS * 1.5 : LIFE_MS);
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};
const snapshot = () => toasts;

export const useToasts = () => useSyncExternalStore(subscribe, snapshot);
