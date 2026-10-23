# UX acceptance gates and release review

The automated gates (issue #202) catch navigation, layout, chart and missing-data regressions on every pull request. This page covers what they check, how to run and update them, and the short manual review to do before a release. The quality bar itself is in `fantasy-intel/frontend/PRODUCT.md`.

## What runs in CI

All of it runs in the parallel E2E jobs in `.github/workflows/ci.yml`. The UX gates run with the route smoke shards (`npm run e2e`) and the multi-league job (`npm run e2e:multileague`), and visual regression runs with the Sleeper smoke. The `E2E smoke (Playwright)` check is the aggregate that passes only when every E2E job passed.

| Step | Spec | Fixture | Widths | Checks |
|---|---|---|---|---|
| `npm run e2e` | `e2e/ux-gates.spec.ts` | Demo/public ESPN league (`scripts/build_e2e_db.py`) | 320, 390, 768, 1440 | Listed below, in both themes |
| `npm run e2e:visual` | `e2e/visual.spec.ts` | Same | 390, 768, 1440 | Screenshots compared with reviewed baselines |
| `npm run e2e:multileague` | `e2e/ux-multileague.spec.ts` | Private ESPN (signed in), Sleeper (superflex, FLEX, TE premium: complex roster slots), stale sync, blank sync (`scripts/e2e_multileague.py`) | 320, 390, 1440 for ESPN private; 320 and 1440 for Sleeper and the sync fixtures | Listed below |

The existing route smoke tests (`routes.spec.ts`, `sleeper.spec.ts`, `multileague.spec.ts`) still cover every page in the nav at 1440 and 375.

**Core destinations.** Overview, Start/Sit, Matchup, Waivers, Trade analyzer, Trade result, Rankings, Player profile, the League pages (Weekly recap, Playoff race, a manager) and Leagues (connect). On the multi-league site the gates also cover the connections list, the settings page of a never-synced league, and the pages of a stale league and a blank one.

**Checks for each page.** The shared helpers are in `e2e/ux.ts`.
- Structure: exactly one `h1`, and it is inside `main`. Exactly one banner and one main landmark. The section navigation is visible and has at least five group links. No backend error box or route error box.
- Decision first: the `h1` comes before any table, chart or method disclosure. On decision pages (Start/Sit, Waivers, Trade result) the Verdict comes before them too. On desktop, the Start/Sit and Waivers verdicts start in the first screenful.
- Layout: the page never scrolls sideways. Every app bar item fits inside the viewport and none overlaps another. Content pushed off the left edge does not create a scrollbar, so only this check catches it.
- Honest data: page text never shows a leaked `undefined`, `NaN`, `null`, `[object Object]` or a SCREAMING_SNAKE engine identifier.
- Accessibility: axe-core runs WCAG 2.2 A/AA plus best-practice rules in dark and light: landmarks, accessible names, ARIA, contrast, target size and focusable scroll regions.
- Zero console errors.
- Keyboard (in `ux-gates.spec.ts`):
  - Tabbing from the top starts in the app bar and reaches the page content.
  - Every focus stop is visible and shows a focus ring.
  - A table that scrolls sideways is a tab stop and scrolls with the arrow keys. A table that fits is not a tab stop.
  - On the multi-league site the league switcher works from the keyboard and re-renders the header.

The added CI time is about 5 to 6 minutes, all inside the existing E2E job: the UX gates about 3.5 minutes, the multi-league UX gates about 1.5, and the visual step under 1.

## Running locally

Build first (`npm run build` in `fantasy-intel/frontend`). Each config starts its own backend and `next start`:

```bash
npm run e2e -- e2e/ux-gates.spec.ts                       # all four UX projects
npm run e2e -- e2e/ux-gates.spec.ts --project=mobile-320  # one width
FI_TEST_PG_DSN=postgresql://... npm run e2e:multileague -- e2e/ux-multileague.spec.ts
npm run e2e:visual                                         # compares, or skips (see below)
```

`agent_check.py full` already runs `npm run e2e` and `npm run e2e:multileague`, so the UX gates are part of it.

## When an accessibility or content check fails

Fix the page. If the fix belongs to another change, add one entry to `e2e/ux-triage.ts`. That file is the only allowlist. Each entry gives a rule (or a text pattern), the route, optionally the element, why it is tolerated and who owns the fix. Keep entries narrow: the same rule on any other route or element still fails. Delete the entry in the change that fixes it.

## Visual baselines: reviewed, never blindly regenerated

Screenshots depend on the browser build. CI installs the Chromium build that matches `@playwright/test` (`npx playwright install`), and a laptop or agent container usually has a different one. So baselines are drawn **only in CI** and committed **only after a person has looked at them**.

1. **Draw.** From the Actions tab, run **Visual baselines** (`.github/workflows/visual-baselines.yml`) on the branch whose UI should be drawn. It builds the app, runs `npm run e2e:visual -- --update-snapshots=all` and uploads the `visual-baselines` artifact. The artifact holds one PNG per route, viewport and theme, plus `BROWSER.txt` with the browser version.
2. **Review.** Download the artifact and open every image that changed against `fantasy-intel/frontend/e2e/visual-baselines/`. Check that each change is the one the pull request intends, and look for clipped text, overlaps, a missing nav item, a broken chart, an unknown value or a theme leak.
3. **Commit.** Replace `e2e/visual-baselines/` with the artifact's contents and commit them in the pull request that explains the change. The reviewer of that pull request reviews the images as well.

How the gate behaves:
- **No baselines committed:** every visual test is skipped with a message, and the gate is not armed. This is the state until the first reviewed set is committed.
- **Baselines committed but drawn by another browser:** the run fails in CI and is skipped locally. Bumping `@playwright/test` therefore means redrawing and reviewing the baselines.
- **A diff larger than 1% of the pixels:** the run fails, and the expected, actual and diff images are in the `playwright-traces` artifact.

The screenshots are deterministic, so they cannot make CI flaky:
- Build-time stamps ("Oct 22, 7:20 PM") and ages ("9d ago") are rewritten to fixed values of the same shape.
- The sync pill and the menu button (whose dot reflects freshness) are masked.
- Animations and the caret are off, and the time zone and locale are fixed.
- CI allows one retry.

Never run `--update-snapshots` to make a red visual check green without the review above.

## Manual release review

Do this before a release, about 15 minutes, on a phone-sized window (390px) and a desktop in both themes. Use the live site or the E2E fixtures. Record anything wrong as an issue.

**First-time navigation**
- [ ] A visitor with no league lands on Leagues and can tell how to add one. No error box, and no setup jargon such as environment variables.
- [ ] Each of the five sections opens from the app bar (desktop) and the tab bar (phone), and its sub-nav marks the current page.
- [ ] Every page opens with its answer: the headline or Verdict comes before the tables and charts, and the method stays collapsed at the end.
- [ ] Nothing is cut off or scrolls sideways at 320px, and long team and player names ellipsise with a title.

**Returning user**
- [ ] The chosen theme, the analyzed team and the last page of each tab survive a reload and a new visit.
- [ ] The data-freshness pill or menu dot is accurate. A stale league says "Sync paused" in words and still shows its last data.
- [ ] Deep links with filters (Rankings with a position and horizon, Waivers for a team) reopen exactly.

**Trade and waiver analysis**
- [ ] On Waivers, the top claim or "Nothing to claim" verdict matches the table under it. The shortlist saves, reloads and stays per league.
- [ ] A trade on the analyzer can be built with taps on a phone and with the keyboard on desktop. The result leads with a verdict and shows no grades or acceptance odds.
- [ ] Missing numbers show as a muted dash with a reason, never as 0, `null` or `NaN`.

**League switching**
- [ ] The league switcher lists recent leagues and changes the header, data and saved team, in both directions, with mouse and keyboard.
- [ ] A signed-out visitor never sees a private league. A never-synced league shows first-sync guidance, not an error (known defect below).
- [ ] A Sleeper league shows no ESPN wording or ESPN-only controls.

## Known defects found by the gates

These are open; the triage file or a `test.fixme` names each one, and each triage entry's owner is its issue:
- #322: the trade builder's player checkboxes are 18px inputs (axe `target-size`).
- #323: Matchup scoreboard team names are `h3` with no `h2` above them (axe `heading-order`).
- #324: the links in the player profile's note lines are told apart by colour only (axe `link-in-text-block`).
- #325: a Sleeper team with no abbreviation prints "(null)" in the Start/Sit header.
- #326: with no saved team, a league's Overview and Matchup show the operator hint `ESPN_MY_TEAM_ID`. The Matchup line
  is the backend's `TeamNotFound` detail (`backend/app/analytics/matchup.py`).
- A connected league that has never synced shows "The backend returned an error (409)" and a "Backend offline" pill. The `test.fixme` in `ux-multileague.spec.ts` covers it.
