// The HIMARI tabs' tests share this: a life to show, and Settings rendered at an address (character-routine §9.2). Each
// test file mocks "@/api/settings" itself (readLife and readLifeHistory as vi.fn) before importing this.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { vi } from "vitest";
import { readLife, readLifeHistory, type Life, type LifeEntry, type LifeHistory } from "@/api/settings";
import { Settings } from "@/routes/Settings";

export const at = "2026-11-25T01:00:00+00:00";
export const entry = (id: number, kind: LifeEntry["kind"], day: string | null, text: string,
  origin: LifeEntry["origin"], revision = 1): LifeEntry =>
  ({ id, kind, day, text, origin, revision, created_at: at, updated_at: at });

/** Today on these pages is 2026-11-26 (a Thursday): only the date is faked, the queries' timers stay real. */
export function onThursday() {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 10, 26, 22, 40));
}

export function aLife(): Life {
  return {
    persona: { id: 2, name: "Friend" },
    enabled: true,
    writer: { running: true, told: "on (deepseek/chat, at 22:00, 200,000 tokens a day each)" },
    entries: [
      entry(5, "fact", null, "고양이를 무서워한다.", "conversation", 3),
      entry(9, "plan", "2026-11-26", "도서관에 간다.", "writer"),
      entry(8, "diary", "2026-11-25", "비가 왔다.\n우산을 잃어버렸다.", "writer"),
      entry(7, "diary", "2026-11-24", "늦잠을 잤다.", "user"),
    ],
    limits: { facts: 200, chars: 500, diary_chars: 1500 },
  };
}

/** The life the reads give, and its history pages in order: the first for no ``before``, each next one for the
 *  ``before`` that is the page before's ``from`` (as often as asked: a change reads them again). */
export function living(life: Life, ...pages: LifeHistory[]) {
  vi.mocked(readLife).mockImplementation(async () => life);
  const history = vi.mocked(readLifeHistory);
  history.mockReset();
  history.mockImplementation(async (before?: string) => {
    const found = before === undefined ? pages[0] : pages.find((_, index) => pages[index - 1]?.from === before);
    if (!found) throw new Error(`no history page before ${before ?? "the life"}`);
    return found;
  });
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname}</p>;
}

/** Settings at ``path``, read (the HIMARI section shown) unless it goes elsewhere. */
export async function shownAt(path = "/settings/himari") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/settings/:section" element={<><Settings /><Where /></>} />
          <Route path="/settings/:section/:tab" element={<><Settings /><Where /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByRole("region", { name: "HIMARI" });
}

export const where = () => screen.getByTestId("where").textContent;
export const part = (name: string) => within(screen.getByRole("region", { name }));
