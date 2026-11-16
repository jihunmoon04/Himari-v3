// The hub's user path as this server relays it (/api/hub/…, V3-P102 §6; the hub's contract user-api-v1). Every
// field may be missing: the server keeps only the contract's fields of the right type, and the hub only adds.
import { enc, get } from "./client";

export interface HubProject {
  name?: string;
  path?: string;
  default_agent?: string | null;
  preset?: string | null;
  profile?: string | null;
}

export interface HubAgent {
  name?: string;
  installed?: boolean;
  resting_until?: string | null;
  modes?: string[];
}

export interface HubRunner {
  name?: string;
  agents?: string[];
  projects?: string[];
  connected?: boolean;
}

export interface HubProjects {
  projects?: HubProject[];
  profiles?: { name?: string; agent?: string; description?: string; fresh?: boolean }[];
  models?: Record<string, string[]>;
  efforts?: Record<string, string[]>;
  agents?: HubAgent[];
  runners?: HubRunner[];
}

export type Activity = "running" | "waiting" | "idle" | "closed";

export interface HubSession {
  id: number;
  project?: string;
  agent?: string;
  runner?: string;
  runner_connected?: boolean;
  profile?: string | null;
  state?: "open" | "closed";
  closed_reason?: string | null;
  activity?: Activity | string;
  task?: number | null;
  last_task?: number | null;
  model?: string | null;
  effort?: string | null;
  context?: { used?: number; size?: number } | null;
  created_at?: string;
  closed_at?: string | null;
  first_seq?: number;
  last_seq?: number;
  live?: boolean;
  queued?: number;
  current?: boolean;
  updated_at?: string; // H7, once the hub has it
}

export interface HubPending {
  id: number;
  task_id: number;
  kind: "permission" | "question";
  asks: string | null;
  task?: { project?: string | null; agent?: string | null; runner?: string | null; prompt?: string | null };
}

export interface HubAttempt {
  id?: number;
  task_id?: number;
  agent?: string;
  runner?: string;
  started_at?: string;
  ended_at?: string | null;
  outcome?: string | null;
  why?: string;
}

export interface HubSessionDetail extends Omit<HubSession, "queued"> {
  pending: HubPending[];
  attempts: HubAttempt[];
  queued: { id?: number; prompt?: string; created_at?: string }[];
}

export interface HubEvent {
  seq: number;
  kind: string;
  attempt?: number;
  at?: string;
  truncated?: boolean;
  body: Record<string, unknown>;
}

export interface HubEventsPage {
  events: HubEvent[];
  first_seq: number | null;
  last_seq: number | null;
  next: number | null;
  more: boolean;
  reset: boolean;
}

export const readHubProjects = () => get<HubProjects>("/api/hub/projects");
export const readHubSession = (id: number) => get<HubSessionDetail>(`/api/hub/sessions/${id}`);
export const readHubEvents = (id: number, after: number, limit = 200) =>
  get<HubEventsPage>(`/api/hub/sessions/${id}/events?after=${after}&limit=${limit}`);
export const readHubPending = () =>
  get<{ enabled: boolean; reachable: boolean; pending: HubPending[] }>("/api/hub/pending");
export const listHubSessions = (closed = false) =>
  get<{ sessions: HubSession[] }>(`/api/hub/sessions?closed=${closed}&limit=500`);
export const hubStreamUrl = () => "/api/hub/stream";
export const hubSessionStreamUrl = (id: number, after: number) => `/api/hub/sessions/${enc(String(id))}/stream?after=${after}`;
