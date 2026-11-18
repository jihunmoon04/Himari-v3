// V3-P103 §5: a ``projects`` signal reads the projects again with a read of its own (one already on its way may have
// begun before the signal), and a signal that comes while it reads gives one more read after it.
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/hub";
import { hubKeys, rereadProjectsQuery } from "./queries";

describe("the projects read again for a signal", () => {
  afterEach(() => vi.restoreAllMocks());

  it("does not join the first read on its way, and reads once more for a signal meanwhile", async () => {
    let calls = 0;
    vi.spyOn(api, "readHubProjects").mockImplementation(() => {
      calls += 1;
      if (calls === 1) return new Promise(() => undefined); // the page's first read, begun before the signal
      return Promise.resolve({ projects: [{ name: `read ${calls}` }] });
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const observer = new QueryObserver(client, { queryKey: hubKeys.projects, queryFn: () => api.readHubProjects() });
    const stop = observer.subscribe(() => undefined);
    await vi.waitFor(() => expect(calls).toBe(1));
    const first = rereadProjectsQuery(client);
    void rereadProjectsQuery(client); // another signal while it reads
    await first;
    expect(calls).toBe(3);
    expect(client.getQueryData(hubKeys.projects)).toEqual({ projects: [{ name: "read 3" }] });
    stop();
  });
});
