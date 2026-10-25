import { expect, test, type Page } from "@playwright/test";
import { collectErrors, expectAccessible, expectNoBrokenValues, expectNoSidewaysScroll, expectStructure, open, stubPhotos } from "./ux";

// Ask Fantasy Intel (issue #219) against the E2E fixture league (Team 2 is the owner's team; week 3). The page sends the
// question to GET /api/ask (ask/v1) and shows the card: a verdict whose numbers are the linked page's own, a link to
// that page with the same view, a clarification with re-ask links, or a declined question with questions that work.
// The form is a plain GET, so every question is a shareable URL. Runs on desktop-1440 and mobile-375.

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
});

const main = (page: Page) => page.locator("main");
const ask = (q: string) => `/ask?${new URLSearchParams({ q })}`;

async function checks(page: Page) {
  await expectStructure(page);
  await expectNoBrokenValues(page);
  await expectNoSidewaysScroll(page);
  await expectAccessible(page);
}

test("a lineup question gets Start/Sit's own answer and opens Start/Sit", async ({ page }) => {
  const errors = collectErrors(page);
  await open(page, ask("Who should I start this week?"));
  await expect(main(page).locator("h1")).toHaveText("Ask");
  await expect(main(page)).toContainText("Start/Sit finds a projected upgrade.");
  await expect(main(page)).toContainText("Start/Sit for Team 2, week 3");
  await expect(main(page)).toContainText("Projected gain");
  await expect(main(page)).toContainText("5 pts");
  await expect(main(page).getByRole("link", { name: "Open Start/Sit" })).toHaveAttribute("href", "/start-sit");
  // the picker shows the team the answer is for, the owner's default team, though the URL names none (Codex P2 on #354)
  const picker = main(page).getByRole("combobox", { name: "Analyzing" });
  await expect(picker.locator("option:checked")).toHaveText(/^Team 2/);
  // the question stays in the field, so it can be edited and asked again
  await expect(page.getByRole("searchbox", { name: "Question" })).toHaveValue("Who should I start this week?");
  await checks(page);
  expect(errors, "console errors").toEqual([]);
});

test("a player screen lists the screener's rows and opens the same screen", async ({ page }) => {
  const errors = collectErrors(page);
  await open(page, ask("WRs with a target share above 10% over the last 3 weeks"));
  await expect(main(page)).toContainText(/player\(s\) match|No player matches/);
  await expect(main(page)).toContainText("Tgt % > 10%");
  await expect(main(page).getByLabel("Answer freshness")).toContainText(/Data through week \d+/);
  const details = main(page).getByRole("region", { name: "Details" });
  await expect(details.getByRole("columnheader", { name: "Tgt %" })).toBeVisible();
  await expect(main(page).getByRole("link", { name: "Open Screener" }).first())
    .toHaveAttribute("href", /^\/screener\?pos=WR&from=\d+&to=\d+/);
  await checks(page);
  expect(errors, "console errors").toEqual([]);
});

// The form is a plain GET, so a question is a shareable URL the server answers. (With JavaScript off the page stays on
// its loading.tsx skeleton, as every streamed page here does, so this does not claim the answer renders without it.)
test("the form submits the question as a shareable URL the server answers", async ({ page, request }) => {
  await open(page, "/ask");
  const form = page.getByRole("search", { name: "Ask a question" });
  await expect(form).toHaveAttribute("method", "get");
  await form.getByRole("searchbox", { name: "Question" }).fill("Is Waiver RB available?");
  await form.getByRole("button", { name: "Ask" }).click();
  await page.waitForURL(/\/ask\?q=Is\+Waiver\+RB\+available/);
  await expect(main(page)).toContainText("Waiver RB is available: free agent.");
  // the answer is in the server's HTML for that URL, not fetched by the browser afterwards
  const res = await request.get(ask("Is Waiver RB available?"));
  expect(res.status()).toBe(200);
  expect(await res.text()).toContain("Waiver RB is available: free agent.");
});

test("an unsupported question is declined, and the questions offered instead are answered", async ({ page }) => {
  const errors = collectErrors(page);
  await open(page, ask("What is the meaning of life?"));
  await expect(main(page)).toContainText("Ask can't answer that exactly");
  await expect(main(page).getByLabel("Answer freshness")).toHaveCount(0);
  await expect(main(page).locator("[data-provenance]")).toHaveCount(0);
  const examples = main(page).getByRole("region", { name: "Questions Ask can answer" }).getByRole("link");
  expect(await examples.count()).toBeGreaterThanOrEqual(10);
  await checks(page);
  await examples.filter({ hasText: "Who is on bye this week?" }).click();
  await page.waitForURL(/q=Who\+is\+on\+bye\+this\+week/);
  await expect(main(page)).toContainText("Bye planner for Team 2");
  expect(errors, "console errors").toEqual([]);
});

test("the question form works by keyboard at 320px and keeps an explicit team", async ({ page }) => {
  const errors = collectErrors(page);
  await page.setViewportSize({ width: 320, height: 812 });
  await open(page, "/ask?team=1");
  const input = page.getByRole("searchbox", { name: "Question" });
  await input.focus();
  await input.fill("Who should I start this week?");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/ask\?q=Who\+should\+I\+start\+this\+week%3F&team=1$/);
  await expect(main(page)).toContainText("Start/Sit for Team 1, week 3");
  await expect(main(page).getByRole("combobox", { name: "Analyzing" }).locator("option:checked")).toHaveText(/^Team 1/);
  await expect(main(page).getByRole("link", { name: "Open Start/Sit" })).toHaveAttribute("href", "/start-sit?team=1");
  await checks(page);
  expect(errors).toEqual([]);
});

test("with no question the page lists what it can answer; a vague metric asks back", async ({ page }) => {
  const errors = collectErrors(page);
  await open(page, "/ask");
  await expect(main(page).getByRole("region", { name: "Questions Ask can answer" })).toBeVisible();
  await expect(main(page)).toContainText("Ask a question about this league");
  await checks(page);
  await open(page, ask("WRs with fantasy points per game above 15"));
  await expect(main(page)).toContainText("Did you mean PPR points per game?");
  expect(errors, "console errors").toEqual([]);
});
