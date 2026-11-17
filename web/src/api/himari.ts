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

/** A file of a conversation (an upload, or one a Tool presented), V3-P66. */
export interface FileView {
  file_id: string;
  version: number;
  name: string | null;
  media_type?: string | null;
  size?: number | null;
  expired?: boolean;
  image?: { state?: string; width?: number | null; height?: number | null; preview?: { width: number; height: number } | null } | null;
  screen?: boolean;
}

export interface ReferenceView {
  path: string;
  name: string;
  whole: boolean;
  start_line?: number | null;
  end_line?: number | null;
}

export interface MemoryView {
  action: string; // saved | updated | forgot | already
  id: string | null;
  text: string | null;
  undo: boolean;
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
  attachments?: FileView[]; // a user message's files
  references?: ReferenceView[]; // and what it pointed at
  presented_files?: FileView[]; // a Tool result's files given to the user
  images?: FileView[]; // and its images
  memory?: MemoryView | null; // what a memory call changed
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
/** A whole file (``identity`` from the explorer) the message points at; the server reads it, never the page. */
export interface ReferenceOut {
  path: string;
  identity: string[];
}

export const sendMessage = (id: string, text: string, attachments: string[] = [], references: ReferenceOut[] = []) =>
  post<Job>(`/api/conversations/${enc(id)}/messages`, {
    text, ...(attachments.length ? { attachments } : {}), ...(references.length ? { references } : {}),
  });
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
  identity?: string[];
  path: string;
  size: number;
  kind: string; // text | image | other
  text: string | null;
  truncated: boolean;
}

export const conversationFolders = (id: string) => get<{ items: AttachedFolder[] }>(`/api/conversations/${enc(id)}/workspaces`);
export const browseFolder = (path: string, after?: string | null) =>
  get<FolderListing>(`/api/machine/list?path=${enc(path)}${after ? `&after=${enc(after)}` : ""}`);
/** Where a path leads (a link followed): its real path and kind. */
export const statMachinePath = (path: string) => get<{ path: string; kind: string }>(`/api/machine/stat?path=${enc(path)}`);
export const showMachineFile = (path: string) => get<ShownFile>(`/api/machine/file?path=${enc(path)}`);
export const machineImageUrl = (path: string) => `/api/machine/image?path=${enc(path)}`;
export const machineDownloadUrl = (path: string) => `/api/machine/download?path=${enc(path)}`;

// -- a conversation's own tools (V3-P102 W6a): its name, the trash, its model, its Tool servers, compacting ---------

export interface DeleteNotice {
  servers: string[]; // the external servers it called, whose own data stays
  unconfigured: string[];
  running: boolean;
  trash_days: number;
  memory_count: number;
  episode_count: number;
}

export interface TrashedConversation extends ConversationSummary {
  trashed_at: string;
  purge_after: string;
}

export interface ModelChoice {
  model: string; // what the next turn uses
  effort: string | null;
  chosen: { model: string | null; effort: string | null } | null; // what this conversation chose (null: defaults)
  fallback: boolean;
}

export interface ToolSource {
  source: string;
  enabled: boolean;
  default: boolean;
  choice: boolean | null; // this conversation's own choice (null: the default)
  available: boolean;
  tools: string[];
  problems: string[];
}

export const readDeleteNotice = (id: string) => get<DeleteNotice>(`/api/conversations/${enc(id)}/delete-notice`);
export const trashConversation = (id: string) =>
  post<{ trashed_at: string; purge_after: string }>(`/api/conversations/${enc(id)}/trash`);
export const restoreConversation = (id: string) => post<void>(`/api/conversations/${enc(id)}/restore`);
export const purgeConversation = (id: string) => del<void>(`/api/conversations/${enc(id)}`);
export const listTrash = (limit = 50, offset = 0) =>
  get<{ items: TrashedConversation[]; next_offset: number | null }>(`/api/conversations?trashed=1&limit=${limit}&offset=${offset}`);
export const readModelChoice = (id: string) => get<ModelChoice>(`/api/conversations/${enc(id)}/model`);
export const saveModelChoice = (id: string, model: string | null, effort: string | null) =>
  put<ModelChoice>(`/api/conversations/${enc(id)}/model`, { model, effort });
export const readToolSources = (id: string) => get<{ sources: ToolSource[] }>(`/api/conversations/${enc(id)}/tools`);
export const setToolSource = (id: string, source: string, enabled: boolean | null) =>
  put<{ sources: ToolSource[] }>(`/api/conversations/${enc(id)}/tools/${enc(source)}`, { enabled });
export const compactConversation = (id: string) => post<Job>(`/api/conversations/${enc(id)}/compact`);
export const dismissWaiting = (id: string) => post<Job>(`/api/conversations/${enc(id)}/dismiss`);
/** The notes a conversation saved, counted from the memory list (what a deletion for good takes with it). */
export const listMemories = () =>
  get<{ memories: { id: string; source?: { conversation?: string | null } | null }[] }>("/api/memories");

// -- what the composer sends besides text (V3-P102 W6b): uploads, MCP prompts; and a memory call undone ------------

export interface Uploaded {
  file_id: string;
  size: number;
  image?: { state?: string } | null;
  commands?: boolean; // false: this server runs no commands (only text files are read)
}

/** The body is the file (an XHR, for its progress). ``onProgress``: 0..1. */
export function uploadFile(conversation: string, file: File, onProgress: (share: number) => void,
                           signal: AbortSignal): Promise<Uploaded> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/conversations/${enc(conversation)}/uploads?name=${enc(file.name)}`);
    xhr.setRequestHeader("content-type", file.type || "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(Math.min(0.99, event.loaded / event.total));
    });
    xhr.addEventListener("load", () => {
      let data: unknown = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = null;
      }
      if (xhr.status === 201 && data && typeof data === "object") return resolve(data as Uploaded);
      const error = (data as { error?: { message?: string } } | null)?.error;
      reject(new Error(error?.message ?? `The upload failed (HTTP ${xhr.status}).`));
    });
    xhr.addEventListener("error", () => reject(new Error("The upload did not finish.")));
    xhr.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    signal.addEventListener("abort", () => xhr.abort());
    xhr.send(file);
  });
}

export const fileUrl = (conversation: string, file: { file_id: string; version: number }) =>
  `/api/conversations/${enc(conversation)}/files/${enc(file.file_id)}/${enc(String(file.version))}`;

export interface PromptInfo {
  source: string;
  name: string;
  title: string | null;
  description: string | null;
  arguments: { name: string; description: string | null; required: boolean }[];
}

export const listPrompts = (id: string) => get<{ prompts: PromptInfo[] }>(`/api/conversations/${enc(id)}/prompts`);
export const expandPrompt = (id: string, source: string, name: string, args: Record<string, string>) =>
  post<{ text: string }>(`/api/conversations/${enc(id)}/prompts/expand`, { source, name, arguments: args });
export const undoMemory = (id: string, run: string, call: string) =>
  post<{ memory: unknown }>(`/api/conversations/${enc(id)}/calls/${enc(run)}/${enc(call)}/memory-undo`);
