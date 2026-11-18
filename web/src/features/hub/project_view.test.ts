// V3-P103 §6.3: a request's card by kind and state; §6.1, §6.2: devices, held names, a project's name.
import { describe, expect, it } from "vitest";
import type { HubProjectRequest, HubProjects } from "@/api/hub";
import {
  cannotAdd,
  cardView,
  folderNameProblem,
  foldersOf,
  heldFor,
  heldWithoutProject,
  hubHasRequestKeys,
  joinPath,
  nameProblem,
  reasonText,
  secondsLeft,
} from "./project_view";
import type { Entry } from "./requests";

const entry = (request: Partial<HubProjectRequest> & { state: string }, unknown = false): Entry => ({
  request: { id: 1, rev: 1, kind: "add_folder", project: "site", runner: "laptop", ...request }, unknown,
  endedSeen: null,
});
const view = (request: Partial<HubProjectRequest> & { state: string }) => cardView(entry(request));

describe("a request's card", () => {
  it("asks the user for HIMARI's request, with its reason and path", () => {
    const shown = view({ state: "waiting_user", origin: "model", why: "for the docs", path: "/r/site" });
    expect(shown.headline).toBe("HIMARI asks for this");
    expect(shown.lines).toEqual(["Add a folder · site · on laptop", "for the docs", "/r/site"]);
    expect(shown.actions).toEqual(["approve", "refuse"]);
  });

  it("shows a local device's pairing code and its time; a user device or a removal is being checked", () => {
    const local = view({ state: "waiting_device", mode: "local", code: "482-913", expires_at: "2026-11-18T10:05:00Z" });
    expect(local.headline).toBe("Pick the folder on laptop's screen");
    expect([local.code, local.expires, local.actions]).toEqual(["482-913", "2026-11-18T10:05:00Z", ["cancel"]]);
    expect(view({ state: "waiting_device", mode: "user" }).headline).toBe("The device is checking");
    const removal = view({ state: "waiting_device", kind: "remove_folder", mode: "local", code: "x" });
    expect([removal.headline, removal.code]).toEqual(["The device is checking", null]);
  });

  it("says what the device does, by kind", () => {
    expect(view({ state: "applying" }).headline).toBe("The device is registering it");
    expect(view({ state: "applying", kind: "remove_folder" }).headline).toBe("The device is removing it");
    expect(view({ state: "applying" }).actions).toEqual([]);
  });

  it("offers settling a lost one, except while the device is being asked", () => {
    expect(view({ state: "lost" })).toMatchObject({ headline: "Not known whether the device registered it",
      actions: ["resolve"] });
    expect(view({ state: "lost", kind: "remove_folder" }).headline).toBe("Not known whether the device removed it");
    expect(view({ state: "lost", resolving: true })).toMatchObject({ headline: "Asking the device…", actions: [] });
    expect(cardView(entry({ state: "lost" }), true).actions).toEqual([]);
  });

  it("ends with the folder and the project, or that it is removed", () => {
    const added = view({ state: "done", kind: "create", result_path: "D:/code/site" });
    expect([added.headline, added.lines.at(-1), added.actions]).toEqual(["Done", "D:/code/site", ["open_project"]]);
    expect(view({ state: "done", kind: "remove_folder" })).toMatchObject({ headline: "Removed", actions: [] });
    expect(view({ state: "done", kind: "remove_folder", reason: "resolved_by_user" }).lines.at(-1))
      .toBe("Settled by you.");
  });

  it("says why it ended otherwise, a reason it does not know as it is, and offers asking again", () => {
    expect(view({ state: "refused", reason: "refused_by_user" })).toMatchObject({
      headline: "Refused", lines: ["Add a folder · site · on laptop", "You refused it."], actions: ["retry"] });
    expect(view({ state: "failed", reason: "brand_new_reason" }).lines.at(-1)).toBe("brand_new_reason");
    for (const state of ["expired", "cancelled"]) expect(view({ state }).actions).toEqual(["retry"]);
    expect(reasonText(null)).toBeNull();
  });

  it("says when the result cannot be confirmed", () => {
    const shown = cardView({ request: null, unknown: true, endedSeen: 1 });
    expect(shown.headline).toMatch(/cannot be confirmed/);
    expect(shown.actions).toEqual(["reread"]);
  });

  it("counts down a device's time", () => {
    expect(secondsLeft("2026-11-18T10:05:00Z", Date.parse("2026-11-18T10:04:30Z"))).toBe(30);
    expect(secondsLeft("2026-11-18T10:05:00Z", Date.parse("2026-11-18T10:06:00Z"))).toBe(0);
    expect(secondsLeft(null, 0)).toBeNull();
  });
});

const projects = (held: HubProjects["held"], connected = ["laptop"]): HubProjects => ({
  projects: [{ name: "site" }],
  runners: connected.map((name) => ({ name, connected: true, approve: { mode: "local" },
    folders: { site: `D:/${name}/site` } })),
  held,
});

describe("devices and held names", () => {
  it("follows the projects for a held name, the request's rev unchanged", () => {
    // the card's "the same name is held" reads projects.held: it changes with each projects read, not the request
    expect(heldFor(projects([]), "site")).toEqual([]);
    expect(heldFor(projects([{ project: "site", runner: "laptop" }]), "site")).toEqual([
      { runner: "laptop", connected: true }]);
  });

  it("shows a held name of a device that has not come back since the hub restarted, as not connected", () => {
    const after = projects([{ project: "orphan", runner: "gone" }, { project: "site", runner: "laptop" }], []);
    expect(heldWithoutProject(after)).toEqual([{ project: "orphan", runner: "gone", connected: false }]);
    expect(heldFor(after, "site")).toEqual([{ runner: "laptop", connected: false }]);
    expect(hubHasRequestKeys(after)).toBe(true);
    expect(hubHasRequestKeys({ projects: [] })).toBe(false); // a hub before H8 (and H12, H13, H17)
  });

  it("says why a device takes no new folder", () => {
    expect(cannotAdd({ name: "a", connected: true, approve: { mode: "local" } })).toBeNull();
    expect(cannotAdd({ name: "a", connected: true, approve: { mode: "user", roots: ["/r"] } })).toBeNull();
    expect(cannotAdd({ name: "a", connected: false, approve: { mode: "user" } })).toBe("Not connected.");
    expect(cannotAdd({ name: "a", connected: true, approve: { mode: "off", why: "shared_token" } }))
      .toMatch(/shared_token.*himari-hub runner approve/);
    expect(cannotAdd({ name: "a", connected: true })).toMatch(/takes no requests/); // an old runner: no approve
  });

  it("lists the devices with a folder of the project", () => {
    expect(foldersOf(projects([], ["laptop", "linux"]), "site")).toEqual([
      { runner: "laptop", connected: true, folder: "D:/laptop/site" },
      { runner: "linux", connected: true, folder: "D:/linux/site" }]);
    expect(foldersOf(projects([]), "other")).toEqual([]);
  });

  it("checks a name as the hub does", () => {
    expect(nameProblem("a/b?c#d 한글 x", [])).toBeNull();
    expect(nameProblem(" a", [])).toMatch(/space/);
    expect(nameProblem("a".repeat(81), [])).toMatch(/80/);
    expect(nameProblem("a\tb", [])).toMatch(/printable/);
    expect(nameProblem("site", ["site"])).toMatch(/already/);
    expect(folderNameProblem("new")).toBeNull();
    for (const bad of ["a/b", "a\\b", ".", "..", " x"]) expect(folderNameProblem(bad)).not.toBeNull();
    expect(joinPath("/home/u/code", "site")).toBe("/home/u/code/site");
    expect(joinPath("D:\\code\\", "site")).toBe("D:\\code\\site");
  });
});
