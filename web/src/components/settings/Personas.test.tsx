// The personas screen (V3-P108 §6.2): the list with the one in use, "No persona", the editor in fields with what
// HIMARI is told (character-persona-fields §5), and the names a duplicate takes.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { addPersona, type Personas } from "@/api/settings";
import { copyName, FIELDS, PersonaSettings, personaText } from "./Personas";

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

  it("opens a new persona in fields, with the guide as placeholders and what HIMARI is told", async () => {
    state.data = emptyList();
    shown();
    fireEvent.click(await screen.findByRole("button", { name: "New persona" }));
    expect(FIELDS.map((f) => f.key)).toEqual(["name", "relation", "world", "calls_user", "personality", "speech",
      "examples", "habits", "likes", "makes", "never", "familiar", "close", "free"]);
    const makes = screen.getByLabelText("하는 일과 만드는 것") as HTMLTextAreaElement;
    expect(makes.value).toBe("");
    expect(makes.placeholder).toContain("블랙홀 무기 설정집");
    fireEvent.change(screen.getByLabelText("Persona name"), { target: { value: "Lucy" } });
    fireEvent.change(screen.getByLabelText("이름"), { target: { value: "루시" } });
    fireEvent.change(makes, { target: { value: "작곡" } });
    expect(screen.getByLabelText("What HIMARI is told").textContent).toBe("이름: 루시\n하는 일과 만드는 것: 작곡");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(addPersona)).toHaveBeenCalledWith("Lucy", { name: "루시", makes: "작곡" });
  });

  it("marks the persona in use and offers the others and no persona", async () => {
    const at = "2026-11-24T00:00:00+00:00";
    state.data = { ...emptyList(), choice: { persona_id: 2, revision: 3, updated_at: at }, companion: false,
      personas: [{ id: 1, name: "Teacher", text: "Call me 선생님.", fields: { free: "Call me 선생님." }, revision: 1,
        created_at: at, updated_at: at },
      { id: 2, name: "Friend", text: "\n반말로.", fields: { free: "\n반말로." }, revision: 2, created_at: at,
        updated_at: at }] };
    shown();
    expect(await screen.findByText("Friend")).toBeTruthy();
    expect(screen.getAllByText("In use")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Use" })).toHaveLength(2); // Teacher and No persona
    expect(screen.getByText("반말로.")).toBeTruthy(); // the first line with text
    expect(screen.getByText(/personas are not used now/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New persona" }));
    expect((screen.getByLabelText("자유 글") as HTMLTextAreaElement).value).toBe(""); // a new one starts empty
  });
});

describe("an open persona", () => {
  afterEach(cleanup);

  it("keeps its unsaved text: nothing else can replace it until it is saved or cancelled", async () => {
    const at = "2026-11-24T00:00:00+00:00";
    state.data = { ...emptyList(), personas: [
      { id: 1, name: "Teacher", text: "a", fields: { free: "a" }, revision: 1, created_at: at, updated_at: at },
      { id: 2, name: "Friend", text: "b", fields: { free: "b" }, revision: 1, created_at: at, updated_at: at }] };
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

describe("what HIMARI is told", () => {
  it("is made as the server makes it", () => {
    expect(personaText({ free: "  as written \n" })).toBe("  as written \n"); // free alone is the text itself
    expect(personaText({ free: "자유 글.", speech: " 반말 ", name: "루시", examples: "- 왔어?\r\n- 밥은?",
      familiar: "사실 해커였다.\n\n[close] 이미 표시된 줄", close: "  세계를 구하러 왔다.", habits: "  " })).toBe(
      "이름: 루시\n말투: 반말\n대사 예시:\n- 왔어?\n- 밥은?\n[familiar] 사실 해커였다.\n[familiar] 이미 표시된 줄\n" +
      "[close] 세계를 구하러 왔다.\n자유 글."); // the same case as tests/data/test_personas.py
    expect(personaText({})).toBe("");
    expect(personaText({ name: "x﻿ " })).toBe("이름: x﻿"); // Python keeps U+FEFF where trim() drops it
    expect(personaText({ makes: "[close] 몰래 쓰는 일기", speech: "- [familiar] 반말" })).toBe(
      "말투:\n- [familiar] 반말\n하는 일과 만드는 것:\n[close] 몰래 쓰는 일기"); // marks kept at the line's start
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
