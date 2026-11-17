// The hub's open sessions for the whole page (V3-P102 §6, §7.3): one /api/hub/stream for every view that needs
// the list. ``sessions`` replaces the list, ``changed`` merges by id, ``hub`` says whether the hub is reachable.
// When the stream cannot open (the hub has no stream left, or the server refused), the list is read every
// POLL_MS and the stream is tried again after RETRY_MS. While a stream that broke off is being connected again by
// the browser, the list is read every POLL_MS too, until it is back.
//
// V3-P103 §4.2, §5: the same stream carries the hub's project requests (``project_request``, one changed request)
// and ``projects`` (read the projects again). Whoever listens for them (onProjectSignals) is also told when the
// stream (re)connects and on every tick of the polling fallback, so that one timer reads everything while the
// stream is away and stops when it is back.
import { useEffect, useSyncExternalStore } from "react";
import { hubStreamUrl, listHubSessions, type HubSession } from "@/api/hub";

const POLL_MS = 10_000;
const RETRY_MS = 30_000;

export interface HubList {
  ready: boolean;
  reachable: boolean;
  sessions: HubSession[]; // newest first
}

let state: HubList = { ready: false, reachable: true, sessions: [] };
const listeners = new Set<() => void>();
let signal: (() => void) | null = null;

/** Called when what waits for the user may have changed: the list came whole (a (re)connect, a poll), or a
 *  session's activity or task changed (§7.3). */
export function onListSignal(handler: () => void) {
  signal = handler;
}

export interface ProjectSignals {
  request?: (data: Record<string, unknown>) => void; // one request changed
  projects?: () => void; // read the projects again
  connected?: () => void; // the stream opened or came back: what happened meanwhile may be lost
  polled?: () => void; // a tick of the polling fallback
}
let projectSignals: ProjectSignals = {};

export function onProjectSignals(handlers: ProjectSignals) {
  projectSignals = handlers;
}

let users = 0;
let stop: (() => void) | null = null;

function set(next: Partial<HubList>) {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

const byNewest = (sessions: Iterable<HubSession>) => [...sessions].sort((a, b) => b.id - a.id);

function replace(sessions: unknown) {
  if (!Array.isArray(sessions)) return;
  set({ ready: true, reachable: true, sessions: byNewest(sessions as HubSession[]) });
  signal?.();
}

function merge(changed: unknown) {
  if (!Array.isArray(changed)) return;
  const byId = new Map(state.sessions.map((s) => [s.id, s]));
  let moved = false;
  for (const session of changed as HubSession[]) {
    const before = byId.get(session.id);
    moved ||= !before || before.activity !== session.activity || before.task !== session.task;
    byId.set(session.id, session);
  }
  set({ sessions: byNewest(byId.values()) });
  if (moved) signal?.();
}

/** The stream for the page (exported for its tests); the returned function ends it. */
export function openList() {
  let source: EventSource | null = null;
  let poll: number | undefined;
  let retry: number | undefined;
  let ended = false;
  let epoch = 0; // bumped when the stream is tried again: a poll answered after that is stale

  const read = async () => {
    const asked = epoch;
    try {
      const sessions = (await listHubSessions()).sessions;
      if (!ended && asked === epoch) replace(sessions);
    } catch {
      if (!ended && asked === epoch) set({ reachable: false });
    }
    if (!ended && asked === epoch) projectSignals.polled?.();
  };

  const connect = () => {
    if (ended) return;
    epoch += 1;
    window.clearInterval(poll);
    poll = undefined;
    source = new EventSource(hubStreamUrl(), { withCredentials: true });
    const on = (name: string, apply: (data: Record<string, unknown>) => void) =>
      source?.addEventListener(name, (message) => {
        try {
          apply(JSON.parse((message as MessageEvent<string>).data) as Record<string, unknown>);
        } catch {
          // a frame that is not JSON: skipped
        }
      });
    on("sessions", (data) => replace(data.sessions));
    on("changed", (data) => merge(data.sessions));
    on("hub", (data) => set({ reachable: data.reachable === true }));
    on("project_request", (data) => projectSignals.request?.(data));
    on("projects", () => projectSignals.projects?.());
    source.addEventListener("open", () => {
      if (poll !== undefined) { // back after a break: the reads by asking stop (never two at once)
        epoch += 1;
        window.clearInterval(poll);
        poll = undefined;
      }
      projectSignals.connected?.();
    });
    source.addEventListener("error", () => {
      if (source?.readyState !== EventSource.CLOSED) { // broke off: the browser connects it again by itself
        if (poll === undefined) poll = window.setInterval(() => void read(), POLL_MS); // ask meanwhile
        return;
      }
      // refused before it started (no stream left, signed out, not set up): read by asking, try again later
      source = null;
      void read();
      poll = window.setInterval(() => void read(), POLL_MS);
      retry = window.setTimeout(connect, RETRY_MS);
    });
  };

  connect();
  return () => {
    ended = true;
    source?.close();
    window.clearInterval(poll);
    window.clearTimeout(retry);
  };
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The hub's open sessions, live; ``enabled`` false (the hub is not set up here) opens nothing. */
export function useHubList(enabled: boolean): HubList {
  useEffect(() => {
    if (!enabled) return;
    users += 1;
    if (users === 1) stop = openList();
    return () => {
      users -= 1;
      if (users === 0) {
        stop?.();
        stop = null;
      }
    };
  }, [enabled]);
  return useSyncExternalStore(subscribe, () => state);
}

/** Sessions grouped by project, projects by name. */
export function byProject(sessions: readonly HubSession[], projects: readonly string[] = []) {
  const groups = new Map<string, HubSession[]>();
  for (const name of projects) groups.set(name, []);
  for (const session of sessions) {
    const name = session.project ?? "?";
    groups.set(name, [...(groups.get(name) ?? []), session]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}
