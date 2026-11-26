// character-day review P2 6: a conversation's events read page by page — by what the server says of its page (``more``,
// ``last_sequence``), never by how many rows it shows.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { get } from "./client";
import { EVENTS_PAGE, readAllEvents, type EventsPage, type EventView } from "./himari";

vi.mock("./client", async (original) => ({ ...(await original<typeof import("./client")>()), get: vi.fn() }));

const row = (sequence: number, kind = "user_message"): EventView => ({ sequence, kind, created_at: "2026-11-26T10:00:00Z" });
const rows = (from: number, count: number) => Array.from({ length: count }, (_, index) => row(from + index));

function pages(...given: EventsPage[]) {
  const asked: string[] = [];
  vi.mocked(get).mockImplementation(async (path: string) => {
    asked.push(path);
    const next = given.shift();
    if (!next) throw new Error(`no page for ${path}`);
    return next as never; // get is generic: the page is what this caller reads it as
  });
  return asked;
}

describe("readAllEvents", () => {
  beforeEach(() => { vi.mocked(get).mockReset(); }); // a block: a function returned is taken for a cleanup

  it("goes on past a page that shows nothing while the server says more, from the cursor it gives", async () => {
    const asked = pages(
      { items: [], last_sequence: 1, more: true }, // a wake not shown yet was the page's only event
      { items: [row(2), { ...row(2, "diary"), id: "w1" }], last_sequence: 2, more: false },
    );
    const read = await readAllEvents("c1");
    expect(asked).toEqual([
      `/api/conversations/c1/events?after=0&limit=${EVENTS_PAGE}`,
      `/api/conversations/c1/events?after=1&limit=${EVENTS_PAGE}`,
    ]);
    expect(read.items.map((item) => item.kind)).toEqual(["user_message", "diary"]);
    expect(read.last_sequence).toBe(2);
  });

  it("goes on past a short page while the server says more, and stops at a long one when it says no more", async () => {
    const asked = pages(
      { items: rows(1, EVENTS_PAGE - 1), last_sequence: EVENTS_PAGE, more: true }, // one hidden: a row short
      { items: [...rows(EVENTS_PAGE + 1, 3), { ...row(EVENTS_PAGE + 3, "diary"), id: "w2" }], last_sequence: EVENTS_PAGE + 3,
        more: false },
    );
    const read = await readAllEvents("c1");
    expect(asked).toHaveLength(2);
    expect(read.items).toHaveLength(EVENTS_PAGE + 3);
  });

  it("keeps the old rule with an older server: a full page may have more, a short one was the last", async () => {
    const asked = pages(
      { items: rows(1, EVENTS_PAGE), last_sequence: EVENTS_PAGE },
      { items: rows(EVENTS_PAGE + 1, 2), last_sequence: EVENTS_PAGE + 2 },
    );
    const read = await readAllEvents("c1");
    expect(asked).toHaveLength(2);
    expect(read.last_sequence).toBe(EVENTS_PAGE + 2);
  });

  it("stops when the cursor does not move, whatever the server says", async () => {
    const asked = pages({ items: [], last_sequence: 4, more: true });
    const read = await readAllEvents("c1", 4);
    expect(asked).toHaveLength(1);
    expect(read).toEqual({ items: [], last_sequence: 4 });
  });
});
