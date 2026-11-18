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

/** How a device takes project requests (V3-P103 §2): ``off`` (none; removing still works), ``local`` (its person
 *  picks the folder, with a pairing code), ``user`` (the user path's approval is enough, under ``roots``). */
export interface HubApprove {
  mode?: "off" | "local" | "user" | string;
  roots?: string[];
  why?: string | null;
}

export interface HubRunner {
  name?: string;
  agents?: string[];
  projects?: string[];
  connected?: boolean;
  approve?: HubApprove; // V3-P103
  folders?: Record<string, string>; // project name: its folder on this device
}

/** H8: a name a device reported that a reservation held back (whether the device is connected or not). */
export interface HubHeld {
  project?: string;
  runner?: string;
  created_at?: string;
}

export interface HubProjects {
  projects?: HubProject[];
  profiles?: { name?: string; agent?: string; description?: string; fresh?: boolean }[];
  models?: Record<string, string[]>;
  efforts?: Record<string, string[]>;
  agents?: HubAgent[];
  runners?: HubRunner[];
  held?: HubHeld[]; // H8; missing from a hub without it
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

// -- projects and device folders (V3-P103 §4) --------------------------------------------------------------------

export type ProjectRequestKind = "create" | "add_folder" | "remove_folder";
export type ProjectRequestState =
  | "waiting_user" | "waiting_device" | "applying" | "done" | "refused" | "expired" | "failed" | "cancelled" | "lost";

/** A project request as the user path gives it (REST and the list stream alike). ``rev`` grows with every change;
 *  ``same_name_on`` is never used (the hub computes it from the holds, not the request). */
export interface HubProjectRequest {
  id: number;
  rev: number;
  state: ProjectRequestState | string;
  kind?: ProjectRequestKind | string;
  project?: string;
  runner?: string;
  origin?: "user" | "model" | string;
  path?: string | null;
  create_dir?: boolean;
  agent?: string | null;
  why?: string | null;
  code?: string | null;
  mode?: string | null;
  result_path?: string | null;
  reason?: string | null;
  resolving?: boolean; // H17
  created_at?: string;
  decided_at?: string | null;
  expires_at?: string | null;
  claimed_at?: string | null;
  ended_at?: string | null;
  same_name_on?: string[];
}

export interface ProjectRequestBody {
  kind: "create" | "add_folder";
  project: string;
  runner: string;
  path?: string;
  create_dir?: boolean;
  agent?: string;
}

export interface ClientKey {
  client_key: string;
  key_time: string;
}

export const readProjectRequests = (open: boolean) =>
  get<{ requests: HubProjectRequest[]; missing: number[] | null }>(`/api/hub/project-requests?open=${open}`);
/** H12: these requests whatever their state (at most 50); ``missing`` null: the hub does not say (no H12). */
export const readProjectRequestsByIds = (ids: readonly number[]) =>
  get<{ requests: HubProjectRequest[]; missing: number[] | null }>(`/api/hub/project-requests?ids=${ids.join(",")}`);
export const makeProjectRequest = (body: ProjectRequestBody & Partial<ClientKey>) =>
  post<{ request: HubProjectRequest }>("/api/hub/project-requests", body);
export const answerProjectRequest = (id: number, approve: boolean) =>
  post<{ request: HubProjectRequest }>(`/api/hub/project-requests/${id}/answer`, { approve });
export const cancelProjectRequest = (id: number) =>
  post<{ request: HubProjectRequest }>(`/api/hub/project-requests/${id}/cancel`);
export const resolveProjectRequest = (id: number, force?: { outcome: "applied" | "not_applied" }) =>
  post<{ request: HubProjectRequest }>(`/api/hub/project-requests/${id}/resolve`,
    force ? { force: true, outcome: force.outcome } : {});
export const removeProjectFolder = (body: { project: string; runner: string } & Partial<ClientKey>) =>
  post<{ request: HubProjectRequest }>("/api/hub/projects/remove-folder", body);
/** Only the settings given change; null clears one. */
export const setProjectSettings = (project: string, settings: { agent?: string | null; profile?: string | null;
  preset?: string | null }) => post<{ project: HubProject }>("/api/hub/projects/settings", { project, ...settings });
export const adoptHeldFolder = (project: string, runner: string) =>
  post<{ project: HubProject }>("/api/hub/projects/adopt", { project, runner });
export const readRunnerDirs = (runner: string, path: string | null) =>
  post<{ path: string | null; dirs: string[] }>("/api/hub/runner-dirs", path === null ? { runner } : { runner, path });
export const newClientKey = () => post<ClientKey>("/api/hub/client-key");
