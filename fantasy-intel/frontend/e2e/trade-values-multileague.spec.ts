import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

// Trade values board (issue #184) on the multi-league site (playwright.multileague.config.ts): every link stays in the
// league of the page (/l/<lg>/...), and the analyzer link keeps the analyzed team from the URL. The ESPN fixture league
// (scripts/build_e2e_db.py) has teams 1-4; its values come from the scoped /v2 API.
type Manifest = { leagues: Record<"espn_public" | "sleeper", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

for (const width of [1440, 375]) {
  test(`links stay in the league and keep the analyzed team (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const { espn_public: lg } = manifest().leagues;
    const errors: string[] = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(`/l/${lg}/trades/values?team=4&pos=WR`);
    await expect(page.locator("main h1:visible")).toHaveText("Trade values", { timeout: 30_000 });
    const table = page.locator("main table").first();
    const trade = table.locator("a", { hasText: /^Trade (for|away)/ });
    expect(await trade.count()).toBeGreaterThan(0);
    for (const href of await trade.evaluateAll((as) => as.map((a) => a.getAttribute("href")))) {
      expect(href).toMatch(new RegExp(`^/l/${lg}/trades\\?`));
      const q = new URL(href!, "http://x").searchParams;
      expect(q.get("team"), href!).toBe("4");
      expect(q.get("ta"), href!).toBe("4");
    }
    for (const href of await table.locator("tbody a").evaluateAll((as) => as.map((a) => a.getAttribute("href")))) {
      expect(href, "every in-app link is league-scoped").toMatch(new RegExp(`^/l/${lg}/`));
    }
    // The owner filter is a plain GET form posting back to this league's page.
    await expect(page.getByRole("form", { name: "Owner" })).toHaveAttribute("action", `/l/${lg}/trades/values`);
    await trade.first().click();
    await expect(page).toHaveURL(new RegExp(`/l/${lg}/trades\\?.*ta=4`));
    expect(errors).toEqual([]);
  });
}
