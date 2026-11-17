// V3-P103 §6.3, §6.5, §4.1: asking again reads the projects first and sends nothing until they are read; a ``user``
// device's folder must be picked again; an unconfirmed request whose key the hub refused is discarded only after
// the open requests and the projects were read again.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { hubKeys } from "@/app/queries";
import * as api from "@/api/hub";
import type { HubProjects } from "@/api/hub";
import { closeProjectDialog, openProjectDialog } from "@/features/hub/dialog";
import { requests } from "@/features/hub/requests";
import { answerLost, unconfirmed } from "@/features/hub/unconfirmed";
import { AdoptButton } from "./HeldFolders";
import { ProjectDialogHost } from "./ProjectDialog";
import { UnconfirmedItem } from "./UnconfirmedRequests";

const hub: HubProjects = {
  projects: [{ name: "site" }],
  runners: [{ name: "linux", connected: true, approve: { mode: "user", roots: ["/r"] }, folders: {} }],
  held: [],
};

function mount(children: React.ReactNode, known?: HubProjects) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (known) client.setQueryData(hubKeys.projects, known); // what the page had read before
  render(<QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>);
}

const askButton = () => screen.getByRole("button", { name: "Ask the device" }) as HTMLButtonElement;
/** "Choose this folder", once the device has listed the folder entered. */
async function choose() {
  const button = screen.getByRole("button", { name: "Choose this folder" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

describe("asking a device again", () => {
  beforeEach(() => {
    vi.spyOn(api, "newClientKey").mockResolvedValue({ client_key: "fresh", key_time: "2026-11-18T10:00:00Z" });
    vi.spyOn(api, "readRunnerDirs").mockImplementation(async (_runner, path) =>
      path === null ? { path: null, dirs: ["/r"] } : { path, dirs: ["site"] });
    vi.spyOn(api, "readProjectRequestsByIds").mockResolvedValue({ requests: [], missing: [] });
  });
  afterEach(() => {
    cleanup();
    closeProjectDialog();
    requests.reset(true);
    unconfirmed.reset();
    vi.restoreAllMocks();
  });

  it("on a user device sends only a folder picked again, never the one before", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub);
    const make = vi.spyOn(api, "makeProjectRequest").mockResolvedValue({
      request: { id: 7, rev: 1, state: "applying", kind: "add_folder", project: "site", runner: "linux" } });
    mount(<ProjectDialogHost />);
    openProjectDialog({ kind: "add_folder", project: "site", runner: "linux", path: "/r/site", again: true });
    expect(await screen.findByText("Before: /r/site (pick it again)")).toBeTruthy();
    await waitFor(() => expect(api.readHubProjects).toHaveBeenCalled());
    expect(askButton().disabled).toBe(true); // nothing picked yet
    fireEvent.click(await screen.findByRole("button", { name: "/r" }));
    fireEvent.click(await screen.findByRole("button", { name: "site" }));
    await choose();
    await waitFor(() => expect(askButton().disabled).toBe(false));
    fireEvent.click(askButton());
    await waitFor(() => expect(make).toHaveBeenCalledWith({ kind: "add_folder", project: "site", runner: "linux",
      path: "/r/site", client_key: "fresh", key_time: "2026-11-18T10:00:00Z" }));
  });

  it("sends nothing while the projects could not be read again, and reads them on request", async () => {
    let down = true;
    vi.spyOn(api, "readHubProjects").mockImplementation(async () => {
      if (down) throw new ApiError(503, "hub_unavailable", "down");
      return hub;
    });
    mount(<ProjectDialogHost />, hub);
    openProjectDialog({ kind: "add_folder", project: "site", runner: "linux", again: true });
    expect(await screen.findByText(/could not be read again/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "/r" }));
    fireEvent.click(await screen.findByRole("button", { name: "site" }));
    await choose();
    expect(askButton().disabled).toBe(true); // a folder, but not the projects as they are now
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(askButton().disabled).toBe(false));
  });
});

describe("a new project whose answer was lost", () => {
  afterEach(() => {
    cleanup();
    closeProjectDialog();
    requests.reset(true);
    unconfirmed.reset();
    vi.restoreAllMocks();
  });

  it("once discarded, is never sent again with its key: the next try has a key of its own", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub);
    vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    vi.spyOn(api, "readRunnerDirs").mockResolvedValue({ path: null, dirs: ["/r"] });
    vi.spyOn(api, "newClientKey").mockResolvedValueOnce({ client_key: "first", key_time: "2026-11-18T10:00:00Z" })
      .mockResolvedValue({ client_key: "second", key_time: "2026-11-18T10:01:00Z" });
    const make = vi.spyOn(api, "makeProjectRequest").mockRejectedValueOnce(new ApiError(503, "hub_unknown", "lost"))
      .mockResolvedValue({ request: { id: 3, rev: 1, state: "applying", kind: "create", project: "new", runner: "linux" } });
    mount(<ProjectDialogHost />, hub);
    openProjectDialog({ kind: "create" });
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("radio", { name: /^linux/ }));
    fireEvent.click(await screen.findByRole("button", { name: "/r" }));
    await choose();
    await waitFor(() => expect(askButton().disabled).toBe(false));
    fireEvent.click(askButton());
    expect(await screen.findByText("Not confirmed: the hub's answer was lost")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(askButton().disabled).toBe(false));
    fireEvent.click(askButton());
    await waitFor(() => expect(make).toHaveBeenCalledTimes(2));
    expect(make.mock.calls[0]![0].client_key).toBe("first");
    expect(make.mock.calls[1]![0].client_key).toBe("second");
  });
});

describe("what the dialog will not send", () => {
  afterEach(() => {
    cleanup();
    closeProjectDialog();
    requests.reset(true);
    unconfirmed.reset();
    vi.restoreAllMocks();
  });

  it("a folder for a device that, read now, has one for the project already", async () => {
    const local: HubProjects = { projects: [{ name: "site" }], held: [],
      runners: [{ name: "laptop", connected: true, approve: { mode: "local" }, folders: { site: "D:/site" } }] };
    vi.spyOn(api, "readHubProjects").mockResolvedValue(local);
    vi.spyOn(api, "newClientKey").mockResolvedValue({ client_key: "k", key_time: "2026-11-18T10:00:00Z" });
    mount(<ProjectDialogHost />, { ...local, runners: [{ ...local.runners![0]!, folders: {} }] });
    openProjectDialog({ kind: "add_folder", project: "site", runner: "laptop", again: true });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getAllByText("It has a folder for this project already.").length).toBeGreaterThan(0);
    expect(askButton().disabled).toBe(true);
  });

  it("a new request while one for the same project and device waits unconfirmed", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub);
    vi.spyOn(api, "newClientKey").mockResolvedValue({ client_key: "new", key_time: "2026-11-18T10:00:00Z" });
    vi.spyOn(api, "readRunnerDirs").mockResolvedValue({ path: null, dirs: ["/r"] });
    unconfirmed.put({ client_key: "old", key_time: "2026-11-18T09:00:00Z", route: "request", at: 1,
      body: { kind: "add_folder", project: "site", runner: "linux", path: "/r" } });
    mount(<ProjectDialogHost />, hub);
    openProjectDialog({ kind: "add_folder", project: "site", runner: "linux" });
    fireEvent.click(await screen.findByRole("button", { name: "/r" }));
    await choose();
    expect(await screen.findByText(/its answer was lost: send it again or discard it first/)).toBeTruthy();
    expect(askButton().disabled).toBe(true);
  });
});

describe("a user device's folder", () => {
  afterEach(() => {
    cleanup();
    closeProjectDialog();
    vi.restoreAllMocks();
  });

  it("is chosen only once the device has listed it: not while it reads, nor when it cannot", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub);
    vi.spyOn(api, "newClientKey").mockResolvedValue({ client_key: "k", key_time: "2026-11-18T10:00:00Z" });
    vi.spyOn(api, "readRunnerDirs").mockImplementation(async (_runner, path) => {
      if (path === null) return { path: null, dirs: ["/r"] };
      throw new ApiError(409, "hub_refused", "not under the roots");
    });
    mount(<ProjectDialogHost />, hub);
    openProjectDialog({ kind: "add_folder", project: "site", runner: "linux" });
    fireEvent.click(await screen.findByRole("button", { name: "/r" }));
    expect(await screen.findByText("not under the roots")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Choose this folder" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("adopting a held folder", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("after a lost answer offers only reading the projects again until they are read", async () => {
    vi.spyOn(api, "adoptHeldFolder").mockRejectedValueOnce(new ApiError(503, "hub_unknown", "lost"));
    const read = vi.spyOn(api, "readHubProjects").mockRejectedValueOnce(new ApiError(503, "hub_unavailable", ""))
      .mockResolvedValue(hub);
    mount(<AdoptButton project="ghost" runner="linux" connected />, hub);
    fireEvent.click(screen.getByRole("button", { name: "Adopt ghost from linux" }));
    const again = await screen.findByRole("button", { name: "Read the projects again (ghost from linux)" });
    fireEvent.click(again);
    expect(await screen.findByRole("button", { name: "Adopt ghost from linux" })).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(2);
  });
});

describe("an unconfirmed request whose key the hub refused", () => {
  afterEach(() => {
    cleanup();
    requests.reset(true);
    unconfirmed.reset();
    vi.restoreAllMocks();
  });

  it("is discarded only once the open requests and the projects were read again", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub);
    const open = vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    const entry = { client_key: "k1", key_time: "2026-11-17T10:00:00Z", route: "request" as const, at: 1,
      body: { kind: "create" as const, project: "site", runner: "linux" }, refused: "client_key_expired" };
    unconfirmed.put(entry);
    mount(<UnconfirmedItem entry={entry} />);
    const discard = screen.getByRole("button", { name: "Discard" }) as HTMLButtonElement;
    expect(discard.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Read the open requests again" }));
    await waitFor(() => expect(discard.disabled).toBe(false));
    expect(open).toHaveBeenCalled();
    expect(await screen.findByText("No open request like it.")).toBeTruthy();
  });

  it("stays when the reads fail: nothing was learnt", async () => {
    vi.spyOn(api, "readHubProjects").mockRejectedValue(new ApiError(503, "hub_unavailable", "down"));
    vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    const entry = { client_key: "k2", key_time: "2026-11-17T10:00:00Z", route: "request" as const, at: 1,
      body: { kind: "create" as const, project: "site", runner: "linux" }, refused: "client_key_expired" };
    unconfirmed.put(entry);
    mount(<UnconfirmedItem entry={entry} />, hub);
    fireEvent.click(screen.getByRole("button", { name: "Read the open requests again" }));
    expect(await screen.findByText(/could not be read again/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Discard" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("a lost answer", () => {
  it("is a hub_unknown, a broken connection, or a 5xx that is not this server's own", () => {
    expect(answerLost(new ApiError(503, "hub_unknown", ""))).toBe(true);
    expect(answerLost(new ApiError(0, "offline", ""))).toBe(true);
    expect(answerLost(new ApiError(502, "http_502", "Bad Gateway"))).toBe(true);
    expect(answerLost(new ApiError(503, "hub_unavailable", ""))).toBe(false); // never reached the hub
    expect(answerLost(new ApiError(409, "hub_refused", ""))).toBe(false);
  });
});
