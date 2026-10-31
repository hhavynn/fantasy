import assert from "node:assert/strict";
import { expect, test } from "@playwright/test";
import { expectAccessible, expectStructure, tabThrough } from "./ux";

/** Deliberate synthetic failures test the same release helpers that guard real league routes. */
test.use({ viewport: { width: 320, height: 640 } });

test("a missing main landmark breaks the release structure check", async ({ page }) => {
  await page.setContent(`<!doctype html><html lang="en"><head><title>Fixture</title></head><body>
    <header><nav aria-label="Sections">
      <a href="/">My team</a><a href="/waivers">Waivers</a>
      <a href="/rankings">Players</a><a href="/trades">Trades</a>
      <a href="/weekly-recap">League</a>
    </nav></header>
    <section id="content"><h1>Fixture heading</h1></section>
    </body></html>`);
  await assert.rejects(() => expectStructure(page), /main|h1/i);
  await page.locator("#content").evaluate((node) => {
    const main = document.createElement("main");
    main.innerHTML = node.innerHTML;
    node.replaceWith(main);
  });
  await expectStructure(page);
});

test("the accessibility release check rejects a button without an accessible name", async ({ page }) => {
  await page.setContent(`<!doctype html><html lang="en"><head><title>Fixture</title></head>
    <body><main><h1>Fixture heading</h1><button data-test="unnamed"></button></main></body></html>`);
  await assert.rejects(() => expectAccessible(page), /axe violations/);
  await page.locator('[data-test="unnamed"]').evaluate((el) => el.setAttribute("aria-label", "Evaluate lineup"));
  await expectAccessible(page);
});

test("the keyboard focus gate detects an unreachably unfocusable action", async ({ page }) => {
  await page.setContent(`<!doctype html><html lang="en"><head><title>Fixture</title></head>
    <body><main><h1>Fixture heading</h1><button tabindex="-1">Apply filter</button></main></body></html>`);
  const skipped = await tabThrough(page, 6, 1);
  assert.equal(skipped.some((s) => s.where === "main"), false,
    "keyboard Tab must not silently count an unreachable action as reachable");
  await page.getByRole("button", { name: "Apply filter" }).evaluate((el) => el.removeAttribute("tabindex"));
  const reachable = await tabThrough(page, 6, 1);
  expect(reachable.some((s) => s.where === "main" && s.visible && s.indicator)).toBe(true);
});
