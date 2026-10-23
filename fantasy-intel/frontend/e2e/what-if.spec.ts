import { expect, test, type Page } from "@playwright/test";

// The Start/Sit what-if builder (issue #171) against the synthetic league (scripts/build_e2e_db.py): team 2 is the
// owner default and Kilo RB (207) is on fantasy IR; nothing in the fixture week has kicked off. Which player the
// engine recommends where is read from the page, not assumed. Runs on desktop-1440 and mobile-375 with touch enabled.
const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");

test.use({ hasTouch: true });

const isPhone = () => /^mobile/.test(test.info().project.name);

test.beforeEach(async ({ page }) => {
  await page.route("https://a.espncdn.com/**", (r) => r.fulfill({ contentType: "image/png", body: PIXEL_PNG }));
});

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading still loading").toHaveText(/\S/, { timeout: 20_000 });
}

async function press(page: Page, locator: ReturnType<Page["locator"]>) {
  if (isPhone()) await locator.tap();
  else await locator.click();
}

const whatIf = (page: Page) => page.getByRole("region", { name: "What-if lineup" });

test("try a swap, see the points and the slot change, then reset to the recommendation", async ({ page }) => {
  await page.goto("/start-sit");
  await loaded(page);
  const section = whatIf(page);
  await expect(section.getByRole("table")).toBeVisible({ timeout: 20_000 });
  await expect(section.getByText("Changed", { exact: true })).toHaveCount(0);
  const flexRow = section.getByRole("row").filter({ has: page.getByLabel("Move into FLEX") });
  const before = (await flexRow.getByRole("link").first().textContent())?.trim() ?? "";
  expect(before).not.toBe("");

  // a bench player (no "swaps with"), as the backend offers him: "Name (POS, TEAM) · proj · −1.0 vs best"
  const flex = section.getByLabel("Move into FLEX");
  const option = flex.locator("option:not([disabled])").filter({ hasNotText: "swaps with" }).first();
  const label = (await option.textContent()) ?? "";
  const name = label.split(" (")[0];
  const delta = /· ([+−]?\d+\.\d) vs best/.exec(label)?.[1];
  expect(name && delta, label).toBeTruthy();
  await flex.selectOption((await option.getAttribute("value")) as string);
  // keyboard, as the disclosure below: the control sits in the last column, which the pinned first column can
  // overlap when a phone scrolls the table sideways
  await flexRow.getByRole("button", { name: "Try" }).focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/whatif=w\d+t2/);
  await expect(section.getByText("Changed", { exact: true })).toHaveCount(1);
  await expect(section.getByText(`was ${before}`)).toBeVisible();
  await expect(section).toContainText(`${name} into FLEX; ${before} to the bench`);
  await expect(section).toContainText(delta === "0.0" ? "0.0 pts" : `${delta} pts`);
  // the engine's own recommendation is untouched by the what-if
  await expect(page.getByRole("region", { name: "Lineup: submitted vs recommended" })).toContainText(before);

  await press(page, section.getByRole("link", { name: "Reset to recommended" }));
  await expect(page).not.toHaveURL(/whatif=/);
  await expect(section.getByText("Changed", { exact: true })).toHaveCount(0);
  await expect(flexRow.getByRole("link").first()).toHaveText(before);
});

test("ineligible and IR players are never offered, and say why", async ({ page }) => {
  await page.goto("/start-sit");
  await loaded(page);
  const section = whatIf(page);
  await expect(section.getByRole("table")).toBeVisible({ timeout: 20_000 });
  await expect(section.getByLabel("Move into FLEX").locator("option", { hasText: "Kilo" })).toHaveCount(0);
  const kRow = section.getByRole("row").filter({ hasText: "Takes K" });
  await expect(kRow).toContainText("No other eligible player");
  const why = section.locator("details").filter({ has: page.locator("summary", { hasText: "Not available here" }) }).first();
  // keyboard: the disclosure sits in the table's last column, which the pinned first column can overlap on phones
  await why.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(why).toContainText("fantasy IR slot and cannot start");
});

test("a what-if link for another roster or a garbled link is not applied and says so", async ({ page }) => {
  await page.goto("/start-sit?whatif=w3t2~FLEX_0.999999");
  await loaded(page);
  const section = whatIf(page);
  await expect(section).toContainText("This what-if was not applied", { timeout: 20_000 });
  await expect(section).toContainText("is not on this team's roster");
  await expect(section.getByText("Changed", { exact: true })).toHaveCount(0);

  await page.goto("/start-sit?whatif=%3Cscript%3E");
  await loaded(page);
  await expect(whatIf(page)).toContainText("This what-if link could not be read", { timeout: 20_000 });
  const o = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(o.scroll, `horizontal scroll at ${o.client}px`).toBeLessThanOrEqual(o.client);
});
