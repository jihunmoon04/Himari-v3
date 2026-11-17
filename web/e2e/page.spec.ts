// V3-P102: the Web page on a real server (at / since W6). Each check also collects CSP violations and page errors;
// there must be none (§7.2: scripts 'self' only, <style> only with the page's nonce).
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { tokenFile } from "../playwright.config";

const token = () => readFileSync(tokenFile, "ascii").trim();

async function watch(page: Page) {
  const problems: string[] = [];
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      (window as unknown as { __csp: string[] }).__csp ??= [];
      (window as unknown as { __csp: string[] }).__csp.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && /Content Security Policy|Refused to/.test(message.text())) {
      problems.push(`console: ${message.text()}`);
    }
  });
  return async () => {
    const csp = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
    expect([...problems, ...csp]).toEqual([]);
  };
}

async function signIn(page: Page) {
  await page.goto("/");
  await page.getByLabel("Operator token").fill(token());
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "What should we work on?" })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("signs in, refuses a wrong token, and serves the page with one CSP and a fresh nonce", async ({ page }) => {
  const done = await watch(page);
  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toMatch(/^default-src 'self';.*; style-src 'self' 'nonce-[A-Za-z0-9_-]{16,64}'$/);
  const meta = await page.locator('meta[name="csp-nonce"]').getAttribute("content");
  expect(csp).toContain(`'nonce-${meta}'`);
  const again = await page.request.get("/c/anything");
  expect(again.headers()["content-security-policy"]).not.toContain(`'nonce-${meta}'`); // a new one each time
  await page.getByLabel("Operator token").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("That token is not valid.", { timeout: 15_000 });
  await signIn(page);
  await done();
});

test("a task: tool steps, a question, its answer, and safe rich text", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model's steps are used once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("Plan a trip");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/[A-Za-z0-9_-]+$/);
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Plan a trip");
  await expect(page.getByText("HIMARI asks")).toBeVisible();
  await expect(page.getByText("Which city?")).toBeVisible();
  await expect(page.getByRole("button", { name: /Activity · 1 step/ })).toBeVisible();
  await expect(page.getByLabel("Message", { exact: true })).toHaveAttribute("placeholder", "Answer HIMARI");
  await page.getByLabel("Message", { exact: true }).fill("Seoul");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "good" })).toHaveAttribute("href", "https://example.com/");
  await expect(page.getByRole("link", { name: "bad" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  await expect(page.locator(".katex").first()).toBeVisible(); // KaTeX from /assets/katex under the same CSP
  await page.reload();
  await expect(page.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Sidebar" }).getByRole("link").first()).toBeVisible();
  await page.getByRole("button", { name: "New task" }).click(); // the sent task is not offered again
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue("");
  await done();
});

test("an answer streams in, and a second job right after gets its own stream", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model's steps are used once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("one");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toHaveText("Thinking…"); // before the first words
  await expect(page.locator(".answer.opacity-80")).toContainText("Answer 1."); // the words as they come
  await expect(page.locator(".answer.opacity-80")).toHaveCount(0, { timeout: 20_000 }); // recorded
  await expect(page.locator(".answer").filter({ hasText: "Answer 1." })).toBeVisible();
  await page.getByLabel("Message", { exact: true }).fill("two");
  await page.keyboard.press("Enter");
  await expect(page.locator(".answer.opacity-80")).toContainText("Answer 2.");
  await expect(page.locator(".answer").filter({ hasText: "Answer 2." })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".answer.opacity-80")).toHaveCount(0, { timeout: 20_000 });
  await done();
});

test("signed out in another tab: the next send shows the sign-in, the draft stays", async ({ page, baseURL }) => {
  const done = await watch(page);
  await signIn(page);
  const composer = page.getByLabel("Message", { exact: true });
  await composer.fill("lost?");
  expect((await page.request.delete("/api/session", { headers: { origin: baseURL! } })).status()).toBe(204);
  // the page may notice by itself first (a background read is refused); otherwise the send is
  // (it may also notice between the two steps: the composer is gone then, and that is the same outcome)
  if (await composer.isVisible()) await composer.press("Enter", { timeout: 3_000 }).catch(() => undefined);
  await expect(page.getByLabel("Operator token")).toBeVisible();
  await page.getByLabel("Operator token").fill(token());
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue("lost?");
  await done();
});

test("menus and themes keep the CSP: Radix's scroll lock styles carry the nonce", async ({ page, isMobile }) => {
  const done = await watch(page);
  await signIn(page);
  if (isMobile) await page.getByRole("button", { name: "Open sidebar" }).click();
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  // while the menu is open Radix's scroll lock has put a <style> element in: it must carry the page's nonce
  const open = await page.evaluate(() => [...document.querySelectorAll("style")].map((element) => element.nonce));
  const nonce = await page.locator('meta[name="csp-nonce"]').getAttribute("content");
  expect(open.length).toBeGreaterThan(0);
  expect(open.every((value) => value === nonce)).toBe(true);
  await page.getByRole("menuitemradio", { name: "Warm" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-look", "warm");
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "dark");
  const styled = await page.evaluate(() =>
    [...document.querySelectorAll("style")].map((element) => element.nonce || "(none)"));
  expect(styled.filter((nonce) => nonce === "(none)")).toEqual([]);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-look", "warm"); // kept in this browser
  await done();
});

test("the narrow layout has no horizontal scroll and opens the list as a drawer", async ({ page, isMobile }) => {
  test.skip(!isMobile, "phone only");
  const done = await watch(page);
  await signIn(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Open sidebar" }).first().click();
  await expect(page.getByRole("navigation", { name: "Sidebar" })).toBeVisible();
  await page.getByRole("navigation", { name: "Sidebar" }).getByRole("button", { name: "Close sidebar" }).click();
  await expect(page.getByRole("navigation", { name: "Sidebar" })).toHaveCount(0);
  await done();
});

test("an unknown asset is 404, never the page; the old /next/ addresses move to the same place", async ({ request, page }) => {
  expect((await request.get("/assets/nope.js")).status()).toBe(404);
  expect((await request.get("/assets/..%2Findex.html")).status()).toBe(404);
  expect((await request.get("/assets/app.js")).status()).toBe(404); // the classic page is gone
  const moved = await request.get("/next/s/5?x=1", { maxRedirects: 0 });
  expect(moved.status()).toBe(308);
  expect(moved.headers()["location"]).toBe("/s/5?x=1");
  const katex = await request.get("/assets/katex/katex.min.js");
  expect(katex.status()).toBe(200); // where the renderer loads it on the first formula
  const done = await watch(page);
  await signIn(page);
  await page.goto("/next/agents"); // an old bookmark
  await expect(page).toHaveURL(/:\d+\/agents$/);
  await expect(page.getByRole("heading", { name: "Work overview" })).toBeVisible();
  await done();
});

// -- the hub's sessions (V3-P102 W2), against the scripted hub of scripts/web_next_hub.py -----------------------

async function openSidebar(page: Page, isMobile: boolean) {
  if (isMobile) await page.getByRole("button", { name: "Open sidebar" }).first().click();
}

test("the sidebar lists the hub's projects and their open sessions", async ({ page, isMobile }) => {
  const done = await watch(page);
  await signIn(page);
  await openSidebar(page, isMobile);
  const demo = page.getByRole("list", { name: "Sessions of demo" });
  await expect(demo.getByRole("link", { name: /claude@linux/ })).toContainText("1"); // a permission waits
  await expect(demo.getByRole("link", { name: /codex@laptop/ })).toBeVisible();
  await expect(page.getByRole("list", { name: "Sessions of empty" })).toContainText("No open sessions");
  await expect(page.getByRole("link", { name: "Work overview" })).toBeVisible();
  await done();
});

test("a session: its turn, thought, edit with its diff, plan, request and text, then a live line", async ({ page }) => {
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/5");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("demo › claude@linux");
  await expect(page.getByText("Waiting for you").first()).toBeVisible();
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Fix the failing test");
  await expect(page.getByText("The test reads the wrong file.")).toHaveCount(0); // the thought starts folded
  await page.getByRole("button", { name: "Thinking" }).click();
  await expect(page.getByText("The test reads the wrong file.")).toBeVisible();
  const edit = page.getByRole("button", { name: /Edit src\/app\.py/ });
  await expect(edit).toContainText("+2");
  await expect(edit).toContainText("−1");
  await edit.click();
  await expect(page.locator("figure").filter({ hasText: "src/app.py" })).toContainText("z = 4");
  await expect(page.getByRole("group", { name: "Agent request" })).toContainText("Permission · Waiting for you");
  await expect(page.getByRole("group", { name: "Agent request" })).toContainText("Run pytest");
  await expect(page.locator(".answer").filter({ hasText: "I changed y and I am waiting" })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  await expect(page.getByText("A live line from the agent.")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("list", { name: "Plan" }).last()).toContainText("Run the tests");
  await done();
});

test("the overview shows every open session and follows the list as it changes", async ({ page }) => {
  const done = await watch(page);
  await signIn(page);
  await page.goto("/agents");
  const cards = page.getByRole("list", { name: "Open sessions" }).getByRole("listitem");
  await expect(cards.filter({ hasText: "archive" })).toHaveCount(1);
  await expect(cards.filter({ hasText: "demo" })).toHaveCount(2);
  await expect(cards.first()).toContainText("1 waiting for you"); // what waits comes first
  // the scripted hub flips codex@laptop between idle and working every 3 s: the card follows the next flip
  const codex = cards.filter({ hasText: "codex@laptop" });
  await expect(codex).toContainText(/Idle|Working/);
  const was = (await codex.textContent())?.includes("Working") ? "Working" : "Idle";
  await expect(codex).toContainText(was === "Working" ? "Idle" : "Working", { timeout: 8_000 });
  await done();
});

test("a long session shows its latest events and reads the earlier ones on request", async ({ page }) => {
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/7");
  await expect(page.getByText("Prompt 225", { exact: true })).toBeVisible();
  await expect(page.getByText("Prompt 125", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Earlier" }).click();
  await expect(page.getByText("Prompt 26", { exact: true })).toBeAttached();
  await page.getByRole("button", { name: "Earlier" }).click();
  await expect(page.getByText("Prompt 1", { exact: true })).toBeAttached();
  await expect(page.getByRole("button", { name: "Earlier" })).toHaveCount(0); // nothing older
  await done();
});

// -- W3: the user's own work on the hub's sessions (the scripted hub's project "work") ----------------------------

test("a message runs in an idle session; one sent meanwhile waits its turn and can be cancelled", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub's sessions change once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/8");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("work › claude@linux");
  await page.getByLabel("Model").selectOption("sonnet"); // the hub offers it for claude
  await page.getByLabel("Message", { exact: true }).fill("First job");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("article", { name: "Your message" }).first()).toHaveText("First job");
  await expect(page.getByRole("button", { name: "Stop" })).toBeVisible(); // it runs: Stop beside Send
  await page.getByLabel("Message", { exact: true }).fill("Second job");
  await page.keyboard.press("Enter");
  const queue = page.getByLabel("Queued messages");
  await expect(queue).toContainText("Second job");
  await queue.getByRole("button", { name: "Cancel" }).click();
  await expect(queue).toHaveCount(0);
  await expect(page.locator(".answer").filter({ hasText: "Done: First job" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);
  await expect(page.getByText("Done: Second job")).toHaveCount(0); // cancelled: it never ran
  await done();
});

test("a permission is answered in its session, and the agent goes on", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub's requests are answered once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/9");
  const card = page.getByRole("group", { name: "Permission request" });
  await expect(card).toContainText("Write notes.md");
  await expect(card.getByRole("button", { name: "Refuse" })).toHaveCount(0); // the agent's own Reject says it
  await card.getByRole("button", { name: "Allow once" }).click();
  await expect(page.locator(".answer").filter({ hasText: "Allowed: Write notes.md." })).toBeVisible();
  await expect(page.getByRole("group", { name: "Agent request" })).toContainText("Answered");
  await expect(page.getByRole("group", { name: "Permission request" })).toHaveCount(0);
  await done();
});

test("approvals lists what waits; a question is answered there; the tab title counts", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub's requests are answered once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/approvals");
  const question = page.getByRole("group", { name: "Question" });
  await expect(question).toContainText("Which color?");
  await expect(question).toContainText("work · codex · on linux");
  await expect(page.getByRole("group", { name: "Permission request" })).toContainText("Run pytest"); // demo's
  await expect(page).toHaveTitle("(2) HIMARI");
  await question.getByLabel("Color").selectOption("blue");
  await question.getByRole("button", { name: "Answer" }).click();
  await expect(page.getByRole("group", { name: "Question" })).toHaveCount(0);
  await expect(page).toHaveTitle("(1) HIMARI");
  await page.goto("/s/10");
  await expect(page.locator(".answer").filter({ hasText: "You chose blue." })).toBeVisible();
  await done();
});

test("a new agent session from the home page opens once its task runs", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub opens sessions once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Send to").selectOption("work");
  await expect(page.getByLabel("Agent")).toHaveValue("claude");
  await page.getByLabel("Message", { exact: true }).fill("Start fresh");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status").filter({ hasText: "Starting the agent" })).toBeVisible();
  await expect(page).toHaveURL(/\/s\/\d+$/, { timeout: 10_000 });
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Start fresh");
  await expect(page.locator(".answer").filter({ hasText: "Done: Start fresh" })).toBeVisible({ timeout: 10_000 });
  await done();
});

test("a session started before leaving home is told on coming back", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub opens sessions once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Send to").selectOption("work");
  await page.getByLabel("Message", { exact: true }).fill("Start and leave");
  await page.keyboard.press("Enter");
  await page.getByRole("link", { name: "Approvals" }).click(); // within the page: what it follows stays
  await expect(page).toHaveURL(/\/approvals$/);
  await page.getByRole("button", { name: "New task" }).click();
  const told = page.getByRole("status").filter({ hasText: "The claude session in work started." });
  await expect(told).toBeVisible({ timeout: 10_000 });
  await expect(page).toHaveURL(/:\d+\/?$/); // told, not moved
  await told.getByRole("link", { name: "Open it" }).click();
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Start and leave");
  await done();
});

test("a message to a session the agent lost follows its task to the new session", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub loses this session once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/12");
  await page.getByLabel("Message", { exact: true }).fill("Carry on");
  await page.keyboard.press("Enter");
  await expect(page).not.toHaveURL(/\/s\/12$/, { timeout: 10_000 });
  await expect(page.getByRole("status").filter({ hasText: "The agent had lost session #12" })).toBeVisible();
  await expect(page.locator(".answer").filter({ hasText: "Done: Carry on" })).toBeVisible({ timeout: 10_000 });
  await page.goto("/s/12");
  await expect(page.getByText(/This session is closed/)).toBeVisible();
  await done();
});

// -- W4: work HIMARI hands to the hub (the scripted model hands over when a message carries a mark) --------------

test("HIMARI hands work to the hub: its card follows the task to the end and opens the session", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model hands each mark over once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("Please hand this over: W4-HAND-OVER");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/[A-Za-z0-9_-]+$/);
  const card = page.getByRole("group", { name: "Handed to the hub" });
  await expect(card).toContainText("work");
  await expect(card).toContainText("Write the release notes");
  await expect(card).toContainText("Done", { timeout: 20_000 }); // queued, working, done: followed by its task
  await expect(card).toContainText("Done: Write the release notes");
  await expect(page.getByRole("list", { name: "Handed to the hub" }).getByRole("link", { name: /work · claude/ }))
    .toBeVisible(); // under the conversation in the sidebar
  await card.getByRole("link", { name: "Open session" }).click();
  await expect(page).toHaveURL(/\/s\/\d+$/);
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Write the release notes");
  await done();
});

test("a hand-over that fails shows as failed, and one the hub refuses says why", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model hands each mark over once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("This one breaks: W4-FAILS");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/[A-Za-z0-9_-]+$/);
  const cards = page.getByRole("group", { name: "Handed to the hub" });
  await expect(cards.first()).toContainText("Failed", { timeout: 20_000 });
  await expect(cards.first()).toContainText("The tests did not pass.");
  await expect(page.getByText("I handed it to the hub (W4-FAILS).")).toBeVisible();
  await page.getByLabel("Message", { exact: true }).fill("And this: W4-NOWHERE");
  await page.keyboard.press("Enter");
  await expect(cards).toHaveCount(2, { timeout: 15_000 });
  await expect(cards.nth(1)).toContainText("Not handed over");
  await expect(cards.nth(1)).toContainText("모르는 프로젝트: nowhere");
  await expect(cards.nth(1).getByRole("link", { name: "Open session" })).toHaveCount(0);
  await done();
});

// -- W5: the right-hand panels ---------------------------------------------------------------------------------

test("a session's panel: its changes by file in either view, its plan and how it changed, its details", async ({ page, isMobile }) => {
  const done = await watch(page);
  await signIn(page);
  await page.goto("/s/5");
  await page.getByRole("button", { name: "Details" }).click();
  const panel = page.getByRole("complementary", { name: "Details" });
  await expect(panel.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
  const files = panel.getByRole("list", { name: "Changed files" });
  await expect(files).toContainText("src/app.py");
  await expect(files).toContainText("+2");
  await expect(files).toContainText("−1");
  await panel.getByRole("button", { name: "Split" }).click();
  await expect(panel.getByRole("table", { name: "src/app.py before and after" })).toContainText("y = 3");
  await panel.getByRole("tab", { name: "Plan" }).click();
  await expect(panel).toContainText("Latest plan · 1 of 2 done");
  await expect(panel.getByRole("list", { name: "Plan" })).toContainText("Run the tests");
  await panel.getByRole("tab", { name: "Info" }).click();
  await expect(panel).toContainText("linux");
  const box = await panel.boundingBox();
  const viewport = page.viewportSize()!;
  if (isMobile) {
    // a sheet from the bottom, the page's whole width
    expect(Math.round(box!.width)).toBe(viewport.width);
    expect(Math.round(box!.y + box!.height)).toBe(viewport.height);
  } else {
    // dragged wider from its left edge, and kept
    const edge = panel.getByRole("separator", { name: "Resize the panel" });
    const at = (await edge.boundingBox())!;
    await page.mouse.move(at.x + at.width / 2, at.y + 200);
    await page.mouse.down();
    await page.mouse.move(at.x - 120, at.y + 200, { steps: 6 });
    await page.mouse.up();
    const wider = (await panel.boundingBox())!.width;
    expect(wider).toBeGreaterThan(box!.width + 80);
    expect(await page.evaluate(() => window.localStorage.getItem("himari.next.panel.width"))).toBe(String(Math.round(wider)));
  }
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await done();
});

test("a conversation's panel: its folders, what it handed to the hub, its details", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there; the panel itself is checked above");
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Plan a trip" }).click();
  await page.getByRole("button", { name: "Details" }).click();
  const panel = page.getByRole("complementary", { name: "Details" });
  await panel.getByRole("tab", { name: "Files" }).click();
  // this server has no machine: said so, nothing breaks
  await expect(panel.getByRole("tabpanel")).toContainText(/no folders here|not available|machine/i);
  await panel.getByRole("tab", { name: "Hub" }).click();
  await expect(panel).toContainText("Nothing handed to the hub in this conversation.");
  await panel.getByRole("tab", { name: "Info" }).click();
  await expect(panel).toContainText(/conv_/);
  await done();
});

test("a conversation's folder opens in its Files panel after loading, and a file in it opens", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there; the panel itself is checked above");
  const done = await watch(page);
  // This server has no machine, so the browser is handed a folder: the panel must go from its loading render to
  // a folder with the same hooks (React #310 hid behind "no folders here" until production).
  const folder = "/home/coder/HIMARI/conversations/c1";
  await page.route(/\/api\/conversations\/[^/?]+\/workspaces$/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300)); // "Loading…" is rendered first
    await route.fulfill({ json: { items: [{ binding_id: null, path: folder, own: true, working: true }] } });
  });
  await page.route(/\/api\/machine\/list\?/, (route) => route.fulfill({ json: {
    path: folder, parent: null, next: null,
    entries: [{ name: "notes.txt", kind: "file", size: 12 }, { name: "src", kind: "directory" }],
  } }));
  await page.route(/\/api\/machine\/file\?/, (route) => route.fulfill({ json: {
    path: `${folder}/notes.txt`, size: 12, kind: "text", text: "W6-FILES-OK\n", truncated: false,
  } }));
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Plan a trip" }).click();
  await page.getByRole("button", { name: "Details" }).click();
  const panel = page.getByRole("complementary", { name: "Details" });
  await panel.getByRole("tab", { name: "Files" }).click();
  const files = panel.getByRole("tabpanel");
  await expect(files.getByRole("group", { name: "Folders" })).toContainText("This conversation · working");
  await expect(files.getByRole("list", { name: "Folder" })).toContainText("notes.txt");
  await files.getByRole("button", { name: /notes\.txt/ }).click();
  await expect(files).toContainText("W6-FILES-OK");
  await files.getByRole("button", { name: "Back" }).click();
  await expect(files.getByRole("list", { name: "Folder" })).toContainText("src");
  await done();
});

// -- W6a: a conversation's own tools ------------------------------------------------------------------------------

test("a conversation's menu: rename, its Tool servers, the trash with Undo, restore and delete for good", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Plan a trip" }).click();
  const menu = () => page.getByRole("button", { name: "Conversation menu" }).click();

  await menu();
  await page.getByRole("menuitem", { name: "Rename…" }).click();
  await page.getByRole("dialog").getByLabel("Name").fill("Trip plans");
  await page.getByRole("dialog").getByRole("button", { name: "Save" }).click();
  await expect(page.locator("header h1")).toHaveText("Trip plans");
  await expect(page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Trip plans" })).toBeVisible();

  await menu();
  await page.getByRole("menuitem", { name: "Tool servers…" }).click();
  const servers = page.getByRole("list", { name: "Tool servers of this conversation" });
  await expect(servers).toContainText("hub");
  await servers.getByLabel("Use hub").selectOption("off");
  await expect(servers).toContainText("not used");
  await servers.getByLabel("Use hub").selectOption("default");
  await expect(servers).toContainText("in use");
  await page.getByRole("dialog").getByRole("button", { name: "Done" }).click();

  await menu();
  await page.getByRole("menuitem", { name: "Move to trash…" }).click();
  await expect(page.getByRole("dialog")).toContainText("It moves to the trash");
  await page.getByRole("dialog").getByRole("button", { name: "Move to trash" }).click();
  await expect(page).toHaveURL(/:\d+\/?$/);
  const moved = page.getByRole("status").filter({ hasText: "Moved “Trip plans” to the trash" });
  await moved.getByRole("button", { name: "Undo" }).click();
  await expect(page.locator("header h1")).toHaveText("Trip plans");

  await menu();
  await page.getByRole("menuitem", { name: "Move to trash…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Move to trash" }).click();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("menuitem", { name: "Trash" }).click();
  const trashed = page.getByRole("list", { name: "Trashed conversations" }).getByRole("listitem").filter({ hasText: "Trip plans" });
  await trashed.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Trip plans" })).toBeVisible();
  await expect(trashed).toHaveCount(0);

  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Trip plans" }).click();
  await menu();
  await page.getByRole("menuitem", { name: "Move to trash…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Move to trash" }).click();
  await page.goto("/trash");
  await page.getByRole("list", { name: "Trashed conversations" }).getByRole("listitem").filter({ hasText: "Trip plans" })
    .getByRole("button", { name: "Delete for good" }).click();
  await expect(page.getByRole("dialog")).toContainText("This cannot be undone.");
  await page.getByRole("dialog").getByRole("button", { name: "Delete for good" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Deleted “Trip plans” for good." })).toBeVisible();
  await expect(page.getByRole("list", { name: "Trashed conversations" }).getByRole("listitem")
    .filter({ hasText: "Trip plans" })).toHaveCount(0);
  await done();
});

test("an answer is copied as written", async ({ page, context, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: /W4-HAND-OVER/ }).click();
  const answer = page.locator(".group").filter({ hasText: "I handed it to the hub (W4-HAND-OVER)." });
  await answer.hover();
  await answer.getByRole("button", { name: "Copy answer" }).click();
  await expect(answer.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("I handed it to the hub (W4-HAND-OVER).");
  await done();
});

// -- W6b: what the composer sends besides text ------------------------------------------------------------------

const PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

test("files are taken from the button and from a paste; one the server refuses says why and is not sent", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  // This server has no machine runner, so it takes no uploads (V3-P101): each file says so, and the message goes
  // without it. An upload that is taken is checked on a server with a runner.
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: /W4-FAILS/ }).click();
  await page.getByLabel("Files to attach").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  const pending = page.getByLabel("To send with the message");
  await expect(pending).toContainText("notes.txt");
  await expect(pending).toContainText(/failed: .+/);
  await page.getByLabel("Message", { exact: true }).evaluate((area, data) => {
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], "pixel.png", { type: "image/png" }));
    area.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }));
  }, PIXEL);
  await expect(pending).toContainText("pixel.png"); // a pasted image is a file, not text
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue("");
  await page.getByLabel("Message", { exact: true }).fill("Read these");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("article", { name: "Your message" }).filter({ hasText: "Read these" })).toBeVisible();
  await expect(pending).toContainText("notes.txt"); // not sent: it stays, saying why
  await pending.getByRole("button", { name: "Remove notes.txt" }).click();
  await pending.getByRole("button", { name: "Remove pixel.png" }).click();
  await expect(pending).toHaveCount(0);
  await done();
});

test("a / prompt with an argument comes into the composer as text", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: /W4-FAILS/ }).click();
  // the conversation's own composer, not the home page's still on screen while it opens
  const message = page.getByPlaceholder("Message HIMARI (/ for prompts)");
  await expect(page).toHaveURL(/\/c\/[A-Za-z0-9_-]+$/);
  await message.fill("/rev");
  const picker = page.getByRole("listbox", { name: "Prompts" });
  await expect(picker.getByRole("option", { name: /\/review/ })).toBeVisible();
  await message.press("Enter");
  const form = page.getByRole("dialog", { name: "Review something" });
  await form.getByLabel("topic").fill("the docs");
  await form.getByRole("button", { name: "Insert" }).click();
  // as the server expands it: which prompt and arguments, then the prompt's text
  await expect(message).toHaveValue(/^\[MCP prompt hub\/review · topic=the docs\]\s+Review the docs carefully\.$/);
  await message.fill("");
  await done();
});

test("what HIMARI remembers shows as a line that can be undone", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model remembers once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("Note this: W6B-REMEMBER");
  await page.keyboard.press("Enter");
  const line = page.getByRole("note", { name: "Memory" });
  await expect(line).toContainText("Remembered", { timeout: 15_000 });
  await expect(line).toContainText("The user likes green tea.");
  await line.getByRole("button", { name: "Undo" }).click();
  await expect(line).toContainText("Undone");
  await done();
});

// -- W6c: settings ---------------------------------------------------------------------------------------------

test("settings: instructions kept, notes added, edited and deleted, the Tool servers' defaults", async ({ page, isMobile }) => {
  test.skip(isMobile, "settings change once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("menuitem", { name: "Instructions" }).click();
  const instructions = page.getByRole("textbox", { name: "Instructions" });
  await instructions.fill("Answer briefly.");
  await page.getByRole("region", { name: "Instructions" }).getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Instructions saved" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Instructions" })).toHaveValue("Answer briefly.");

  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Memory" }).click();
  const fresh = page.getByRole("form", { name: "New note" });
  await fresh.getByLabel("Note").fill("The user drinks coffee.");
  await fresh.getByRole("button", { name: "Add note" }).click();
  const notes = page.getByRole("list", { name: "Notes" });
  const coffee = notes.getByRole("listitem").filter({ hasText: "The user drinks coffee." });
  await expect(coffee).toBeVisible();
  await coffee.getByRole("button", { name: "Edit" }).click();
  await notes.getByRole("form", { name: "Edit note" }).getByLabel("Note").fill("The user drinks tea now.");
  await notes.getByRole("form", { name: "Edit note" }).getByRole("button", { name: "Save" }).click();
  const tea = notes.getByRole("listitem").filter({ hasText: "The user drinks tea now." });
  await expect(tea).toBeVisible();
  await tea.getByRole("button", { name: /Delete note/ }).click();
  await expect(notes.getByRole("listitem").filter({ hasText: "The user drinks tea now." })).toHaveCount(0);

  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Tool servers" }).click();
  const hubDefault = page.getByLabel("hub on by default");
  await expect(hubDefault).toBeChecked();
  // the box shows what the server saved: it turns once the save answers
  await hubDefault.click();
  await expect(hubDefault).not.toBeChecked();
  await hubDefault.click();
  await expect(hubDefault).toBeChecked();

  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "Folders" }).click();
  await expect(page.getByRole("region", { name: "Folders" })).toContainText("not available here");
  await done();
});

test("settings: a device added shows its pairing code once, is renamed and revoked; quiet starts and ends", async ({ page, isMobile }) => {
  test.skip(isMobile, "settings change once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/settings/devices");
  await page.getByLabel("New device name").fill("Test phone");
  await page.getByRole("button", { name: "Add device" }).click();
  await expect(page.getByLabel("Pairing code")).toHaveText(/\S{4,}/);
  const devices = page.getByRole("list", { name: "Devices" });
  const phone = devices.getByRole("listitem").filter({ hasText: "Test phone" });
  await expect(phone).toContainText("not paired yet");
  await phone.getByRole("button", { name: "Rename" }).click();
  await devices.getByLabel("Device name").fill("Test tablet");
  await devices.getByRole("button", { name: "Save" }).click();
  const tablet = devices.getByRole("listitem").filter({ hasText: "Test tablet" });
  await expect(tablet).toBeVisible();
  page.once("dialog", (dialog) => void dialog.accept());
  await tablet.getByRole("button", { name: "Revoke" }).click();
  await expect(devices.getByRole("listitem").filter({ hasText: "Test tablet" })).toHaveCount(0);
  const quiet = page.getByLabel("Quiet");
  await quiet.getByRole("button", { name: "Quiet for an hour" }).click();
  await expect(quiet).toContainText("Quiet until");
  await quiet.getByRole("button", { name: "End quiet" }).click();
  await expect(quiet).toContainText("Not quiet.");
  await done();
});

// -- W6d: drafts across the switch ---------------------------------------------------------------------------------

test("a draft the classic page kept comes into the new page; the user's own sign-out wipes drafts", async ({ page, isMobile }) => {
  test.skip(isMobile, "drafts are checked once, on desktop");
  const done = await watch(page);
  await page.goto("/");
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const request = indexedDB.open("himari-local", 1); // as the classic page's local_store.js made it
    request.onupgradeneeded = () => { request.result.createObjectStore("drafts"); request.result.createObjectStore("unsaved"); };
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const tx = request.result.transaction("drafts", "readwrite");
      tx.objectStore("drafts").put({ text: "From the classic page", references: [], writer: "w1" }, "home");
      tx.oncomplete = () => { request.result.close(); resolve(); };
    };
  }));
  await page.getByLabel("Operator token").fill(token());
  await page.getByRole("button", { name: "Sign in" }).click();
  const message = page.getByLabel("Message", { exact: true });
  await expect(message).toHaveValue("From the classic page");
  await expect.poll(() => page.evaluate(async () => (await indexedDB.databases()).map((d) => d.name)))
    .not.toContain("himari-local"); // moved, and the old store deleted
  await message.fill("A secret draft");
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await page.getByLabel("Operator token").fill(token());
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Message", { exact: true })).toHaveValue("");
  await done();
});
