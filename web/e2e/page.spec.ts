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
  expect((await page.request.delete("/api/session", { headers: { origin: baseURL! } })).status()).toBe(204);
  await page.getByLabel("Message", { exact: true }).fill("lost?");
  await page.keyboard.press("Enter");
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
