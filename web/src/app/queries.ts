// Server state through TanStack Query (V3-P102 §7.1). The principal's event stream (/api/events) tells when to
// read again: a job started or ended, a conversation changed. Nothing here polls while that stream is open.
import { QueryClient, useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { ApiError, onUnauthorized } from "@/api/client";
import {
  conversationActivity,
  listConversations,
  readAllEvents,
  readConversation,
  readServer,
  readSession,
} from "@/api/himari";
import { readHubPending, readHubProjects, readHubSession, readHubTask } from "@/api/hub";
import { onListSignal, onProjectSignals } from "@/features/hub/list";
import { requests } from "@/features/hub/requests";
import { openStream } from "@/lib/sse";

export const queryClient: QueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

// any call refused for the sign-in (a read or a send): ask again whether we are signed in
onUnauthorized(() => void queryClient.invalidateQueries({ queryKey: ["session"] }));

export const keys = {
  session: ["session"] as const,
  server: ["server"] as const,
  conversations: ["conversations"] as const,
  activity: ["activity"] as const,
  detail: (id: string) => ["conversation", id] as const,
  events: (id: string) => ["conversation", id, "events"] as const,
};

export const useSession = () => useQuery({ queryKey: keys.session, queryFn: readSession, staleTime: 60_000 });
export const useServer = (enabled: boolean) =>
  useQuery({ queryKey: keys.server, queryFn: readServer, enabled, staleTime: 300_000 });
export const useConversations = () =>
  useInfiniteQuery({
    queryKey: keys.conversations,
    queryFn: ({ pageParam }) => listConversations(50, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
  });
export const useActivity = () => useQuery({ queryKey: keys.activity, queryFn: conversationActivity });

export const useDetail = (id: string) =>
  useQuery({
    queryKey: keys.detail(id),
    queryFn: () => readConversation(id),
    // while a job runs the live stream and /api/events lead; this is the slow safety net
    refetchInterval: (query) => (query.state.data?.job.running ? 5_000 : false),
  });

export const useEvents = (id: string) => useQuery({ queryKey: keys.events(id), queryFn: () => readAllEvents(id) });

/** The principal's event stream: what changed, so the right reads happen again. */
export function usePrincipalEvents(enabled: boolean) {
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    const refresh = (conversation: unknown) => {
      void client.invalidateQueries({ queryKey: keys.activity });
      void client.invalidateQueries({ queryKey: keys.conversations });
      if (typeof conversation === "string") {
        void client.invalidateQueries({ queryKey: keys.detail(conversation) });
      }
    };
    return openStream("/api/events", ["hello", "activity", "job", "conversation"], ({ event, data }) => {
      if (event === "hello") {
        // (re)connected: nothing is replayed, so whatever is open is read again
        refresh(null);
        void client.invalidateQueries({ queryKey: ["conversation"] });
      }
      else refresh(data.conversation);
    });
  }, [client, enabled]);
}

// -- the hub (V3-P102 W2) ---------------------------------------------------------------------------------------

export const hubKeys = {
  projects: ["hub", "projects"] as const,
  pending: ["hub", "pending"] as const,
  session: (id: number) => ["hub", "session", id] as const,
  task: (id: number) => ["hub", "task", id] as const,
};

const TASK_POLL_MS = 15_000; // V3-P102 §5.1: a running task is read this often even without a hint

/** A task followed to its end (H1): read every 15 s until done or failed; ``null``: nothing to read. */
export const useHubTask = (id: number | null, enabled: boolean) =>
  useQuery({
    queryKey: hubKeys.task(id ?? 0),
    queryFn: () => readHubTask(id!),
    enabled: enabled && id !== null,
    staleTime: 5_000,
    retry: (count, error) => !(error instanceof ApiError && error.code === "hub_refused") && count < 2,
    refetchInterval: (query) => {
      const status = query.state.data?.task.status;
      if (status === "done" || status === "failed") return false;
      if (query.state.error instanceof ApiError && query.state.error.code === "hub_refused") return false;
      return TASK_POLL_MS;
    },
  });

// V3-P103 §4.1: after a lost answer to a settings change or an adoption, the projects are in doubt for the whole page
// (every control of them, and over leaving a page) until a read of them that started after that moment comes in.
let projectReads = 0; // reads started by readProjectsNow
let projectsDoubtedSince: number | null = null; // the read count when the answer was lost
const doubtListeners = new Set<() => void>();
const tellDoubt = () => { for (const listener of doubtListeners) listener(); };

/** A lost answer to a change of the projects: their controls stop until the projects are read again. */
export function doubtProjects() {
  projectsDoubtedSince = projectReads;
  tellDoubt();
}

/** Signed out: nothing of it stays. */
export function clearProjectsDoubt() {
  projectsDoubtedSince = null;
  tellDoubt();
}

/** The projects read now: a read of its own (one on its way may have started before a change), not the cache.
 *  True when it went through; it then ends a doubt that began before it started. */
export async function readProjectsNow(client: QueryClient): Promise<boolean> {
  const started = ++projectReads;
  try {
    await client.cancelQueries({ queryKey: hubKeys.projects });
    await client.fetchQuery({ queryKey: hubKeys.projects, queryFn: readHubProjects, staleTime: 0 });
  } catch {
    return false;
  }
  if (projectsDoubtedSince !== null && started > projectsDoubtedSince) clearProjectsDoubt();
  return true;
}

/** Whether this server has the hub set up: "off" (404, nothing of it is shown), "down" (it did not answer), "on".
 *  ``doubted``: a change of them may have been done and they were not read since (§4.1). */
export function useHubProjects() {
  const notSetUp = (error: unknown) => error instanceof ApiError && error.status === 404;
  const query = useQuery({
    queryKey: hubKeys.projects,
    queryFn: readHubProjects,
    staleTime: 60_000,
    // a hub that did not answer is asked again (with backoff, then every 30 s) until it does; "not set up" is final
    retry: (count, error) => !notSetUp(error) && count < 3,
    retryDelay: (count) => Math.min(30_000, 2_000 * 2 ** count),
    refetchInterval: (q) => (q.state.error && !notSetUp(q.state.error) ? 30_000 : false),
  });
  const doubted = useSyncExternalStore((listener) => {
    doubtListeners.add(listener);
    return () => doubtListeners.delete(listener);
  }, () => projectsDoubtedSince !== null);
  const status = query.isLoading ? "loading"
    : query.error instanceof ApiError && query.error.status === 404 ? "off"
    : query.error ? "down" : "on";
  return { ...query, status, doubted } as const;
}

export const useHubSessionDetail = (id: number) =>
  useQuery({ queryKey: hubKeys.session(id), queryFn: () => readHubSession(id), retry: false });

// what waits for the user may have changed (a session's activity or task, the list read whole): read it again
onListSignal(() => void queryClient.invalidateQueries({ queryKey: hubKeys.pending }));

// V3-P103 §5: a request that changed is merged by its rev; on ``projects``, a (re)connect and every tick of the
// polling fallback the projects and the open and followed requests are read again
const rereadProjects = () => {
  void queryClient.invalidateQueries({ queryKey: hubKeys.projects });
  void requests.refresh();
};
onProjectSignals({ request: (data) => requests.merge(data), projects: rereadProjects, connected: rereadProjects,
  polled: rereadProjects });

export const useHubPending = (enabled: boolean) =>
  useQuery({ queryKey: hubKeys.pending, queryFn: readHubPending, enabled, refetchInterval: 30_000 });
