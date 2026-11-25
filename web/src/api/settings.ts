// The settings the classic page manages (V3-P102 W6c), on the same routes: instructions (V3-P69), the companion
// personas (V3-P108), HIMARI's life (its facts, plans and diary; character-life §6), memory, its suggestions and
// timeline (V3-P71, V3-P85), devices and quiet (V3-P83, V3-P88), the Tool servers' defaults (V3-P53).
import { del, enc, get, post, put, api } from "./client";

export interface InstructionsText {
  text: string;
  revision: number;
  updated_at: string | null;
  max_chars: number;
}

export const readInstructions = () => get<InstructionsText>("/api/instructions");
export const saveInstructions = (text: string, revision: number) => put<InstructionsText>("/api/instructions", { text, revision });

export interface Persona {
  id: number;
  name: string; // the user's label in the list, never told to HIMARI
  text: string; // what HIMARI is told: made from the fields when saved (character-persona-fields §3)
  fields: PersonaFields;
  revision: number;
  created_at: string;
  updated_at: string;
}

// character-persona-fields §2: the filled fields; a persona from before the fields is all "free"
export type PersonaFields = Partial<Record<PersonaField, string>>;
export type PersonaField = "name" | "relation" | "world" | "calls_user" | "personality" | "speech" | "examples" |
  "habits" | "likes" | "makes" | "never" | "familiar" | "close" | "free";

export interface PersonaChoice {
  persona_id: number | null; // null: no persona
  revision: number;
  updated_at: string | null;
}

export interface Personas {
  personas: Persona[];
  choice: PersonaChoice;
  companion: boolean; // whether the companion is on (personas are told only then)
  limits: { count: number; name_chars: number; text_chars: number };
}

export const readPersonas = () => get<Personas>("/api/personas");
export const addPersona = (name: string, fields: PersonaFields) => post<Persona>("/api/personas", { name, fields });
export const editPersona = (id: number, name: string, fields: PersonaFields, revision: number) =>
  put<Persona>(`/api/personas/${id}`, { name, fields, revision });
export const deletePersona = (id: number, revision: number) =>
  del<PersonaChoice>(`/api/personas/${id}?revision=${revision}`);
export const choosePersona = (personaId: number | null, revision: number) =>
  put<PersonaChoice>("/api/personas/choice", { persona_id: personaId, revision });

// arc: a thread over weeks, a project (character-world §4); work: a piece of a project (character-work §4);
// thought: what HIMARI thought and left unsaid, kept a few days (character-mind §5.2)
export type LifeKind = "fact" | "plan" | "diary" | "arc" | "work" | "thought";
export type ThoughtAbout = "user" | "self" | "work" | "intent";
export type LifeOrigin = "writer" | "conversation" | "user"; // the story writer, a conversation, the user

export interface LifeEntry {
  id: number;
  kind: LifeKind;
  day: string | null; // YYYY-MM-DD for a plan or a diary, null for a fact
  time?: string | null; // HH:MM when a plan starts (character-state §3), null otherwise
  text: string; // an arc's goal
  origin: LifeOrigin;
  revision: number;
  created_at: string;
  updated_at: string;
  // character-world §4: an arc's title, whether it is open, and its last day and ending once closed (``day`` is
  // the day it started); a diary's arc_id is the arc its day moved.
  title?: string | null;
  status?: "open" | "closed";
  end_day?: string | null;
  ending?: string | null;
  arc_id?: number | null;
  // character-work §3, §4: an arc's "so far"; a plan's arc_id is its project (a work plan); a work piece has its
  // title, one-line summary, project (arc_id) and plan (plan_id), the piece itself as ``text``
  so_far?: string | null;
  summary?: string | null;
  plan_id?: number | null;
  // character-mind §5.2: a kept thought's subject and when it stops being told (it is only deleted, never edited)
  about?: ThoughtAbout | null;
  expires_at?: string | null;
}

/** HIMARI's mood while it is told (character-state §2): a reply's last mood or the story writer's, with why. */
export interface HimariMood { mood: string; set_at: string; source: "reply" | "writer"; reason: string | null }
/** What HIMARI is doing now (character-state §3): asleep in the quiet hours, or a timed plan under way. */
export interface HimariNow { asleep: boolean; doing: string | null; since: string | null }

export interface Life {
  persona: { id: number; name: string } | null; // null: no persona chosen (the default's life)
  enabled: boolean; // whether the life is used now (the companion and life are on)
  writer: { running: boolean; told: string };
  mood?: HimariMood | null; // null: none now (or the life is off)
  now?: HimariNow | null; // null: nothing now (or the life is off)
  // character-world §2, §3: whether the weather and the day's topics are used (as told at startup), today's weather
  world?: { weather: string; today: string | null; topics: string };
  // facts, arcs, then plans, work pieces and diaries, newest day first (the last 30 days on), then the kept thoughts
  entries: LifeEntry[];
  // character-work §8: whether the worker runs (with the story writer) and the next work plan
  work?: { running: boolean; next: LifeEntry | null };
  limits: { facts: number; chars: number; diary_chars: number; arc_chars?: number; arc_title_chars?: number;
    work_chars?: number; summary_chars?: number };
}

export const readLife = () => get<Life>("/api/life");
/** ``personaId``: the persona the page shows (null for the default); refused with ``persona_changed`` when another
 * one was chosen since. */
export const addLifeFact = (text: string, personaId: number | null) =>
  post<LifeEntry>("/api/life", { text, persona_id: personaId });
/** ``title``: an arc's or a work piece's new title (character-world §4); ``summary``: a work piece's new summary
 * (character-work §4); left out, each is kept. */
export const editLife = (id: number, text: string, revision: number, title?: string, summary?: string) =>
  put<LifeEntry>(`/api/life/${id}`, {
    text, revision, ...(title === undefined ? {} : { title }), ...(summary === undefined ? {} : { summary }),
  });
export const deleteLife = (id: number, revision: number) => del<void>(`/api/life/${id}?revision=${revision}`);

// The bond (character-bond §1): what is settled between the user and the persona in use, how close the two are, and
// the memories' timeline (the bond's entries with the newest episodes).
export type BondTag = "name" | "promise" | "joke" | "first" | "date";
export const BOND_TAGS: BondTag[] = ["name", "promise", "joke", "first", "date"];
export type BondStage = "new" | "familiar" | "close";
export const BOND_STAGES: BondStage[] = ["new", "familiar", "close"];

export interface BondEntry {
  id: number;
  tag: BondTag;
  text: string;
  month_day: string | null; // MM-DD for a date, null otherwise
  origin: "conversation" | "user";
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface BondStanding {
  stage: BondStage;
  since: string | null; // when it moved there; null: never moved
  reason: string | null; // "b<id>" (an entry), "30d"/"90d" (days together) or "user"
  revision: number; // 0: never stored
}

export type BondMemory =
  | { kind: "bond"; id: number; at: string; tag: BondTag; text: string }
  | { kind: "episode"; id: number; at: string; text: string; title: string | null };

export interface Bond {
  persona: { id: number; name: string } | null;
  enabled: boolean; // whether the bond is told now (the companion and life are on)
  stage: BondStanding;
  together_days: number; // the first conversation's day is day 1; 0: no conversation yet
  since: string | null; // YYYY-MM-DD of the first conversation
  today: string[]; // today's anniversaries
  entries: BondEntry[]; // names, promises, then the rest, newest first
  memories: BondMemory[]; // newest first
  limits: { entries: number; chars: number };
}

export const readBond = () => get<Bond>("/api/bond");
/** ``personaId``: the persona the page shows (null for the default); refused with ``persona_changed`` as for life. */
export const addBond = (tag: BondTag, text: string, monthDay: string | null, personaId: number | null) =>
  post<BondEntry>("/api/bond", { tag, text, month_day: monthDay, persona_id: personaId });
export const editBond = (id: number, tag: BondTag, text: string, monthDay: string | null, revision: number) =>
  put<BondEntry>(`/api/bond/${id}`, { tag, text, month_day: monthDay, revision });
export const deleteBond = (id: number, revision: number) => del<void>(`/api/bond/${id}?revision=${revision}`);
export const setBondStage = (stage: BondStage, personaId: number | null, revision: number) =>
  put<BondStanding>("/api/bond/stage", { stage, persona_id: personaId, revision });

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
