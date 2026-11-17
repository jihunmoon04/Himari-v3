// The settings the classic page manages (V3-P102 W6c), on the same routes: instructions and the companion persona
// (V3-P69, V3-P89), memory, its suggestions and timeline (V3-P71, V3-P85), devices and quiet (V3-P83, V3-P88), the
// Tool servers' defaults (V3-P53), and the registered folders (V3-P74 §10.3).
import { del, enc, get, post, put, api } from "./client";

export interface InstructionsText {
  text: string;
  revision: number;
  updated_at: string | null;
  max_chars: number;
  companion?: boolean; // the persona only: whether the companion is on (it is told only then)
}

export const readInstructions = () => get<InstructionsText>("/api/instructions");
export const saveInstructions = (text: string, revision: number) => put<InstructionsText>("/api/instructions", { text, revision });
export const readPersona = () => get<InstructionsText>("/api/instructions/persona");
export const savePersona = (text: string, revision: number) => put<InstructionsText>("/api/instructions/persona", { text, revision });

export type Category = "user" | "preference" | "project" | "reference" | "loop";
export const CATEGORIES: Category[] = ["user", "preference", "project", "reference", "loop"];

export interface Note {
  id: number;
  category: Category;
  text: string;
  origin: string;
  source: { conversation: string; title: string | null; preview: string; in_trash: boolean } | null;
  revision: number;
  created_at: string;
  updated_at: string;
  due: string | null;
  overdue: boolean;
  valid_until: string | null;
  expired: boolean;
  kept_by_user: boolean;
}

export interface Memories {
  memories: Note[];
  max_count: number;
  max_chars: number;
  enabled: boolean;
  auto: { running: boolean; told: boolean; model: string | null; daily_tokens: number | null; today_tokens: number;
    last_applied_at: string | null; failed: number };
}

export interface NoteInput {
  category: Category;
  text: string;
  due?: string | null;
  valid_until?: string | null;
}

export interface Proposal {
  pass_id: string;
  item: number;
  kind: string;
  category: string | null;
  text: string | null;
  due: string | null;
  valid_until: string | null;
  created_at: string;
  conversation: { id: string; title: string | null; preview: string; in_trash: boolean };
}

export interface Episode {
  id: number;
  conversation: { id: string; title: string | null; preview: string };
  started_at: string;
  ended_at: string;
  text: string;
  origin: string;
  revision: number;
}

const zone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

export const readMemories = () => get<Memories>("/api/memories");
export const addNote = (note: NoteInput) => post<Note>("/api/memories", { ...note, time_zone: zone() });
export const editNote = (id: number, revision: number, note: NoteInput) =>
  put<Note>(`/api/memories/${id}`, { ...note, revision, time_zone: zone() });
export const deleteNote = (id: number, revision: number) => del<void>(`/api/memories/${id}?revision=${revision}`);
export const readProposals = () => get<{ proposals: Proposal[] }>("/api/memories/proposals");
/** ``withoutDates``: keep it with no due or end date (when the suggested ones have passed: ``stale_date``). */
export const decideProposal = (passId: string, item: number, decision: "keep" | "discard", withoutDates = false) =>
  post<Note | undefined>(`/api/memories/proposals/${enc(passId)}/${item}`, {
    decision, time_zone: zone(), ...(withoutDates ? { due: null, valid_until: null } : {}),
  });
export const readEpisodes = (cursor?: string | null) =>
  get<{ episodes: Episode[]; next: string | null }>(`/api/episodes?limit=30${cursor ? `&cursor=${enc(cursor)}` : ""}`);
export const editEpisode = (id: number, text: string, revision: number) => put<Episode>(`/api/episodes/${id}`, { text, revision });
export const deleteEpisode = (id: number, revision: number) => del<void>(`/api/episodes/${id}?revision=${revision}`);

export interface Device {
  device_id: string;
  name: string;
  paired: boolean;
  connected: boolean;
  current: boolean;
  created_at: string;
  paired_at: string | null;
  last_seen_at: string | null;
  pairing_expires_at: string | null;
}

export const readDevices = () => get<{ items: Device[]; enabled: boolean; quiet_until: string | null }>("/api/devices");
export const addDevice = (name: string) => post<Device & { code: string; expires_at: string }>("/api/devices", { name });
export const renameDevice = (id: string, name: string) => api<Device>("PATCH", `/api/devices/${enc(id)}`, { name });
export const revokeDevice = (id: string) => del<void>(`/api/devices/${enc(id)}`);
export const setQuiet = (until: Date | null) =>
  put<{ until: string | null }>("/api/presence/quiet", { until: until === null ? null : until.toISOString() });

export interface McpSettings {
  revision: string;
  sources: { source: string; default: boolean; available: boolean; tools: number }[];
}

export const readMcpSettings = () => get<McpSettings>("/api/settings/mcp");
export const setMcpDefault = (source: string, enabled: boolean, revision: string) =>
  put<McpSettings>(`/api/settings/mcp/${enc(source)}`, { enabled, revision });

export interface Workspace {
  id: string;
  path: string;
  label: string | null;
  note: string | null;
  created_at: string;
  attached: boolean;
  cloud_synced: boolean;
}

export const readWorkspaces = () => get<{ available: boolean; items: Workspace[] }>("/api/workspaces");
export const registerWorkspace = (path: string, label: string | null, note: string | null) =>
  post<Workspace>("/api/workspaces", { path, label, note });
export const editWorkspace = (id: string, label: string | null, note: string | null) =>
  api<Workspace>("PATCH", `/api/workspaces/${enc(id)}`, { label, note });
export const unregisterWorkspace = (id: string) => del<void>(`/api/workspaces/${enc(id)}`);
export const attachWorkspace = (conversation: string, workspaceId: string) =>
  post<unknown>(`/api/conversations/${enc(conversation)}/workspaces`, { workspace_id: workspaceId });
export const detachWorkspace = (conversation: string, bindingId: string) =>
  del<unknown>(`/api/conversations/${enc(conversation)}/workspaces/${enc(bindingId)}`);
export const setWorkingWorkspace = (conversation: string, bindingId: string) =>
  put<unknown>(`/api/conversations/${enc(conversation)}/workspaces/working`, { binding_id: bindingId });
