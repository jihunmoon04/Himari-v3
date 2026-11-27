// Settings → HIMARI → Projects (character-world §4, character-work §8; character-routine §9.2): the open projects
// with their so far and pieces (newest first), then the closed ones with how they ended; a project's title and goal
// and a piece's title, summary and text edited; the worker and the next work time.
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { editLife, endProject } from "@/api/settings";
import { toast } from "@/features/toast";
import { aLife, entry, living, onThursday, part, shownAt } from "./testing";

vi.mock("@/api/settings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/settings")>()),
  readLife: vi.fn(),
  readLifeHistory: vi.fn(),
  editLife: vi.fn(async () => ({})),
  deleteLife: vi.fn(async () => undefined),
  endProject: vi.fn(async () => ({})),
}));
vi.mock("@/features/toast", () => ({ toast: vi.fn() }));

const shown = () => shownAt("/settings/himari/projects");

describe("the Projects tab", () => {
  beforeEach(() => { vi.clearAllMocks(); onThursday(); living(aLife()); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("shows the open projects with their so far and pieces, then the closed ones, and the next work time",
    async () => {
      const plan = { ...entry(21, "plan", "2026-11-26", "냉각부 도면 그려야지.", "writer"), time: "20:00", arc_id: 11 };
      living({
        ...aLife(),
        work: { running: true, next: plan },
        entries: [
          { ...entry(11, "arc", "2026-11-10", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open",
            end_day: null, so_far: "포신과 냉각부까지 정했다." },
          { ...entry(12, "arc", "2026-11-12", "매일 한 곡", "writer"), title: "작곡 노트", status: "open", end_day: null },
          { ...entry(10, "arc", "2026-11-01", "매일 십 분", "writer", 2), title: "우쿨렐레", status: "closed",
            end_day: "2026-11-20", ending: "한 곡을 끝까지 쳤다." },
          plan,
          { ...entry(31, "work", "2026-11-26", "첫 스케치.", "writer"), time: "20:00", title: "포신 스케치",
            summary: "세 겹 방열판", arc_id: 11, plan_id: 22, draft: true },
          { ...entry(30, "work", "2026-11-25", "냉각부 설계.\n증발열은 포신 뒤로.", "writer"), time: "20:00",
            title: "냉각부", summary: "냉각부 설계 끝냈어.", arc_id: 11, plan_id: 20 },
        ],
      });
      await shown();
      expect(screen.getByLabelText("Work status").textContent).toBe(
        "Work: on · next: 2026-11-26 20:00 냉각부 도면 그려야지. (블랙홀 전자포)");
      const project = within(part("Open projects").getByLabelText("Project 블랙홀 전자포"));
      expect(project.getByText("설정집 한 권")).toBeTruthy(); // its goal
      expect(project.getByText("So far: 포신과 냉각부까지 정했다.")).toBeTruthy();
      expect(project.getByText("since 2026-11-10 · open · written by the story writer")).toBeTruthy();
      expect(project.getAllByText(/^2026-11-2\d 20:00 · /).map((line) => line.textContent)).toEqual([
        "2026-11-26 20:00 · written by the story writer", "2026-11-25 20:00 · written by the story writer"]);
      expect(project.getByText("draft")).toBeTruthy();
      expect(project.getByText("Show the draft")).toBeTruthy();
      expect(project.getByText("Show the piece")).toBeTruthy();
      expect(within(part("Open projects").getByLabelText("Project 작곡 노트")).getByText("Nothing made yet.")).toBeTruthy();
      const closed = within(part("Closed projects").getByLabelText("Project 우쿨렐레"));
      expect(closed.getByText("2026-11-01 – 2026-11-20 · closed · written by the story writer")).toBeTruthy();
      expect(closed.getByText("Ending: 한 곡을 끝까지 쳤다.")).toBeTruthy();
      expect(part("Open projects").queryByText("우쿨렐레")).toBeNull();
    });

  it("edits a project's title and goal, and a piece's title, summary and text", async () => {
    living({
      ...aLife(),
      entries: [
        { ...entry(11, "arc", "2026-11-10", "풍경화 한 장", "writer"), title: "그림 연습", status: "open", end_day: null },
        { ...entry(30, "work", "2026-11-25", "냉각부 설계.\n증발열은 포신 뒤로.", "writer"), time: "20:00",
          title: "냉각부", summary: "냉각부 설계 끝냈어.", arc_id: 11, plan_id: 20 },
      ],
      limits: { ...aLife().limits, arc_chars: 200, arc_title_chars: 60, work_chars: 3000, summary_chars: 200 },
    });
    await shown();
    const project = within(part("Open projects").getByLabelText("Project 그림 연습"));
    fireEvent.click(project.getAllByRole("button", { name: "Edit" })[0] as HTMLElement); // the project's
    const title = screen.getByLabelText("Arc title") as HTMLInputElement;
    const goal = screen.getByLabelText("Arc goal") as HTMLTextAreaElement;
    expect([title.value, title.maxLength, goal.value, goal.maxLength]).toEqual(["그림 연습", 60, "풍경화 한 장", 200]);
    fireEvent.change(title, { target: { value: " 수채화 연습 " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(11, "풍경화 한 장", 1, "수채화 연습"));
    await waitFor(() => expect(screen.queryByLabelText("Arc title")).toBeNull());

    fireEvent.click(project.getAllByRole("button", { name: "Edit" })[1] as HTMLElement); // the piece's
    const summary = screen.getByLabelText("Piece summary") as HTMLInputElement;
    expect([summary.value, summary.maxLength]).toEqual(["냉각부 설계 끝냈어.", 200]);
    expect((screen.getByLabelText("Piece text") as HTMLTextAreaElement).maxLength).toBe(3000);
    fireEvent.change(summary, { target: { value: " 냉각부 끝! " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(editLife).toHaveBeenCalledWith(
      30, "냉각부 설계.\n증발열은 포신 뒤로.", 1, "냉각부", "냉각부 끝!"));
  });

  it("shows a piece of an open project older than the 30 days sent (not a stray date in Records)", async () => {
    living({
      ...aLife(),
      since: "2026-10-27",
      entries: [
        { ...entry(11, "arc", "2026-10-01", "설정집 한 권", "writer"), title: "블랙홀 전자포", status: "open" },
        { ...entry(30, "work", "2026-10-12", "포신.", "writer"), time: "20:00", title: "포신", summary: "포신 끝.",
          arc_id: 11, plan_id: 20 },
      ],
    });
    await shown();
    expect(within(part("Open projects").getByLabelText("Project 블랙홀 전자포")).getByText("포신 끝.")).toBeTruthy();
  });

  it("ends an open project with a one-line ending, from the revision opened, and says why it was last changed",
    async () => {
      living({
        ...aLife(),
        entries: [
          { ...entry(11, "arc", "2026-11-10", "풍경화 한 장", "himari", 3), title: "그림 연습", status: "open",
            end_day: null, reason: "대화하다가 그리고 싶어졌어." },
          { ...entry(10, "arc", "2026-11-01", "매일 십 분", "writer", 2), title: "우쿨렐레", status: "closed",
            end_day: "2026-11-20", ending: "한 곡을 끝까지 쳤다." },
        ],
      });
      await shown();
      const project = within(part("Open projects").getByLabelText("Project 그림 연습"));
      expect(project.getByText("Why: 대화하다가 그리고 싶어졌어.")).toBeTruthy();
      expect(project.getByText("since 2026-11-10 · open · by HIMARI")).toBeTruthy();
      expect(within(part("Closed projects").getByLabelText("Project 우쿨렐레"))
        .queryByRole("button", { name: "End project" })).toBeNull(); // a closed one is never ended again
      fireEvent.click(project.getByRole("button", { name: "End project" }));
      expect(project.getAllByRole("button", { name: "Edit" }).every((b) => (b as HTMLButtonElement).disabled))
        .toBe(true); // one editor at a time
      const ending = screen.getByLabelText("Ending") as HTMLInputElement;
      expect(ending.maxLength).toBe(200);
      fireEvent.change(ending, { target: { value: " 한 장 다 그렸다. " } });
      vi.spyOn(window, "confirm").mockReturnValueOnce(false);
      fireEvent.click(screen.getByRole("button", { name: "End project" }));
      expect(endProject).not.toHaveBeenCalled(); // asked first
      vi.spyOn(window, "confirm").mockReturnValueOnce(true);
      fireEvent.click(screen.getByRole("button", { name: "End project" }));
      await waitFor(() => expect(endProject).toHaveBeenCalledWith(11, "한 장 다 그렸다.", 3));
      await waitFor(() => expect(screen.queryByLabelText("Ending")).toBeNull());
    });

  it("keeps the ending when the project was changed elsewhere", async () => {
    vi.mocked(endProject).mockRejectedValueOnce(new ApiError(409, "revision_conflict", "changed"));
    living({
      ...aLife(),
      entries: [{ ...entry(11, "arc", "2026-11-10", "풍경화 한 장", "writer"), title: "그림 연습", status: "open" }],
    });
    await shown();
    fireEvent.click(screen.getByRole("button", { name: "End project" }));
    fireEvent.change(screen.getByLabelText("Ending"), { target: { value: "끝." } });
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole("button", { name: "End project" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/changed elsewhere/), "error"));
    expect((screen.getByLabelText("Ending") as HTMLInputElement).value).toBe("끝.");
  });

  it("says when there are no projects and the worker is off", async () => {
    living({ ...aLife(), work: { running: false, next: null } });
    await shown();
    expect(screen.getByLabelText("Work status").textContent).toBe("Work: off (it runs with the story writer)");
    expect(part("Open projects").getByText("No open projects.")).toBeTruthy();
    expect(part("Closed projects").getByText("No project ended lately.")).toBeTruthy();
  });
});
