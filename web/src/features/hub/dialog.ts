// The one dialog that asks a device for a folder (V3-P103 §6.1, §6.2, §6.3): a new project, a folder added to a
// project, or either asked again after it ended. Any card or page opens it; the shell draws it.
import { useSyncExternalStore } from "react";

export interface DialogInit {
  kind: "create" | "add_folder";
  project?: string; // fixed for add_folder; the name typed for create
  runner?: string;
  path?: string | null; // local: the start location suggested; user: the folder to confirm again
  createDir?: boolean;
  agent?: string | null;
  again?: boolean; // asked again after it ended: the projects are read first, a user path is picked again
}

let current: DialogInit | null = null;
let generation = 0; // a new opening resets the dialog's own state
const listeners = new Set<() => void>();
let snapshot: { init: DialogInit | null; generation: number } = { init: null, generation };

function set(init: DialogInit | null) {
  current = init;
  if (init) generation += 1;
  snapshot = { init: current, generation };
  for (const listener of listeners) listener();
}

export const openProjectDialog = (init: DialogInit) => set(init);
export const closeProjectDialog = () => set(null);

export function useProjectDialog() {
  return useSyncExternalStore((listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, () => snapshot);
}
