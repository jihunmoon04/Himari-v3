// V3-P102 W1: the new page at /next/ on a real server. Each check also collects CSP violations and page errors;
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
  await page.goto("/next/");
  await page.getByLabel("Operator token").fill(token());
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "What should we work on?" })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("signs in, refuses a wrong token, and serves the page with one CSP and a fresh nonce", async ({ page }) => {
  const done = await watch(page);
  const response = await page.goto("/next/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toMatch(/^default-src 'self';.*; style-src 'self' 'nonce-[A-Za-z0-9_-]{16,64}'$/);
  const meta = await page.locator('meta[name="csp-nonce"]').getAttribute("content");
  expect(csp).toContain(`'nonce-${meta}'`);
  const again = await page.request.get("/next/c/anything");
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
  await expect(page).toHaveURL(/\/next\/c\/[A-Za-z0-9_-]+$/);
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
  if (await composer.isVisible()) await composer.press("Enter");
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

test("an unknown asset is 404, never the page", async ({ request }) => {
  expect((await request.get("/next/assets/nope.js")).status()).toBe(404);
  expect((await request.get("/next/assets/..%2Findex.html")).status()).toBe(404);
  const redirect = await request.get("/next", { maxRedirects: 0 });
  expect(redirect.status()).toBe(308);
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
  await page.goto("/next/s/5");
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
  await page.goto("/next/agents");
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
  await page.goto("/next/s/7");
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
  await page.goto("/next/s/8");
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
  await page.goto("/next/s/9");
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
  await page.goto("/next/approvals");
  const question = page.getByRole("group", { name: "Question" });
  await expect(question).toContainText("Which color?");
  await expect(question).toContainText("work · codex · on linux");
  await expect(page.getByRole("group", { name: "Permission request" })).toContainText("Run pytest"); // demo's
  await expect(page).toHaveTitle("(2) HIMARI");
  await question.getByLabel("Color").selectOption("blue");
  await question.getByRole("button", { name: "Answer" }).click();
  await expect(page.getByRole("group", { name: "Question" })).toHaveCount(0);
  await expect(page).toHaveTitle("(1) HIMARI");
  await page.goto("/next/s/10");
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
  await expect(page).toHaveURL(/\/next\/s\/\d+$/, { timeout: 10_000 });
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
  await expect(page).toHaveURL(/\/next\/approvals$/);
  await page.getByRole("button", { name: "New task" }).click();
  const told = page.getByRole("status").filter({ hasText: "The claude session in work started." });
  await expect(told).toBeVisible({ timeout: 10_000 });
  await expect(page).toHaveURL(/\/next\/?$/); // told, not moved
  await told.getByRole("link", { name: "Open it" }).click();
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Start and leave");
  await done();
});

test("a message to a session the agent lost follows its task to the new session", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted hub loses this session once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.goto("/next/s/12");
  await page.getByLabel("Message", { exact: true }).fill("Carry on");
  await page.keyboard.press("Enter");
  await expect(page).not.toHaveURL(/\/next\/s\/12$/, { timeout: 10_000 });
  await expect(page.getByRole("status").filter({ hasText: "The agent had lost session #12" })).toBeVisible();
  await expect(page.locator(".answer").filter({ hasText: "Done: Carry on" })).toBeVisible({ timeout: 10_000 });
  await page.goto("/next/s/12");
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
  await expect(page).toHaveURL(/\/next\/c\/[A-Za-z0-9_-]+$/);
  const card = page.getByRole("group", { name: "Handed to the hub" });
  await expect(card).toContainText("work");
  await expect(card).toContainText("Write the release notes");
  await expect(card).toContainText("Done", { timeout: 20_000 }); // queued, working, done: followed by its task
  await expect(card).toContainText("Done: Write the release notes");
  await expect(page.getByRole("list", { name: "Handed to the hub" }).getByRole("link", { name: /work · claude/ }))
    .toBeVisible(); // under the conversation in the sidebar
  await card.getByRole("link", { name: "Open session" }).click();
  await expect(page).toHaveURL(/\/next\/s\/\d+$/);
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Write the release notes");
  await done();
});

test("a hand-over that fails shows as failed, and one the hub refuses says why", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model hands each mark over once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("This one breaks: W4-FAILS");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/next\/c\/[A-Za-z0-9_-]+$/);
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
