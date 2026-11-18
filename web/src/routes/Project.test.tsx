// V3-P103 §4.1, §6.2: a settings change whose answer was lost: the projects are read again, and the form then shows
// the settings as the hub has them (the change it did not make is not left standing in the form).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import * as api from "@/api/hub";
import type { HubProjects } from "@/api/hub";
import { clearProjectsDoubt, hubKeys, readProjectsNow } from "@/app/queries";
import { Project } from "./Project";

const hub: HubProjects = {
  projects: [{ name: "site", default_agent: null }],
  runners: [],
  agents: [{ name: "codex" }],
  held: [],
};

describe("a project's settings after a lost answer", () => {
  afterEach(() => {
    cleanup();
    clearProjectsDoubt();
    vi.restoreAllMocks();
  });

  it("show the hub's settings once read again, when the change was not made", async () => {
    vi.spyOn(api, "readHubProjects").mockResolvedValue(hub); // read again: no default agent, as before
    vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    const set = vi.spyOn(api, "setProjectSettings").mockRejectedValue(new ApiError(503, "hub_unknown", "lost"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(hubKeys.projects, hub);
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/project?name=site"]}><Project /></MemoryRouter>
      </QueryClientProvider>,
    );
    const agent = screen.getByLabelText("Default agent") as HTMLSelectElement;
    fireEvent.change(agent, { target: { value: "codex" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(set).toHaveBeenCalledWith("site", { agent: "codex" }));
    await waitFor(() => expect(agent.value).toBe("")); // the hub's, not the attempted one
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("show them too when the projects are read again by another control after this one's read failed", async () => {
    const read = vi.spyOn(api, "readHubProjects").mockRejectedValueOnce(new ApiError(503, "hub_unavailable", ""))
      .mockResolvedValue(hub);
    vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    vi.spyOn(api, "setProjectSettings").mockRejectedValue(new ApiError(503, "hub_unknown", "lost"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(hubKeys.projects, hub);
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/project?name=site"]}><Project /></MemoryRouter>
      </QueryClientProvider>,
    );
    const agent = screen.getByLabelText("Default agent") as HTMLSelectElement;
    fireEvent.change(agent, { target: { value: "codex" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/whether it changed is not known yet/)).toBeTruthy();
    expect(agent.value).toBe("codex");
    expect(await readProjectsNow(client)).toBe(true); // say, an unconfirmed request's "Read again"
    await waitFor(() => expect(agent.value).toBe(""));
    expect(read).toHaveBeenCalledTimes(2);
  });
});

describe("a project's settings", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("start over on another project's page: a draft never moves to another project", async () => {
    const two: HubProjects = { ...hub, projects: [{ name: "site" }, { name: "docs" }] };
    vi.spyOn(api, "readHubProjects").mockResolvedValue(two);
    vi.spyOn(api, "readProjectRequests").mockResolvedValue({ requests: [], missing: null });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(hubKeys.projects, two);
    const router = createMemoryRouter([{ path: "/project", element: <Project /> }],
      { initialEntries: ["/project?name=site"] });
    render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
    fireEvent.change(screen.getByLabelText("Default agent"), { target: { value: "codex" } });
    await act(() => router.navigate("/project?name=docs"));
    expect(await screen.findByRole("heading", { level: 1, name: "docs" })).toBeTruthy();
    expect((screen.getByLabelText("Default agent") as HTMLSelectElement).value).toBe("");
  });
});
