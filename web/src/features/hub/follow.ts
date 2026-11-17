// What this page sent to the hub, followed by its task id to the end (V3-P102 §5.2, the hub's ``task``, H1).
//
// ``new`` answers with no session (one exists only once the task runs), and a ``send`` goes on in a new session when
// the agent lost the one it was sent to: only the task says where it really runs. So every task sent from here is
// read again every FAST_MS (SLOW_MS after a minute, longer while the hub does not answer) until it is done or
// failed. The pages decide what to do with it: open the session it runs in, say it moved, show a task that failed
// before it ever ran. Kept for the whole page (not one view), so moving between pages loses nothing.
import { useSyncExternalStore } from "react";
import { ApiError } from "@/api/client";
import { readHubTask, type HubTask } from "@/api/hub";

const FAST_MS = 2_000;
const SLOW_MS = 5_000;
const FAST_FOR_MS = 60_000;
const DOWN_MS = [5_000, 10_000, 30_000];

export interface Followed {
  task: HubTask;
  from: number | null; // the session it was sent to; null: a new one (``new``)
  project: string | null;
  done: boolean; // done or failed, or lost: no longer read
  lost: string | null; // the hub does not know it (any more)
}

let followed: Followed[] = [];
const listeners = new Set<() => void>();
const timers = new Map<number, number>();

function set(next: Followed[]) {
  followed = next;
  for (const listener of listeners) listener();
}

function update(id: number, change: Partial<Followed>) {
  set(followed.map((f) => (f.task.id === id ? { ...f, ...change } : f)));
}

export const ended = (task: HubTask) => task.status === "done" || task.status === "failed";

function schedule(id: number, started: number, misses: number) {
  const wait = misses > 0 ? DOWN_MS[Math.min(misses - 1, DOWN_MS.length - 1)]!
    : Date.now() - started < FAST_FOR_MS ? FAST_MS : SLOW_MS;
  timers.set(id, window.setTimeout(() => void read(id, started, misses), wait));
}

async function read(id: number, started: number, misses: number) {
  timers.delete(id);
  if (!followed.some((f) => f.task.id === id)) return; // dismissed meanwhile
  try {
    const { task } = await readHubTask(id);
    const known = followed.find((f) => f.task.id === id);
    if (!known) return;
    const done = ended(task);
    update(id, { task, project: task.project ?? known.project, done });
    if (!done) schedule(id, started, 0);
  } catch (failure) {
    if (!followed.some((f) => f.task.id === id)) return;
    if (failure instanceof ApiError && (failure.status === 404 || failure.code === "hub_refused")) {
      update(id, { lost: failure.message, done: true });
      return;
    }
    schedule(id, started, misses + 1); // the hub, or this server, did not answer: later
  }
}

/** Follow a task this page just sent; ``from``: the session it was sent to (null for a new one). */
export function follow(task: HubTask, from: number | null, project: string | null) {
  if (followed.some((f) => f.task.id === task.id)) return;
  // what ``send`` answers names the session it was sent to, not yet the one it runs in: that is the task's to say
  set([...followed, { task: { ...task, session: null }, from, project, done: false, lost: null }]);
  schedule(task.id, Date.now(), 0);
}

/** Stop showing (and reading) a followed task. */
export function dismiss(id: number) {
  window.clearTimeout(timers.get(id));
  timers.delete(id);
  set(followed.filter((f) => f.task.id !== id));
}

/** Where a followed task runs: its session once the hub says so (null until then). */
export const runsIn = (f: Followed): number | null => f.task.session ?? null;

/** A task that ended without ever running in a session: what the page shows in its place. */
export const failedBeforeRunning = (f: Followed) =>
  f.lost !== null || (f.done && f.task.status === "failed" && runsIn(f) === null);

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const snapshot = () => followed;

/** Every task this page follows, oldest first. */
export function useFollowed(): readonly Followed[] {
  return useSyncExternalStore(subscribe, snapshot);
}

/** For the tests: the list as it is now. */
export const peekFollowed = (): readonly Followed[] => followed;

/** Forget everything and stop reading (a sign-out; the tests). */
export function resetFollowed() {
  for (const timer of timers.values()) window.clearTimeout(timer);
  timers.clear();
  set([]);
}
