// What a project request's card says and offers, by kind and state (V3-P103 §6.3), and what the projects say about
// devices and held names (§6.1, §6.2). Pure functions, tested in project_view.test.ts.
import type { HubProjectRequest, HubProjects, HubRunner } from "@/api/hub";
import type { Entry } from "./requests";

export type Action = "approve" | "refuse" | "cancel" | "resolve" | "open_project" | "retry" | "reread";

export interface CardView {
  tone: "wait" | "run" | "good" | "bad" | "unknown";
  headline: string;
  code: string | null; // the pairing code, shown large
  expires: string | null; // when a device's turn ends (local, waiting)
  lines: string[];
  actions: Action[];
}

export const KINDS: Record<string, string> = {
  create: "New project", add_folder: "Add a folder", remove_folder: "Remove a folder",
};

export const REASONS: Record<string, string> = {
  refused_by_user: "You refused it.",
  cancelled_by_user: "Cancelled.",
  name_taken: "That name was taken meanwhile.",
  refused_on_device: "Refused on the device.",
  failed_on_device: "The device could not do it.",
  no_answer: "Nobody allowed it in time.",
  no_answer_on_device: "Nobody picked a folder on the device in time.",
  runner_gone: "The device went away.",
  policy_changed: "The device's way of taking requests changed.",
  server_restarted: "The hub restarted.",
  no_result: "The device did not report back.",
  resolved_by_user: "Settled by you.",
  resolved_by_fence: "Settled with the device.",
};

/** A reason in words; one this page does not know is shown as it is. */
export const reasonText = (reason: string | null | undefined) => (reason ? REASONS[reason] ?? reason : null);

const adding = (r: HubProjectRequest) => r.kind !== "remove_folder";

/** The card for ``entry``. ``asking``: a settling of this request runs from this page. */
export function cardView(entry: Entry, asking = false): CardView {
  const r = entry.request;
  if (entry.unknown || !r) {
    return {
      tone: "unknown", headline: entry.unknown ? "The result cannot be confirmed (the hub's record is gone)."
        : "Reading the request…", code: null, expires: null, lines: [], actions: entry.unknown ? ["reread"] : [],
    };
  }
  const where = [KINDS[r.kind ?? ""] ?? r.kind ?? "Request", r.project, r.runner && `on ${r.runner}`]
    .filter(Boolean).join(" · ");
  const lines = [where];
  switch (r.state) {
    case "waiting_user":
      if (r.why) lines.push(r.why);
      if (r.path) lines.push(r.path);
      return { tone: "wait", headline: "HIMARI asks for this", code: null, expires: null, lines,
        actions: ["approve", "refuse"] };
    case "waiting_device":
      if (adding(r) && r.mode === "local") {
        return { tone: "wait", headline: `Pick the folder on ${r.runner ?? "the device"}'s screen`,
          code: r.code ?? null, expires: r.expires_at ?? null, lines, actions: ["cancel"] };
      }
      if (r.path) lines.push(r.path);
      return { tone: "run", headline: "The device is checking", code: null, expires: null, lines, actions: ["cancel"] };
    case "applying":
      return { tone: "run", headline: adding(r) ? "The device is registering it" : "The device is removing it",
        code: null, expires: null, lines, actions: [] };
    case "lost":
      if (r.resolving || asking) {
        return { tone: "run", headline: "Asking the device…", code: null, expires: null, lines, actions: [] };
      }
      return { tone: "wait", headline: adding(r) ? "Not known whether the device registered it"
        : "Not known whether the device removed it", code: null, expires: null, lines, actions: ["resolve"] };
    case "done":
      if (r.reason) lines.push(reasonText(r.reason)!); // settled: by the device or by the user
      if (!adding(r)) return { tone: "good", headline: "Removed", code: null, expires: null, lines, actions: [] };
      if (r.result_path) lines.push(r.result_path);
      return { tone: "good", headline: "Done", code: null, expires: null, lines, actions: ["open_project"] };
    case "refused":
    case "expired":
    case "failed":
    case "cancelled": {
      const why = reasonText(r.reason);
      if (why) lines.push(why);
      const headline = { refused: "Refused", expired: "Expired", failed: "Failed", cancelled: "Cancelled" }[r.state];
      return { tone: "bad", headline, code: null, expires: null, lines, actions: ["retry"] };
    }
    default:
      return { tone: "unknown", headline: r.state, code: null, expires: null, lines, actions: [] };
  }
}

/** Seconds left until ``expires`` (ISO), never below 0; null without one. */
export function secondsLeft(expires: string | null, now: number): number | null {
  if (!expires) return null;
  const end = Date.parse(expires);
  return Number.isNaN(end) ? null : Math.max(0, Math.round((end - now) / 1000));
}

// -- devices and held names ---------------------------------------------------------------------------------------

export const runnerOf = (projects: HubProjects | undefined, name: string | undefined): HubRunner | undefined =>
  (projects?.runners ?? []).find((r) => r.name === name);

/** Why a device takes no new folders (null: it does). */
export function cannotAdd(runner: HubRunner | undefined): string | null {
  if (!runner || runner.connected === false) return "Not connected.";
  const mode = runner.approve?.mode;
  if (mode === "local" || mode === "user") return null;
  const why = runner.approve?.why;
  return `It takes no requests${why ? ` (${why})` : ""}. On that device: himari-hub runner approve`;
}

export const MODE_TEXT: Record<string, string> = {
  local: "picked on that device's screen",
  user: "picked here (under its roots)",
};

/** The names held for ``project`` (H8, the hub's ledger), each with whether its device is connected now. */
export function heldFor(projects: HubProjects | undefined, project: string) {
  return (projects?.held ?? []).flatMap((h) => h.project === project && h.runner
    ? [{ runner: h.runner, connected: runnerOf(projects, h.runner)?.connected === true }] : []);
}

/** Held names that no project has yet (the sidebar's held folders, §6.2). */
export function heldWithoutProject(projects: HubProjects | undefined) {
  const names = new Set((projects?.projects ?? []).map((p) => p.name));
  return (projects?.held ?? []).flatMap((h) => h.project && h.runner && !names.has(h.project)
    ? [{ project: h.project, runner: h.runner, connected: runnerOf(projects, h.runner)?.connected === true }] : []);
}

/** Whether the hub has the V3-P103 items (H8, H12, H13, H17 ship together): ``held`` in its projects says so. */
export const hubHasRequestKeys = (projects: HubProjects | undefined) => Array.isArray(projects?.held);

/** The devices that have a folder for ``project``: name, connected, its folder. */
export function foldersOf(projects: HubProjects | undefined, project: string) {
  return (projects?.runners ?? []).flatMap((r) => {
    const folder = r.folders?.[project];
    const listed = folder !== undefined || (r.projects ?? []).includes(project);
    return listed && r.name ? [{ runner: r.name, connected: r.connected === true, folder: folder ?? null }] : [];
  });
}

/** A project name as the hub takes it: at most 80 characters, no space at either end, printable. */
export function nameProblem(name: string, existing: readonly (string | undefined)[]): string | null {
  if (!name) return "Give it a name.";
  if (name.length > 80) return "At most 80 characters.";
  if (name.trim() !== name) return "No space at the start or the end.";
  if (/[\p{C}\p{Zl}\p{Zp}]|[^\S ]/u.test(name)) return "Only printable characters."; // as Python's isprintable
  if (existing.includes(name)) return "A project of this name is already there.";
  return null;
}

/** A new folder's name under the chosen one: one name, no separator, not ``.`` or ``..``. */
export function folderNameProblem(name: string): string | null {
  if (!name) return null;
  if (/[\\/]/.test(name) || name === "." || name === ".." || name.trim() !== name) {
    return "One folder name: no / or \\, not . or .., no space at either end.";
  }
  return null;
}

/** ``parent`` joined with ``name`` the way the device's own paths are written. */
export const joinPath = (parent: string, name: string) => {
  const separator = parent.includes("\\") && !parent.includes("/") ? "\\" : "/";
  return parent.endsWith(separator) ? `${parent}${name}` : `${parent}${separator}${name}`;
};
