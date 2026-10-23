import fs from "node:fs";
import path from "node:path";
import { expect, test, type BrowserContext } from "@playwright/test";

// Sign-out clears this app's account- and league-specific browser data (STATUS 1t, lib/local-data.ts), so the next
// account on the same browser starts clean, and keeps device preferences such as the theme. The multi-league harness
// (scripts/e2e_multileague.py) has one synthetic viewer, so "account B" is a fresh sign-in on the same browser after
// account A signed out: what B sees depends only on what is left in this browser's storage, not on who B is.
type Manifest = { leagues: Record<"espn_public" | "espn_private" | "sleeper", string>; cookie: { name: string; value: string } };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

async function signIn(context: BrowserContext) {
  const { cookie } = manifest();
  await context.addCookies([{ name: cookie.name, value: cookie.value, domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
}

test("after account A signs out, account B on the same browser sees none of A's saved views or recents", async ({ page, context }) => {
  test.setTimeout(120_000);
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
  const { espn_public: a } = manifest().leagues;
  const views = page.getByRole("group", { name: "Rankings views" });
  const dialog = page.getByRole("dialog", { name: "Search" });
  const input = dialog.getByRole("combobox", { name: "Search players and pages" });

  // Account A: saves a view and picks a player in the palette, in league A.
  await signIn(context);
  await page.goto(`/l/${a}/rankings?pos=RB`);
  await expect(views).toBeVisible({ timeout: 30_000 });
  await views.locator("summary").click();
  await views.getByLabel("Save current filters as").fill("A's RBs");
  await views.getByRole("button", { name: "Save view" }).click();
  await expect(views.getByRole("status")).toHaveText("Saved “A's RBs” on this device for this league.");
  await page.keyboard.press("Control+k");
  await input.fill("Kilo");
  await dialog.getByRole("option", { name: /^Kilo RB/ }).click();
  await expect(page).toHaveURL(new RegExp(`/l/${a}/players\\?id=207`));
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Kilo RB", { timeout: 30_000 });
  await page.evaluate(() => localStorage.setItem("fantasy-intel:theme", "light"));   // a device preference, kept

  // A signs out from the account menu.
  await page.locator("summary", { hasText: "Account" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await page.waitForURL((u) => !u.search.includes("id=207"), { timeout: 30_000 });
  const left = await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }));
  expect(left.local.filter((k) => k.startsWith("fi:"))).toEqual([]);
  expect(left.local).toContain("fantasy-intel:theme");
  expect(left.session.filter((k) => k.startsWith("fi:") || k.startsWith("fantasy-intel:team"))).toEqual([]);

  // Account B signs in on the same browser and opens the same league: no saved view, no Recent.
  await context.clearCookies();
  await signIn(context);
  await page.goto(`/l/${a}/rankings`);
  await expect(views).toBeVisible({ timeout: 30_000 });
  await views.locator("summary").click();
  await expect(views).toContainText("None yet.");
  await expect(views.getByRole("link", { name: "A's RBs", exact: true })).toHaveCount(0);
  await page.keyboard.press("Control+k");
  await expect(input).toBeFocused();
  await expect(dialog.getByRole("group", { name: "Recent" })).toHaveCount(0);
  await expect(dialog).not.toContainText("Kilo RB");
  expect(await page.evaluate(() => localStorage.getItem("fantasy-intel:theme"))).toBe("light");
});
