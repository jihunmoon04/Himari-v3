// V3-P103 §4.1, §6.2: a settings change whose answer was lost: the projects are read again, and the form then shows
// the settings as the hub has them (the change it did not make is not left standing in the form).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import * as api from "@/api/hub";
import type { HubProjects } from "@/api/hub";
import { clearProjectsDoubt, hubKeys } from "@/app/queries";
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
});
