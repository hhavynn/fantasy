import { expect, test } from "@playwright/test";
import { expectStrengthInWords, expectTryItLinksStayIn } from "./start-sit-strength";

// Issue #172 in the browser, on the E2E league (scripts/build_e2e_db.py) at desktop-1440 and mobile-375: the
// recommended move carries a strength badge and its reason, in words and never a percentage or consensus, and every
// "Other options" link is a what-if on this Start/Sit page. Which grade the fixture earns is read from the page.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

test("Start/Sit says how close its call is in words, and its other options open the what-if here", async ({ page }) => {
  await page.goto("/start-sit");
  const shown = await expectStrengthInWords(page, { badge: true });
  expect(shown.some((t) => /3-point margin/.test(t)), "a graded call states its rule").toBe(true);
  await expectTryItLinksStayIn(page, "/start-sit");
});
