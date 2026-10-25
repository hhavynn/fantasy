import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { expectStrengthInWords, expectTryItLinksStayIn } from "./start-sit-strength";

// Issue #172 under /l/<lg>/ (scripts/e2e_multileague.py), at a phone and a desktop width: the public ESPN league's
// strength badge and reason are in words, and its "Other options" links stay inside that league's Start/Sit; the
// partial-data league (ESPN's projection missing for every other player) is graded unverified, never clear or close.
type Manifest = { leagues: Record<"espn_public" | "espn_partial", string> };
const manifest = (): Manifest =>
  JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../data/e2e-multileague/manifest.json"), "utf8"));
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

for (const { width, height } of [{ width: 375, height: 812 }, { width: 1440, height: 900 }]) {
  test(`at ${width}px a league's strength is in words and its other options stay in that league`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    const lg = manifest().leagues.espn_public;
    // an anonymous visitor reads a public league from a team's perspective (?team=): with none, Start/Sit asks for one
    await page.goto(`/l/${lg}/start-sit?team=1`);
    await expectStrengthInWords(page, { badge: true });
    await expectTryItLinksStayIn(page, `/l/${lg}/start-sit`);
  });

  test(`at ${width}px a league with partial projections is unverified, never clear or close`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto(`/l/${manifest().leagues.espn_partial}/start-sit?team=1`);
    const shown = await expectStrengthInWords(page, { badge: false });
    expect(shown.filter((t) => /^(Clear call|Close call)$|^(Clear|Close call):/.test(t))).toEqual([]);
    expect(shown.some((t) => /^Unverified/.test(t)), "the partial league is not graded unverified").toBe(true);
  });
}
