// Server state through TanStack Query (V3-P102 §7.1). The principal's event stream (/api/events) tells when to
// read again: a job started or ended, a conversation changed. Nothing here polls while that stream is open.
import { QueryCache, QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { ApiError } from "@/api/client";
import {
  conversationActivity,
  listConversations,
  readAllEvents,
  readConversation,
  readServer,
  readSession,
} from "@/api/himari";
import { openStream } from "@/lib/sse";

export const queryClient: QueryClient = new QueryClient({
  // a read refused for the sign-in (it expired, or ended elsewhere): ask again whether we are signed in
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (error instanceof ApiError && error.status === 401 && query.queryKey[0] !== "session") {
        void queryClient.invalidateQueries({ queryKey: ["session"] });
      }
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

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
export const useConversations = () => useQuery({ queryKey: keys.conversations, queryFn: () => listConversations(100) });
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
      if (event === "hello") refresh(null);
      else refresh(data.conversation);
    });
  }, [client, enabled]);
}
