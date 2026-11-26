// A day session's page (character-day §3.2, §18.12): its date title with the Day mark; once closed it is read only —
// the composer gives way to "a closed day".
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConversationDetail } from "@/api/himari";
import { Conversation } from "./Conversation";

const at = "2026-11-26T01:00:00+00:00";
const detail = (rest: Partial<ConversationDetail["conversation"]>): ConversationDetail => ({
  conversation: { id: "c1", title: "11월 26일 (목)", preview: null, created_at: at, last_activity_at: at, last_sequence: 3,
    ...rest },
  job: { running: false, generation: 1, job_id: null, state: "completed", detail: null, error: null },
  continuable: false,
});
const state: { detail: ConversationDetail } = { detail: detail({}) };

vi.mock("@/app/queries", () => ({
  keys: { detail: (id: string) => ["conversation", id], events: (id: string) => ["conversation", id, "events"] },
  useDetail: () => ({ data: state.detail, error: null }),
  useEvents: () => ({ data: { items: [], last_sequence: 0 }, isLoading: false }),
  usePrincipalEvents: vi.fn(),
}));
vi.mock("@/lib/sse", () => ({ openStream: () => () => undefined }));
vi.mock("@/components/HimariComposer", () => ({ HimariComposer: () => "the composer" }));
vi.mock("@/components/ConversationMenu", () => ({ ConversationMenu: () => null }));
vi.mock("@/components/SidePanel", () => ({ SidePanel: () => null }));
vi.mock("@/components/FilesPanel", () => ({ FilesPanel: () => null }));

function shown() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/c/c1"]}>
        <Routes><Route path="/c/:id" element={<Conversation />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("a day session's page", () => {
  afterEach(cleanup);

  it("marks today's day session and keeps its composer", () => {
    state.detail = detail({ day_session: "2026-11-26", closed: false });
    shown();
    expect(screen.getByRole("heading", { name: "11월 26일 (목)" })).toBeTruthy();
    expect(screen.getByText("Day")).toBeTruthy();
    expect(screen.getByText("the composer")).toBeTruthy();
    expect(screen.queryByLabelText("Closed day")).toBeNull();
  });

  it("closes the composer once the day closed", () => {
    state.detail = detail({ day_session: "2026-11-26", closed: true });
    shown();
    expect(screen.getByLabelText("Closed day").textContent).toContain("A closed day: read only.");
    expect(screen.queryByText("the composer")).toBeNull();
  });

  it("leaves any other conversation as it was", () => {
    state.detail = detail({ title: "작업", day_session: null, closed: false });
    shown();
    expect(screen.queryByText("Day")).toBeNull();
    expect(screen.getByText("the composer")).toBeTruthy();
  });
});
