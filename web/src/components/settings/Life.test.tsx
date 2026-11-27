// Settings → HIMARI (character-life §6, character-routine §9.2): whose life it is and whether it is used, above five
// tabs at /settings/himari/<tab> — Today when none is named, an unknown one back to it, the old address moved with its
// tab — each tab its own (./himari/*.test.tsx).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Settings } from "@/routes/Settings";
import { aLife, living, onThursday, part, shownAt, where } from "./himari/testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const tabs = () => screen.getByRole("navigation", { name: "HIMARI tabs" });
const current = () => tabs().querySelector("[aria-current=page]")?.textContent;

describe("the HIMARI section", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(aLife()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("says whose life it is, above the tabs, and opens on Today", async () => {
    await shownAt("/settings/himari");
    expect(await screen.findByText("Friend's life")).toBeTruthy();
    expect(screen.queryByText(/not used now/)).toBeNull();
    expect([...tabs().querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Today", "/settings/himari/today"], ["Schedule", "/settings/himari/schedule"],
      ["Records", "/settings/himari/records"], ["Projects", "/settings/himari/projects"],
      ["About HIMARI", "/settings/himari/about"]]);
    expect(current()).toBe("Today");
    expect(screen.getByRole("region", { name: "Today and tomorrow" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Facts" })).toBeNull();
  });

  it("shows the default's life and says when the life is not used", async () => {
    living({ ...aLife(), persona: null, enabled: false, entries: [] });
    await shownAt("/settings/himari/about");
    expect(await screen.findByText("Default (no persona)")).toBeTruthy();
    expect(screen.getByText(/not used now/)).toBeTruthy();
    expect(part("Facts").getByText("No facts yet.")).toBeTruthy();
  });

  it("goes from tab to tab by address, and back to Today from one it does not know", async () => {
    await shownAt("/settings/himari/records");
    expect(current()).toBe("Records");
    expect(screen.getByRole("region", { name: "Records" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Projects" }));
    expect(where()).toBe("/settings/himari/projects");
    expect(current()).toBe("Projects");
    expect(screen.getByRole("region", { name: "Open projects" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "About HIMARI" }));
    expect(screen.getByRole("region", { name: "Facts" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Schedule" }));
    expect(screen.getByRole("region", { name: "Schedule" })).toBeTruthy();
    cleanup();

    await shownAt("/settings/himari/sleep-log");
    expect(where()).toBe("/settings/himari");
    expect(current()).toBe("Today");
  });

  it("asks before a tab change throws away changes not saved, and not when there are none (review 5)", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await shownAt("/settings/himari/about");
    fireEvent.click(part("Facts").getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("link", { name: "Records" })); // opened, nothing changed: no question
    expect(confirm).not.toHaveBeenCalled();
    expect(where()).toBe("/settings/himari/records");
    fireEvent.click(screen.getByRole("link", { name: "About HIMARI" }));
    fireEvent.click(part("Facts").getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Entry text"), { target: { value: "고친 사실." } });
    fireEvent.click(screen.getByRole("link", { name: "Projects" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(where()).toBe("/settings/himari/about"); // stayed, with the text
    expect((screen.getByLabelText("Entry text") as HTMLTextAreaElement).value).toBe("고친 사실.");
    fireEvent.click(screen.getByRole("link", { name: "Projects" }));
    expect(where()).toBe("/settings/himari/projects");
    fireEvent.click(screen.getByRole("link", { name: "About HIMARI" })); // the editor closed with the tab
    fireEvent.click(screen.getByRole("link", { name: "Today" }));
    expect(confirm).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it("keeps the old address of the section, with its tab, and drops a tab of another section", async () => {
    await shownAt("/settings/life/records");
    expect(where()).toBe("/settings/himari/records");
    expect(current()).toBe("Records");
    cleanup();
    await shownAt("/settings/life");
    expect(where()).toBe("/settings/himari");
    cleanup();
    function Where() { return <p data-testid="where">{useLocation().pathname}</p>; }
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/settings/tools/today"]}>
          <Routes>
            <Route path="/settings/:section" element={<Where />} />
            <Route path="/settings/:section/:tab" element={<Settings />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(where()).toBe("/settings/tools");
  });
});
