// The hub's user path as this server relays it (/api/hub/…, V3-P102 §6; the hub's contract user-api-v1). Every
// field may be missing: the server keeps only the contract's fields of the right type, and the hub only adds.
import { enc, get, post, put } from "./client";

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
  title?: string | null; // H2: the name the user gave it
}

export interface HubChoice {
  value: string | number | boolean;
  title?: string | null;
  description?: string | null;
}

export interface HubField {
  name: string;
  title?: string | null;
  description?: string | null;
  multiple?: boolean;
  choices?: HubChoice[];
}

export interface HubPending {
  id: number; // the request's (interaction's) id: a request event's interaction_id
  task_id: number;
  kind: "permission" | "question";
  asks: string | null;
  task?: { project?: string | null; agent?: string | null; runner?: string | null; prompt?: string | null };
  options?: { option_id: string; name?: string | null; kind?: string | null }[]; // a permission's choices
  fields?: HubField[]; // a question's
}

/** A task (user-api-v1 §3.1); followed with ``task`` (H1, §4.5) it also says where it really runs. */
export interface HubTask {
  id: number;
  status?: "queued" | "running" | "waiting" | "done" | "failed" | string;
  agent?: string | null;
  runner?: string | null;
  profile?: string | null;
  model?: string | null;
  effort?: string | null;
  mode?: string | null;
  prompt?: string;
  created_at?: string;
  finished_at?: string | null;
  session?: number | null; // send: the session it was sent to; task: the one it really runs in (null: none yet)
  project?: string | null;
  result?: string | null;
  result_truncated?: boolean;
  pending?: HubPending[];
}

/** What a message may choose for itself (only what the hub's ``projects`` offers for that agent). */
export interface MessageChoices {
  model?: string;
  effort?: string;
  mode?: string;
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
export const readHubTask = (id: number) => get<{ task: HubTask }>(`/api/hub/tasks/${id}`);
export const sendToHubSession = (id: number, prompt: string, choices: MessageChoices = {}) =>
  post<{ task: HubTask }>(`/api/hub/sessions/${id}/messages`, { prompt, ...choices });
export const startHubSession = (project: string, prompt: string, choices: MessageChoices & { agent?: string } = {}) =>
  post<{ task: HubTask }>("/api/hub/sessions", { project, prompt, ...choices });
export const closeHubSession = (id: number) => post<{ closed: number; cancelled: number[] }>(`/api/hub/sessions/${id}/close`);
/** H2: open a closed session again (the hub says why when it cannot). */
export const reopenHubSession = (id: number) => post<{ reopened: number }>(`/api/hub/sessions/${id}/reopen`);
/** H2: the session's name; empty or null clears it. */
export const renameHubSession = (id: number, title: string | null) =>
  put<{ session: number; title: string | null }>(`/api/hub/sessions/${id}/title`, { title });
/** H2: the closed sessions, the latest closed first. */
export const listClosedHubSessions = (limit = 50) =>
  get<{ sessions: HubSession[] }>(`/api/hub/sessions?state=closed&limit=${limit}`);
export const cancelHubTask = (id: number) => post<void>(`/api/hub/tasks/${id}/cancel`);
/** Exactly one of: a permission's option, a question's content, or a refusal. */
export type HubAnswer = { option_id: string } | { content: Record<string, unknown> } | { refuse: true };
export const answerHubPending = (id: number, answer: HubAnswer) => post<void>(`/api/hub/pending/${id}/answer`, answer);
export const hubStreamUrl = () => "/api/hub/stream";
export const hubSessionStreamUrl = (id: number, after: number) => `/api/hub/sessions/${enc(String(id))}/stream?after=${after}`;
