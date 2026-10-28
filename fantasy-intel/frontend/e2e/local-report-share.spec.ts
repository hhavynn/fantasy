import { expect, test } from "@playwright/test";
import { open, stubPhotos } from "./ux";

test.beforeEach(async ({ page }) => {
  await stubPhotos(page);
  await page.addInitScript(() => {
    type ShareRecord = { title?: string; text?: string; url?: string; files?: File[] };
    const win = window as Window & { __testSharedReport?: ShareRecord };
    Object.defineProperty(navigator, "share", {
      configurable: true,
      value: async (data: ShareRecord) => { win.__testSharedReport = data; },
    });
  });
});

test("native share only receives the allowlisted summary text, never a URL or raw player data", async ({ page }) => {
  for (const path of ["/weekly-recap", "/matchup", "/my-team/value", "/trades?ta=2&tb=1&a=206&a=307&b=202"]) {
    await test.step(path, async () => {
      await open(page, path);
      const button = page.getByRole("button", { name: "Share name-free summary", exact: true });
      await expect(button).toBeVisible({ timeout: 30_000 });
      await button.click();
      const sent = await page.evaluate(() =>
        (window as Window & { __testSharedReport?: { title?: string; text?: string; url?: string; files?: File[] } })
          .__testSharedReport);
      expect(sent).toBeDefined();
      expect(sent?.title).toBe("Fantasy Intel summary");
      expect(sent?.text).toContain("Fantasy Intel");
      expect(sent?.url).toBeUndefined();
      expect(sent?.files).toBeUndefined();
      expect(sent?.text).not.toMatch(/https?:\/\/|espn_player_id|team_id|league_id|PRIVATE_|\[object Object\]/);
      // Reset between routes: a stale prior share must not make a missing button pass.
      await page.evaluate(() => {
        delete (window as Window & { __testSharedReport?: unknown }).__testSharedReport;
      });
    });
  }
});
