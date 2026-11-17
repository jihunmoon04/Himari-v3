// HIMARI's own conversations (the server's /api/conversations…), as the old page uses them.
import { api, del, enc, get, post, put } from "./client";

export interface Session {
  authenticated: boolean;
  model: string | null;
}

export interface ModelOption {
  id: string;
  efforts: string[];
  default_effort: string | null;
  default: boolean;
}

export interface ServerInfo {
  model: string;
  sandbox: string;
  models: ModelOption[];
  streaming: boolean;
  hub_source?: string | null; // V3-P102 §5.1: the mcp.json server that is the hub (null: none here)
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  preview: string | null;
  created_at: string;
  last_activity_at: string;
  last_sequence: number;
}

export interface Job {
  running: boolean;
  generation: number;
  job_id: string | null;
  state: string | null;
  detail: string | null;
  error: string | null;
}

export interface Display {
  text: string;
}

export interface Waiting {
  call_id: string;
  question: string;
  display?: Display | null;
}

export interface ConversationDetail {
  conversation: ConversationSummary;
  job: Job;
  waiting: Waiting | null;
  continuable: boolean;
}

export interface ToolCallView {
  call_id: string;
  tool_id: string;
  arguments: Record<string, unknown>;
  display?: Display;
}

export interface EventView {
  sequence: number;
  kind: string; // user_message | assistant_final | assistant_tool_calls | tool_result | wake | …
  created_at: string;
  text?: string;
  display?: Display | null;
  model?: string | null;
  run_id?: string | null;
  call_id?: string;
  is_error?: boolean;
  calls?: ToolCallView[];
  answers_call?: string | null;
  answers_run?: string | null;
}

export interface EventsPage {
  items: EventView[];
  last_sequence: number;
}

export const readSession = () => get<Session>("/api/session");
export const signIn = (token: string) => post<void>("/api/session", { token });
export const signOut = () => del<void>("/api/session");
export const readServer = () => get<ServerInfo>("/api/server");

export const listConversations = (limit = 50, offset = 0) =>
  get<{ items: ConversationSummary[]; next_offset: number | null }>(`/api/conversations?limit=${limit}&offset=${offset}`);
export const conversationActivity = () => get<{ working: string[]; waiting: string[] }>("/api/conversations/activity");
export const createConversation = () => post<ConversationSummary>("/api/conversations", {});
export const readConversation = (id: string) => get<ConversationDetail>(`/api/conversations/${enc(id)}`);
export const readEvents = (id: string, after: number, limit = 500) =>
  get<EventsPage>(`/api/conversations/${enc(id)}/events?after=${after}&limit=${limit}`);
export const sendMessage = (id: string, text: string) =>
  post<Job>(`/api/conversations/${enc(id)}/messages`, { text });
export const cancelJob = (id: string) => post<Job>(`/api/conversations/${enc(id)}/cancel`);
export const resumeTurn = (id: string) => post<Job>(`/api/conversations/${enc(id)}/resume`);
export const renameConversation = (id: string, title: string | null) =>
  api<ConversationSummary>("PATCH", `/api/conversations/${enc(id)}`, { title });
export const setModel = (id: string, model: string | null, effort: string | null) =>
  put<unknown>(`/api/conversations/${enc(id)}/model`, { model, effort });

/** Every event after ``after``, page by page. */
export async function readAllEvents(id: string, after = 0): Promise<EventsPage> {
  const items: EventView[] = [];
  let cursor = after;
  for (;;) {
    const page = await readEvents(id, cursor);
    items.push(...page.items);
    if (page.items.length < 500 || page.last_sequence <= cursor) return { items, last_sequence: page.last_sequence };
    cursor = page.last_sequence;
  }
}

// -- a conversation's folders, to look at (V3-P75, V3-P99: the explorer only shows) ---------------------------------

export interface AttachedFolder {
  binding_id?: string | null;
  path: string;
  label?: string | null;
  note?: string | null;
  working?: boolean;
  own?: boolean;
}

export interface FolderListing {
  path: string;
  parent: string | null;
  entries: { name: string; kind: string; size?: number | null; modified?: number | null }[];
  next: string | null;
}

export interface ShownFile {
  path: string;
  size: number;
  kind: string; // text | image | other
  text: string | null;
  truncated: boolean;
}

export const conversationFolders = (id: string) => get<{ items: AttachedFolder[] }>(`/api/conversations/${enc(id)}/workspaces`);
export const browseFolder = (path: string) => get<FolderListing>(`/api/machine/list?path=${enc(path)}`);
export const showMachineFile = (path: string) => get<ShownFile>(`/api/machine/file?path=${enc(path)}`);
export const machineImageUrl = (path: string) => `/api/machine/image?path=${enc(path)}`;
export const machineDownloadUrl = (path: string) => `/api/machine/download?path=${enc(path)}`;
