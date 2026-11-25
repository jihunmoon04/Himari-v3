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

test("a conversation: a tool step, then safe rich text", async ({ page, isMobile }) => {
  test.skip(isMobile, "the scripted model's steps are used once, on desktop");
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("Plan a trip");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/[A-Za-z0-9_-]+$/);
  await expect(page.getByRole("article", { name: "Your message" })).toHaveText("Plan a trip");
  await expect(page.getByRole("heading", { name: "Plan", exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /Activity · 1 step/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "good" })).toHaveAttribute("href", "https://example.com/");
  await expect(page.getByRole("link", { name: "bad" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  await expect(page.locator(".katex").first()).toBeVisible(); // KaTeX from /assets/katex under the same CSP
  await page.reload();
  await expect(page.getByRole("heading", { name: "Plan", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Sidebar" }).getByRole("link").first()).toBeVisible();
  await page.getByRole("button", { name: "New conversation" }).click(); // the sent message is not offered again
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
  const moved = await request.get("/next/c/5?x=1", { maxRedirects: 0 });
  expect(moved.status()).toBe(308);
  expect(moved.headers()["location"]).toBe("/c/5?x=1");
  const katex = await request.get("/assets/katex/katex.min.js");
  expect(katex.status()).toBe(200); // where the renderer loads it on the first formula
  const done = await watch(page);
  await signIn(page);
  await page.goto("/next/trash"); // an old bookmark
  await expect(page).toHaveURL(/:\d+\/trash$/);
  await expect(page.getByRole("heading", { name: "Trash" })).toBeVisible();
  await done();
});

// -- W5: the right-hand panel -----------------------------------------------------------------------------------

test("a conversation's panel: its files and its details", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: "Plan a trip" }).click();
  await page.getByRole("button", { name: "Details" }).click();
  const panel = page.getByRole("complementary", { name: "Details" });
  await panel.getByRole("tab", { name: "Files" }).click();
  await expect(panel.getByRole("tabpanel")).toContainText("No files in this conversation yet.");
  await panel.getByRole("tab", { name: "Info" }).click();
  await expect(panel.getByRole("tabpanel")).toContainText("Started");
  await panel.getByRole("button", { name: "Close details" }).click();
  await expect(panel).toHaveCount(0);
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
  await expect(servers).toContainText("notes");
  await servers.getByLabel("Use notes").selectOption("off");
  await expect(servers).toContainText("not used");
  await servers.getByLabel("Use notes").selectOption("default");
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
  test.skip(isMobile, "the scripted model's steps are used once, on desktop");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const done = await watch(page);
  await signIn(page);
  await page.getByLabel("Message", { exact: true }).fill("Copy this: W6A-COPY");
  await page.keyboard.press("Enter");
  const answer = page.locator(".group").filter({ hasText: "A bold answer (W6A-COPY)." });
  await expect(answer).toBeVisible({ timeout: 15_000 });
  await answer.hover();
  await answer.getByRole("button", { name: "Copy answer" }).click();
  await expect(answer.getByRole("button", { name: "Copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("A **bold** answer (W6A-COPY).");
  await done();
});

// -- W6b: what the composer sends besides text ------------------------------------------------------------------

const PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

test("files are taken from the button and from a paste; one the server refuses says why and is not sent", async ({ page, isMobile }) => {
  test.skip(isMobile, "the conversations list is a drawer there");
  // This server gives no Conversation a folder (machine_principals=(), V3-P109), so it takes no uploads: each file
  // says so, and the message goes without it. An upload that is taken is checked in the Python tests.
  const done = await watch(page);
  await signIn(page);
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: /W6A-COPY/ }).click();
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
  await page.getByRole("list", { name: "Conversations" }).getByRole("link", { name: /W6A-COPY/ }).click();
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
  await expect(message).toHaveValue(/^\[MCP prompt notes\/review · topic=the docs\]\s+Review the docs carefully\.$/);
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

  await page.getByRole("navigation", { name: "Settings sections" }).getByRole("link", { name: "You and HIMARI" }).click();
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
  const notesDefault = page.getByLabel("notes on by default");
  await expect(notesDefault).toBeChecked();
  // the box shows what the server saved: it turns once the save answers
  await notesDefault.click();
  await expect(notesDefault).not.toBeChecked();
  await notesDefault.click();
  await expect(notesDefault).toBeChecked();
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
