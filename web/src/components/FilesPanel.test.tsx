// The file panel goes from "Loading…" to a folder in one mounted component: the same hooks on every render, or
// React stops the page (#310, "Rendered more hooks than during the previous render").
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilesPanel, innermost } from "./FilesPanel";

vi.mock("@/api/himari", () => ({
  conversationFolders: vi.fn(async () => ({ items: [{ binding_id: null, path: "/w/c1", own: true, working: true }] })),
  browseFolder: vi.fn(async (path: string) => ({ path, parent: null, entries: [{ name: "notes.txt", kind: "file", size: 12 }] })),
  statMachinePath: vi.fn(),
  showMachineFile: vi.fn(),
  machineImageUrl: (path: string) => path,
  machineDownloadUrl: (path: string) => path,
}));
vi.mock("@/api/settings", () => ({
  readWorkspaces: vi.fn(async () => ({ items: [] })),
  attachWorkspace: vi.fn(), detachWorkspace: vi.fn(), setWorkingWorkspace: vi.fn(),
}));

describe("the file panel", () => {
  afterEach(cleanup);

  it("shows a conversation's folder after loading it, without React stopping the page", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<QueryClientProvider client={client}><FilesPanel conversation="c1" /></QueryClientProvider>);
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(await screen.findByText("notes.txt")).toBeTruthy();
    expect(screen.getByText(/This conversation/)).toBeTruthy();
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("finds the deepest attached folder holding a path", () => {
    expect(innermost(["/a", "/a/b"], "/a/b/c.txt")).toBe("/a/b");
    expect(innermost(["/a", "/a/b"], "/a/bc")).toBe("/a");
    expect(innermost(["/a"], "/z")).toBeNull();
  });
});
