import assert from "node:assert/strict";
import { test } from "@playwright/test";
import { expectNoBrokenValues, expectNoSidewaysScroll } from "./ux";

/**
 * Gate self-tests (#202): deliberately introduce bad DOM to verify the *same*
 * assertions used for real app routes reject it, then prove recovery is accepted.
 * These synthetic pages contain no user or provider data, and do not update baselines.
 */
test.use({ viewport: { width: 320, height: 640 } });

test("320px overflow must fail the shared release gate, then pass when corrected", async ({ page }) => {
  await page.setContent(`<!doctype html><html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    </head><body style="margin:0"><main>
    <div data-test="overflow" style="width:500px;height:10px">Synthetic</div>
    </main></body></html>`);
  await assert.rejects(() => expectNoSidewaysScroll(page), /horizontal scroll at 320px/);
  await page.locator('[data-test="overflow"]').evaluate((element) => {
    (element as HTMLElement).style.width = "100%";
  });
  await expectNoSidewaysScroll(page);
});

test("unavailable data rendered as NaN or undefined must fail, then a dash must pass", async ({ page }) => {
  await page.setContent(`<!doctype html><html><main>
    <p>Projected: NaN</p><p>Available: undefined</p>
    </main></html>`);
  await assert.rejects(() => expectNoBrokenValues(page), /raw undefined\/NaN\/null/);
  await page.locator("main").evaluate((element) => {
    element.textContent = "Projected: — · Available: —";
  });
  await expectNoBrokenValues(page);
});
