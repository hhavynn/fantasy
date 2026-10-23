import { expect, test, type Locator, type Page } from "@playwright/test";

// The Waivers add/drop simulator (issue #178) against the synthetic league (scripts/build_e2e_db.py): team 2 is the
// owner default with one open active spot; Waiver RB (209) is a free agent, Waiver WR (309) is on waivers, Echo RB
// (201) is on team 1, Juliet RB (206) is on team 2's bench. Runs on desktop-1440 and mobile-375 with touch enabled.
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

async function press(page: Page, locator: Locator) {
  if (isPhone()) await locator.tap();
  else await locator.click();
}

const panel = (page: Page) => page.getByRole("region", { name: "Add/drop simulator" });

test("simulate an add and a drop, read the impact, then clear it", async ({ page }) => {
  await page.goto("/waivers");
  await loaded(page);
  const sim = panel(page);
  await expect(sim.getByText("Choose a player to add, one to drop, or both")).toBeVisible({ timeout: 20_000 });
  await expect(sim).toContainText("nothing is sent to ESPN");

  const add = sim.getByLabel("Add", { exact: true });
  const drop = sim.getByLabel("Drop", { exact: true });
  // only available players are offered: never a rostered one
  await expect(add.locator("option", { hasText: "Echo RB" })).toHaveCount(0);
  await expect(add.locator("option", { hasText: "Waiver WR" })).toContainText("waivers");
  await add.selectOption("209");
  await drop.selectOption("206");
  const submit = sim.getByRole("button", { name: "Simulate" });
  await submit.scrollIntoViewIfNeeded();
  if (isPhone()) expect((await submit.boundingBox())!.height, "touch target shorter than 44px").toBeGreaterThanOrEqual(44);
  await press(page, submit);

  await expect(page).toHaveURL(/add=209&drop=206/);
  await expect(sim.getByText("Add Waiver RB, drop Juliet RB: a projected lineup gain")).toBeVisible();
  await expect(sim).toContainText(/Week 3/);
  await expect(sim).toContainText("+3.0 pts");
  await expect(sim).toContainText("Waiver RB starts; Juliet RB leaves the lineup");
  await sim.getByText("Week by week, depth and byes").click();
  await expect(sim.getByRole("table")).toBeVisible();

  // the page has no horizontal scroll with the panel open
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "horizontal page scroll").toBeLessThanOrEqual(1);

  await press(page, sim.getByRole("link", { name: "Clear move" }));
  await expect(page).not.toHaveURL(/add=|drop=/);
  await expect(sim.getByText("Choose a player to add, one to drop, or both")).toBeVisible();
  // the pickers follow the URL on client navigation (Clear, Back), never the previous move
  await expect(add).toHaveValue("");
  await expect(drop).toHaveValue("");
  await page.goBack();
  await expect(page).toHaveURL(/add=209&drop=206/);
  await expect(sim.getByText("Add Waiver RB, drop Juliet RB: a projected lineup gain")).toBeVisible();
  await expect(add).toHaveValue("209");
  await expect(drop).toHaveValue("206");
});

test("a rostered player is explained, never simulated as a claim", async ({ page }) => {
  await page.goto("/waivers?add=201#add-drop");
  await loaded(page);
  const sim = panel(page);
  await expect(sim.getByText("Add Echo RB: not possible as asked")).toBeVisible({ timeout: 20_000 });
  await expect(sim).toContainText("Echo RB is rostered by Team 1");
  await expect(sim.getByRole("table")).toHaveCount(0);
});

test("a waiver claim and an edited link are labelled", async ({ page }) => {
  await page.goto("/waivers?add=309");
  await loaded(page);
  await expect(panel(page)).toContainText("Waiver WR is on waivers: he can only be acquired by a claim", { timeout: 20_000 });
  await page.goto("/waivers?add=99999&drop=206");
  await loaded(page);
  await expect(panel(page).getByText("This link's move was not simulated")).toBeVisible({ timeout: 20_000 });
});
