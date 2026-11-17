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
    fireEvent.click(screen.getByRole("button", { name: "Choose this folder" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Choose this folder" }));
    expect(askButton().disabled).toBe(true); // a folder, but not the projects as they are now
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(askButton().disabled).toBe(false));
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
