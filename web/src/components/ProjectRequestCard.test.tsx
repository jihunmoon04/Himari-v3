// V3-P103 §6.3, §6.4, §4.1: a request's card asks again by kind (with a new client key, as things are now), and
// after a lost answer it settles safely: the request is read again; a hub without H17 rests the button a minute.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import * as api from "@/api/hub";
import type { HubProjectRequest, HubProjects } from "@/api/hub";
import { closeProjectDialog, useProjectDialog } from "@/features/hub/dialog";
import { requests } from "@/features/hub/requests";
import { unconfirmed } from "@/features/hub/unconfirmed";
import { ProjectRequestCard } from "./ProjectRequestCard";

const withH = (folders: Record<string, string>, held = true): HubProjects => ({
  projects: [{ name: "site" }],
  runners: [{ name: "linux", connected: true, approve: { mode: "user", roots: ["/r"] }, folders }],
  ...(held ? { held: [] } : {}),
});

let dialog: ReturnType<typeof useProjectDialog> | null = null;
function DialogProbe() {
  dialog = useProjectDialog();
  return null;
}

function show(request: HubProjectRequest, projects: HubProjects) {
  vi.spyOn(api, "readHubProjects").mockResolvedValue(projects);
  requests.merge(request);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter><ProjectRequestCard id={request.id} /><DialogProbe /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a project request's card", () => {
  beforeEach(() => {
    vi.spyOn(api, "readProjectRequestsByIds").mockResolvedValue({ requests: [], missing: [] });
    vi.spyOn(api, "newClientKey").mockResolvedValue({ client_key: "fresh", key_time: "2026-11-18T10:00:00Z" });
  });
  afterEach(() => {
    cleanup();
    requests.reset(true);
    unconfirmed.reset();
    closeProjectDialog();
    vi.restoreAllMocks();
  });

  it("asks a local folder again through the dialog, with the same device, project and choices", async () => {
    show({ id: 1, rev: 3, state: "expired", kind: "create", project: "site", runner: "laptop", mode: "local",
      create_dir: true, path: "D:/code", agent: "claude", reason: "no_answer_on_device" }, withH({}));
    expect(await screen.findByText("Nobody picked a folder on the device in time.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ask again" }));
    expect(dialog?.init).toEqual({ kind: "create", project: "site", runner: "laptop", path: "D:/code",
      createDir: true, agent: "claude", again: true });
  });

  it("asks a removal again only while the device still reports the folder, with a new key", async () => {
    const remove = vi.spyOn(api, "removeProjectFolder").mockResolvedValue({
      request: { id: 9, rev: 1, state: "waiting_device", kind: "remove_folder", project: "site", runner: "linux" } });
    show({ id: 2, rev: 2, state: "failed", kind: "remove_folder", project: "site", runner: "linux" },
      withH({ site: "/r/site" }));
    fireEvent.click(await screen.findByRole("button", { name: "Ask again" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith({ project: "site", runner: "linux", client_key: "fresh",
      key_time: "2026-11-18T10:00:00Z" }));
    expect(requests.isTracked(9)).toBe(true);
  });

  it("does not send a removal whose folder is already gone", async () => {
    const remove = vi.spyOn(api, "removeProjectFolder");
    show({ id: 3, rev: 2, state: "cancelled", kind: "remove_folder", project: "site", runner: "linux" }, withH({}));
    fireEvent.click(await screen.findByRole("button", { name: "Ask again" }));
    expect(await screen.findByText("It is already removed.")).toBeTruthy();
    expect(remove).not.toHaveBeenCalled();
  });

  it("allows HIMARI's request, and after a lost answer reads it again", async () => {
    const answer = vi.spyOn(api, "answerProjectRequest")
      .mockRejectedValueOnce(new ApiError(503, "hub_unknown", "lost"));
    const read = vi.mocked(api.readProjectRequestsByIds);
    show({ id: 4, rev: 1, state: "waiting_user", kind: "create", project: "site", runner: "linux", origin: "model",
      why: "for the docs" }, withH({}));
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText(/did not answer; it is read again/)).toBeTruthy();
    expect(answer).toHaveBeenCalledWith(4, true);
    await waitFor(() => expect(read).toHaveBeenCalledWith([4]));
    await waitFor(() => expect((screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement).disabled)
      .toBe(false)); // read again, and it still waits: it may be allowed again
  });

  it("after a lost answer whose request cannot be read again, offers nothing but reading it again", async () => {
    vi.spyOn(api, "answerProjectRequest").mockRejectedValueOnce(new ApiError(503, "hub_unknown", "lost"));
    const read = vi.mocked(api.readProjectRequestsByIds).mockRejectedValueOnce(new ApiError(503, "hub_unavailable", ""));
    show({ id: 8, rev: 1, state: "waiting_user", kind: "create", project: "site", runner: "linux", origin: "model" },
      withH({}));
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText(/could not be read again/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Refuse" }) as HTMLButtonElement).disabled).toBe(true);
    read.mockResolvedValueOnce({ requests: [{ id: 8, rev: 2, state: "applying", kind: "create", project: "site",
      runner: "linux" }], missing: [] });
    fireEvent.click(screen.getByRole("button", { name: "Read it again" }));
    expect(await screen.findByText("The device is registering it")).toBeTruthy(); // the first one went through
  });

  it("stays in doubt for every card of it, and when the card is drawn again", async () => {
    vi.spyOn(api, "answerProjectRequest").mockRejectedValueOnce(new ApiError(503, "hub_unknown", "lost"));
    vi.mocked(api.readProjectRequestsByIds).mockRejectedValue(new ApiError(503, "hub_unavailable", ""));
    const waiting = { id: 12, rev: 1, state: "waiting_user", kind: "create", project: "site", runner: "linux",
      origin: "model" } as const;
    show(waiting, withH({}));
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText(/could not be read again/)).toBeTruthy();
    cleanup(); // left the page and came back
    show(waiting, withH({}));
    expect((screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Read it again" })).toBeTruthy();
  });

  it("without H17, rests the settle button for a minute after a lost answer", async () => {
    vi.spyOn(api, "resolveProjectRequest").mockRejectedValue(new ApiError(503, "hub_unknown", "lost"));
    show({ id: 5, rev: 1, state: "lost", kind: "add_folder", project: "site", runner: "linux" }, withH({}, false));
    fireEvent.click(await screen.findByRole("button", { name: "Settle" }));
    const resting = await screen.findByRole("button", { name: "Wait a minute" });
    expect((resting as HTMLButtonElement).disabled).toBe(true);
  });

  it("when the device cannot be asked, lets the user say what happened on it", async () => {
    const settle = vi.spyOn(api, "resolveProjectRequest")
      .mockRejectedValueOnce(new ApiError(409, "hub_refused", "기기가 연결돼야 정리할 수 있습니다"))
      .mockResolvedValueOnce({ request: { id: 6, rev: 3, state: "done", kind: "remove_folder", project: "site",
        runner: "linux", reason: "resolved_by_user" } });
    show({ id: 6, rev: 1, state: "lost", kind: "remove_folder", project: "site", runner: "linux" }, withH({}));
    fireEvent.click(await screen.findByRole("button", { name: "Settle" }));
    expect(await screen.findByText("기기가 연결돼야 정리할 수 있습니다")).toBeTruthy();
    fireEvent.click(screen.getByText("If the device is lost"));
    fireEvent.click(screen.getByLabelText(/^Removed\./));
    fireEvent.click(screen.getByRole("button", { name: "Settle as this" }));
    await waitFor(() => expect(settle).toHaveBeenLastCalledWith(6, { outcome: "applied" }));
    expect(await screen.findByText("Removed")).toBeTruthy();
  });
});
