// The personas screen (V3-P108 §6.2): the list with the one in use, "No persona", the first persona's guide, and the
// names a duplicate takes.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Personas } from "@/api/settings";
import { copyName, PersonaSettings, STARTER } from "./Personas";

const state: { data: Personas } = { data: emptyList() };

function emptyList(): Personas {
  return { personas: [], choice: { persona_id: null, revision: 0, updated_at: null }, companion: true,
    limits: { count: 20, name_chars: 40, text_chars: 10000 } };
}

vi.mock("@/api/settings", () => ({
  readPersonas: vi.fn(async () => state.data),
  addPersona: vi.fn(),
  editPersona: vi.fn(),
  deletePersona: vi.fn(),
  choosePersona: vi.fn(),
}));

function shown() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><PersonaSettings /></QueryClientProvider>);
}

describe("the personas screen", () => {
  afterEach(cleanup);

  it("opens the first persona with the guide", async () => {
    state.data = emptyList();
    shown();
    fireEvent.click(await screen.findByRole("button", { name: "New persona" }));
    expect((screen.getByLabelText("Persona text") as HTMLTextAreaElement).value).toBe(STARTER);
  });

  it("marks the persona in use and offers the others and no persona", async () => {
    const at = "2026-11-24T00:00:00+00:00";
    state.data = { ...emptyList(), choice: { persona_id: 2, revision: 3, updated_at: at }, companion: false,
      personas: [{ id: 1, name: "Teacher", text: "Call me 선생님.", revision: 1, created_at: at, updated_at: at },
        { id: 2, name: "Friend", text: "\n반말로.", revision: 2, created_at: at, updated_at: at }] };
    shown();
    expect(await screen.findByText("Friend")).toBeTruthy();
    expect(screen.getAllByText("In use")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Use" })).toHaveLength(2); // Teacher and No persona
    expect(screen.getByText("반말로.")).toBeTruthy(); // the first line with text
    expect(screen.getByText(/personas are not used now/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New persona" }));
    expect((screen.getByLabelText("Persona text") as HTMLTextAreaElement).value).toBe(""); // the guide only first
  });
});

describe("a duplicate's name", () => {
  it("is the first free copy name", () => {
    expect(copyName("Teacher", ["Teacher"], 40)).toBe("Teacher copy");
    expect(copyName("Teacher", ["Teacher", "teacher COPY"], 40)).toBe("Teacher copy 2");
    expect(copyName("가".repeat(40), [], 40)).toBe(`${"가".repeat(35)} copy`);
  });
});
