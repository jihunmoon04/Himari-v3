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
    expect(STARTER.split("\n").at(-1)).toBe(
      "숨긴 이야기: 줄 앞에 [familiar] 또는 [close]를 붙이면 그만큼 가까워진 뒤에 드러납니다"); // character-world §5
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

describe("an open persona", () => {
  afterEach(cleanup);

  it("keeps its unsaved text: nothing else can replace it until it is saved or cancelled", async () => {
    const at = "2026-11-24T00:00:00+00:00";
    state.data = { ...emptyList(), personas: [
      { id: 1, name: "Teacher", text: "a", revision: 1, created_at: at, updated_at: at },
      { id: 2, name: "Friend", text: "b", revision: 1, created_at: at, updated_at: at }] };
    shown();
    const [firstEdit] = await screen.findAllByRole("button", { name: "Edit" });
    if (!firstEdit) throw new Error("no Edit button");
    fireEvent.click(firstEdit);
    expect((screen.getByRole("button", { name: "Edit" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Duplicate" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "New persona" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getAllByRole("button", { name: "Edit" }).every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
  });
});

describe("a duplicate's name", () => {
  it("is the first free copy name", () => {
    expect(copyName("Teacher", ["Teacher"], 40)).toBe("Teacher copy");
    expect(copyName("Teacher", ["Teacher", "teacher COPY"], 40)).toBe("Teacher copy 2");
    expect(copyName("가".repeat(40), [], 40)).toBe(`${"가".repeat(35)} copy`);
    expect(copyName("😀".repeat(40), [], 40)).toBe(`${"😀".repeat(35)} copy`); // cut in code points, no lone surrogate
    expect(copyName("히", ["히 COPY"], 40)).toBe("히 copy 2"); // NFC, as the server compares
  });
});
