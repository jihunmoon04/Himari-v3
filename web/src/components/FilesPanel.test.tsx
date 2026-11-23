// The file panel lists the conversation's own files, each a download (an image as its preview).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilesPanel } from "./FilesPanel";

vi.mock("@/api/himari", () => ({
  listFiles: vi.fn(async () => ({ items: [{ file_id: "f1", version: 2, name: "notes.txt", size: 12 }], next_offset: null })),
  fileUrl: (conversation: string, file: { file_id: string; version: number }) =>
    `/api/conversations/${conversation}/files/${file.file_id}/${file.version}`,
  undoMemory: vi.fn(),
}));

describe("the file panel", () => {
  afterEach(cleanup);

  it("lists the conversation's files after loading them", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><FilesPanel conversation="c1" /></QueryClientProvider>);
    expect(screen.getByText("Loading…")).toBeTruthy();
    const link = await screen.findByRole("link", { name: "Download notes.txt" });
    expect(link.getAttribute("href")).toBe("/api/conversations/c1/files/f1/2");
  });
});
