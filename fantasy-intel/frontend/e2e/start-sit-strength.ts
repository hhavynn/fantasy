import { expect, type Page } from "@playwright/test";
import { WHATIF_PARAM } from "../lib/what-if";

// Shared checks for issue #172's stopping criterion: Start/Sit states how close its call is in words (a label and its
// rule), never a percentage or an invented consensus, and each "Other options" link is a what-if on the same page of
// the same league. Used by start-sit-strength.spec.ts (the E2E league) and start-sit-strength-multileague.spec.ts.

const STRENGTH = /^(Clear call|Close call|Unverified)$/;
// a graded reason states the 3-point rule; an unverified one names what is unknown
const REASON = /3-point margin the decision backtests treat as a close call|^Unverified: /;
const FABRICATED = /%|\bexperts?\b|\bconsensus\b/i;

/** Every strength badge and reason on the page is in words: at least one reason, and each recommended move's badge. */
export async function expectStrengthInWords(page: Page, { badge }: { badge: boolean }) {
  const main = page.locator("main");
  await expect(main.locator("h1:visible"), "page heading still loading").toHaveText(/\S/, { timeout: 20_000 });
  const reasons = main.getByText(REASON);
  await expect(reasons.first(), "no recommendation strength reason").toBeVisible({ timeout: 20_000 });
  const badges = main.locator(".badge").filter({ hasText: STRENGTH });
  if (badge) await expect(badges.first(), "no strength badge on the recommended move").toBeVisible();
  const shown = [...await badges.allInnerTexts(), ...await reasons.allInnerTexts()].map((t) => t.trim());
  expect(shown.length).toBeGreaterThan(0);
  for (const text of shown) expect(text, "a strength is stated as a number or a consensus").not.toMatch(FABRICATED);
  return shown;
}

/** Each "Try it" link opens the what-if on `path` (this league's Start/Sit), and following one applies it there. */
export async function expectTryItLinksStayIn(page: Page, path: string) {
  const options = page.getByRole("region", { name: "Other options" });
  const links = options.getByRole("link", { name: /^Try .+ at .+$/ });
  await expect(links.first(), "no Other options links").toBeVisible({ timeout: 20_000 });
  const here = new URL(page.url());
  const hrefs = await links.evaluateAll((els) => els.map((el) => el.getAttribute("href") ?? ""));
  expect(hrefs.length).toBeGreaterThan(0);
  for (const href of hrefs) {
    const url = new URL(href, here);
    expect(url.origin, href).toBe(here.origin);
    expect(url.pathname, href).toBe(path);
    expect(url.searchParams.get(WHATIF_PARAM), href).toBeTruthy();
  }
  const first = new URL(hrefs[0], here);
  await links.first().click();
  await expect.poll(() => new URL(page.url()).pathname, { timeout: 30_000 }).toBe(path);
  await expect.poll(() => new URL(page.url()).searchParams.get(WHATIF_PARAM), { timeout: 30_000 })
    .toBe(first.searchParams.get(WHATIF_PARAM));
  await expect(page.getByRole("region", { name: "What-if lineup" }).getByText("Changed", { exact: true }).first())
    .toBeVisible({ timeout: 30_000 });
}
