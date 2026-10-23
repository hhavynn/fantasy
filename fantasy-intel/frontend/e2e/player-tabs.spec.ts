import { expect, test, type Page } from "@playwright/test";
import { collectErrors, expectAccessible, expectNoBrokenValues, expectNoSidewaysScroll, open, stubPhotos } from "./ux";

// Player profile tabs (#187) on the synthetic ESPN fixture (scripts/build_e2e_db.py): 201 "Echo RB" has NFL usage,
// -16012 "Chiefs D/ST" has no NFL usage and no season points. The tabs are an ARIA tablist of links: deep-linkable,
// server-rendered, one tab stop with arrow-key focus, and only the selected panel in the page.
const TABS = [
  { id: "overview", name: "Overview", heading: "Recent form" },
  { id: "usage", name: "Usage", heading: "Usage by week" },
  { id: "game-log", name: "Game log", heading: "Game log" },
  { id: "matchups", name: "Matchups", heading: "Remaining schedule" },
  { id: "outlook", name: "Outlook", heading: "Forward view" },
] as const;

const tablist = (page: Page) => page.getByRole("tablist", { name: /profile$/ });
const tab = (page: Page, name: string) => tablist(page).getByRole("tab", { name, exact: true });

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
});

test("an old /players?id= link lands on the Overview, with compare and every tab", async ({ page }) => {
  const errors = collectErrors(page);
  await open(page, "/players?id=201");
  await expect(page.locator("main h1")).toContainText("Echo RB");
  await expect(tablist(page).getByRole("tab")).toHaveCount(TABS.length);
  await expect(tablist(page).getByRole("tab", { name: "News" })).toHaveCount(0);
  await expect(tab(page, "Overview")).toHaveAttribute("aria-selected", "true");
  const panel = page.getByRole("tabpanel", { name: "Overview" });
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("heading", { name: "Recent form" })).toBeVisible();
  await expect(page.getByRole("tabpanel")).toHaveCount(1);
  await expect(page.locator("main").getByRole("link", { name: /Compare Echo RB/ })).toBeVisible();
  expect(errors, "console errors").toEqual([]);
});

for (const t of TABS) {
  test(`?tab=${t.id} deep-links to ${t.name}, which opens with a summary and is accessible`, async ({ page }) => {
    test.skip(!test.info().project.name.includes("desktop"), "axe and structure once, at desktop width");
    const errors = collectErrors(page);
    await open(page, `/players?id=201&tab=${t.id}`);
    await expect(tab(page, t.name)).toHaveAttribute("aria-selected", "true");
    await expect(tab(page, t.name)).toHaveAttribute("tabindex", "0");
    await expect(tablist(page).locator('[role="tab"][tabindex="0"]')).toHaveCount(1);
    const panel = page.getByRole("tabpanel", { name: t.name });
    await expect(panel.getByRole("heading", { level: 2 }).first()).toBeVisible();
    await expect(panel.locator("dl").first()).toContainText("Scoring");
    await expect(panel.locator("dl").first()).toContainText("Window");
    await expect(panel.getByRole("heading", { name: t.heading, exact: true })).toBeVisible();
    // The header (name, compare) is the same on every tab.
    await expect(page.locator("main h1")).toContainText("Echo RB");
    await expect(page.locator("main").getByRole("link", { name: /Compare Echo RB/ })).toBeVisible();
    await expectNoBrokenValues(page);
    await expectAccessible(page, t.id);
    expect(errors, "console errors").toEqual([]);
  });
}

test("an unknown tab is the Overview", async ({ page }) => {
  await open(page, "/players?id=201&tab=news");
  await expect(tab(page, "Overview")).toHaveAttribute("aria-selected", "true");
});

test("arrow keys move focus along the tabs; Enter opens the focused one", async ({ page }) => {
  test.skip(!test.info().project.name.includes("desktop"), "keyboard journey once, at desktop width");
  await open(page, "/players?id=201");
  await tab(page, "Overview").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Usage")).toBeFocused();
  await page.keyboard.press("End");
  await expect(tab(page, "Outlook")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Overview")).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(tab(page, "Outlook")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tab(page, "Overview")).toBeFocused();
  // Moving focus does not change the selection (each tab is a navigation).
  await expect(tab(page, "Overview")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Game log")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/[?&]id=201&tab=game-log$/);
  await expect(tab(page, "Game log")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Matchups")).toBeFocused();
  await page.keyboard.press("Space");
  await expect(page).toHaveURL(/tab=matchups/);
  await expect(page.getByRole("tabpanel", { name: "Matchups" })).toBeVisible();
});

test("the week window filters usage and survives a tab switch", async ({ page }) => {
  await open(page, "/players?id=201&tab=usage");
  const weeks = page.getByRole("group", { name: "Weeks shown" });
  await expect(weeks.getByRole("link", { name: "Season" })).toHaveAttribute("aria-current", "true");
  await weeks.getByRole("link", { name: "Last 3 weeks" }).click();
  await expect(page).toHaveURL(/[?&]id=201&tab=usage&weeks=3$/);
  await expect(weeks.getByRole("link", { name: "Last 3 weeks" })).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("tabpanel", { name: "Usage" }).locator("dl").first()).toContainText("Last 3 weeks: wk");
  await tab(page, "Game log").click();
  await expect(page).toHaveURL(/[?&]id=201&tab=game-log&weeks=3$/);
  const rows = page.getByRole("tabpanel", { name: "Game log" }).locator("table tbody tr");
  expect(await rows.count()).toBeLessThanOrEqual(3);
  await expectNoSidewaysScroll(page);
});

test("a player without usage or points says so on every tab instead of showing zeros", async ({ page }) => {
  for (const t of TABS) {
    await open(page, `/players?id=-16012&tab=${t.id}`);
    await test.step(t.id, async () => {
      const panel = page.getByRole("tabpanel", { name: t.name });
      await expect(panel).toBeVisible();
      await expectNoBrokenValues(page);
      await expectNoSidewaysScroll(page);
      // A dataset that is absent is named as absent; no panel invents a measured zero for it.
      if (t.id !== "outlook" && t.id !== "matchups") {
        await expect(panel).toContainText(/No .*(stored|recorded|evaluation|usage)/);
      }
      if (t.id === "matchups") await expect(panel).toContainText("No role or matchup context");
    });
  }
});

test("each tab stays a short page on a 320px phone", async ({ page }) => {
  test.skip(test.info().project.name.includes("desktop"), "phone layout");
  await page.setViewportSize({ width: 320, height: 640 });
  const heights: Record<string, number> = {};
  for (const t of TABS) {
    await open(page, `/players?id=201&tab=${t.id}`);
    await expectNoSidewaysScroll(page);
    heights[t.id] = await page.evaluate(() => document.documentElement.scrollHeight);
  }
  test.info().annotations.push({ type: "heights-320", description: JSON.stringify(heights) });
  for (const [id, h] of Object.entries(heights)) expect(h, `${id} page height at 320px`).toBeLessThan(5000);
});
