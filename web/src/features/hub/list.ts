// The hub's open sessions for the whole page (V3-P102 §6, §7.3): one /api/hub/stream for every view that needs
// the list. ``sessions`` replaces the list, ``changed`` merges by id, ``hub`` says whether the hub is reachable.
// When the stream cannot open (the hub has no stream left, or the server refused), the list is read every
// POLL_MS and the stream is tried again after RETRY_MS.
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
}

function merge(changed: unknown) {
  if (!Array.isArray(changed)) return;
  const byId = new Map(state.sessions.map((s) => [s.id, s]));
  for (const session of changed as HubSession[]) byId.set(session.id, session);
  set({ sessions: byNewest(byId.values()) });
}

function open() {
  let source: EventSource | null = null;
  let poll: number | undefined;
  let retry: number | undefined;
  let ended = false;

  const read = async () => {
    try {
      replace((await listHubSessions()).sessions);
    } catch {
      set({ reachable: false });
    }
  };

  const connect = () => {
    if (ended) return;
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
    source.addEventListener("error", () => {
      if (source?.readyState !== EventSource.CLOSED) return; // the browser reconnects by itself
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
    if (users === 1) stop = open();
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
