import { expect, test, type Page } from "@playwright/test";

// The Playoff race what-if (issue #192) against the synthetic league (scripts/build_e2e_db.py): four teams, team 2 is
// the owner default, nothing in week 3 has kicked off, so every remaining regular-season game is open. Runs on
// desktop-1440 and mobile-375. It waits for real content (the standings section), never for the loading skeleton.

const isPhone = () => /^mobile/.test(test.info().project.name);

async function loaded(page: Page) {
  await expect(page.locator("main h1:visible"), "page heading").toHaveText("Playoff race lab", { timeout: 20_000 });
  await expect(page.getByRole("region", { name: /^Actual vs hypothetical standings/ })).toBeVisible();
}

const whatIf = (page: Page) => page.getByRole("region", { name: /^What-if: your results vs the baseline/ });

async function noSideScroll(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(over, "horizontal page scroll").toBeLessThanOrEqual(0);
}

test("set a future result, see deterministic standings against the baseline, copy the link and reset", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/playoff-race");
  await loaded(page);

  // Scenarios off: no what-if block, and actual vs hypothetical standings are labelled.
  await expect(whatIf(page)).toHaveCount(0);
  const standings = page.getByRole("region", { name: /^Actual vs hypothetical standings/ });
  await expect(standings.getByRole("columnheader", { name: "Actual now" })).toBeVisible();
  await expect(standings.getByRole("columnheader", { name: "Hypothetical", exact: true })).toBeVisible();
  // The method text says no team is described as clinched or eliminated; no visible claim says one is.
  await expect(page.getByText(/clinched|eliminated/i).filter({ visible: true })).toHaveCount(0);

  // Set the first remaining game (week 3, Team 1 at home to Team 2, the projected winner) to a home win.
  const matchups = page.getByRole("region", { name: "Remaining regular-season matchups" });
  await matchups.getByRole("link", { name: "Home wins" }).first().click();
  await expect(page).toHaveURL(/\/playoff-race\?o=\d+%3Ahome$/);
  const panel = whatIf(page);
  await expect(panel).toBeVisible();

  // Its inputs, method and sample count are stated; nothing is a probability, a clinch or an elimination.
  await expect(panel.getByText("Method: deterministic, 1 exact scenario, 0 random samples, no seed, no probability.")).toBeVisible();
  const setResult = panel.getByRole("listitem").filter({ hasText: "Team 1 wins" });
  await expect(setResult).toContainText("baseline: Team 2 wins (projection)");
  await expect(panel.getByRole("heading", { name: "Clinch and elimination: not claimed" })).toBeVisible();
  await expect(panel.getByText(/tiebreak and seeding settings are not mapped/)).toBeVisible();
  await expect(panel.getByText(/clinched|eliminated/i).filter({ visible: true })).toHaveCount(0);

  // The deterministic standings change: Team 1 gains the win, Team 2 takes the loss, both labelled hypothetical.
  const table = panel.getByRole("table").first();
  await expect(table.getByRole("columnheader", { name: "Baseline (hypothetical)" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Your what-if (hypothetical)" })).toBeVisible();
  for (const team of ["Team 1", "Team 2"]) {
    const cells = table.getByRole("row").filter({ hasText: team }).getByRole("cell");
    await expect(cells.nth(3)).not.toHaveText("No change");
    expect(await cells.nth(1).textContent()).not.toBe(await cells.nth(2).textContent());
  }
  await expect(table.getByRole("row").filter({ hasText: "Team 4" }).getByRole("cell").nth(3)).toHaveText("No change");

  // Link back to the matchup analysis from the spotlight team's side (team 2 plays in the game).
  await expect(setResult.getByRole("link", { name: "Matchup analysis" })).toHaveAttribute("href", "/matchup?week=3&team=2");

  // Copy the scenario link: the clipboard holds this exact what-if.
  const copy = panel.getByRole("button", { name: "Copy scenario link" });
  if (isPhone()) {
    const box = await copy.boundingBox();
    expect(box!.height, "touch target shorter than 44px").toBeGreaterThanOrEqual(44);
  }
  await copy.click();
  await expect(panel.getByRole("status")).toHaveText("Link copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(page.url());
  await noSideScroll(page);

  // Reset returns to the scenario-off page.
  await panel.getByRole("link", { name: "Reset to the baseline" }).click();
  await expect(page).toHaveURL(/\/playoff-race$/);
  await expect(page.getByRole("region", { name: /^Actual vs hypothetical standings/ })).toBeVisible();
  await expect(whatIf(page)).toHaveCount(0);
});

test("the rooting guide compares other games one at a time, labelled hypothetical, with no claim", async ({ page }) => {
  // Fixture: in week 3 team 2 (the default) plays team 1, and team 4 plays team 3. Team 2 sits first by a distance
  // and the fixture's provider records differ from its stored finals, so the possible range is withheld.
  await page.goto("/playoff-race");
  await loaded(page);
  const guide = page.getByRole("region", { name: /^Rooting guide: wk 3/ });
  await expect(guide).toBeVisible();
  await expect(guide.getByText("Hypothetical", { exact: true })).toBeVisible();
  await expect(guide.getByText(/one at a time against this scenario\. Deterministic, no probability\./)).toBeVisible();
  await expect(guide.getByText(/No other game in wk 3 has a better side for Team 2 on its own\./)).toBeVisible();
  await expect(guide.getByText("Neither result is better for Team 2: Team 4 vs Team 3.")).toBeVisible();
  await expect(guide.getByText(/Possible range not used: The provider's records do not match/)).toBeVisible();
  await expect(guide.getByText(/later open games also remain/)).toBeVisible();
  await expect(guide.getByText(/clinched|eliminated|%/i).filter({ visible: true })).toHaveCount(0);
  await noSideScroll(page);
});
