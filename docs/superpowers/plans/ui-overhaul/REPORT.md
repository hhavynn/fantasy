# Fantasy Intel: mobile-first UI/UX overhaul

Branch `ui/mobile-first-overhaul` from `origin/main` (a24a28f). Frontend only (`fantasy-intel/frontend`); no backend,
analytics, schema, auth or provider code changed. Before/after screenshots: `docs/screenshots/ui-overhaul/`.

## 1. Audit summary

Baseline (a24a28f): lint, typecheck, 537 unit tests, build and the Playwright suite all green. No route overflowed
sideways at 390 or 1440 px and no route logged a console error, so the problems were structural, not broken pages.
"Confirmed" means seen in a rendered page or measured; "UX concern" is a judgement.

| Sev | Finding | Where | Type |
|---|---|---|---|
| High | Phones carried four navigation systems at once: sticky top bar, sticky sub-nav, fixed tab bar and a menu, about 150 px of permanent chrome on an 844 px screen. | `components/Nav.tsx`, `app/globals.css` | Confirmed |
| High | Rankings on phones rendered one ~300 px card per player: 9,360 px for 28 players. | `app/rankings/RankingsCards.tsx` | Confirmed |
| High | Trade discovery on phones was ~17,500 px tall; a methodology stat strip and a three-row sort chip wall came before the first trade, and each trade card was ~850 px. | `app/trades/discover/page.tsx` | Confirmed |
| High | Trade analyzer at 1440×900: the bar with Evaluate sat below the fold until the user scrolled past both rosters (it was top-sticky only once reached). Found by the new e2e test. | `app/trades/builder.module.css` | Confirmed |
| High | First run (no imported league) showed a red "The backend returned an error (503)" box. | `components/BackendProblem.tsx` | Confirmed (empty DB probe) |
| High | Phone form fields were 13 px; iOS Safari zooms the page when a field under 16 px takes focus. | `app/globals.css` | Confirmed size; zoom is documented iOS behaviour, not device-tested |
| Medium | The pinned first column on phone tables was the lineup slot (QB, RB…), so player names scrolled away. | Overview roster, Start/Sit lineup | Confirmed |
| Medium | Matchup on phones: two stacked team lists, so no slot could be compared in place; week navigation duplicated (prev/next chips plus a week strip). | `app/matchup/Lineups.tsx` | Confirmed |
| Medium | Start/Sit: the recommendation was an inline prose sentence after a five-stat strip. | `app/start-sit/page.tsx` | UX concern |
| Medium | Waivers: filters before the decision; chip rows clipped ("Ig…") with no sign they scroll. | `app/waivers/page.tsx` | Confirmed |
| Medium | Trade builder on phones: both full rosters stacked; picks on the roster off screen were invisible. | `app/trades/TradeBuilder.tsx` | Confirmed |
| Medium | At 1024 px the sync pill truncated to "NFL s…"; adding a fifth section would have overflowed 721–768 px (measured 71 px). | app bar | Confirmed |
| Medium | `/leagues` with the prototype off rendered disabled "Connect ESPN (unavailable)" buttons and an empty "Connected leagues" panel. | `app/leagues/page.tsx` | Confirmed |
| Medium | Metric definitions mostly lived in `title` tooltips, which touch users cannot open. | tables site-wide | UX concern |
| Medium | Disclosure summaries were 17–21 px tall tap targets. | site-wide | Confirmed (measured) |
| Low | Team value used its own team form instead of the shared picker; page eyebrows mixed "Projections", "Player research", "Research lab". | various | Confirmed |
| Low | Identity badges at 10 px. | `components/PlayerIdentity.module.css` | Confirmed |

## 2. Design direction

Kept the identity (Night navy, one electric-blue accent, Barlow Condensed numbers, IBM Plex text; matches the
"Fantasy Intel" design system artifact) and improved execution rather than restyling.

- **Navigation model: five destinations.** My team (Overview, Start/Sit, Matchup, Injuries, Team value), **Waivers**
  (Waivers, Kickers), Players (Rankings, Player profile, Compare + Research cluster), Trades (Analyzer, Discover),
  League. Five maps one-to-one onto a phone tab bar; Waivers became primary because it is the other weekly job.
  Desktop: section links plus a sticky sub-nav band. Below 1100 px the theme/projection switches and data status
  move into the menu so the five links always fit. Phones: tab bar + top bar are the only fixed chrome; the sub-nav
  scrolls away (also on landscape phones). The menu carries a dot when data is stale.
- **Decision → evidence → method.** A new `Verdict` primitive (headline sentence, context, 2–4 backing figures) opens
  Start/Sit, Waivers and the trade result.
- **Dense, comparable phone layouts** instead of card walls: ranked rows, slot pairs, compact trade cards; disclosure
  for secondary detail.
- **Explanations without hover:** `Glossary` ("Terms on this page") from one dictionary, `lib/glossary.ts`.
- **Touch and type tokens:** `--space-*`, `--text-*`, `--title-size`, `--tap` (44 px); 16 px fields on phones.

## 3. Implemented changes

Kit (`components/ui`): `Verdict`/`VerdictFigure`, `Glossary`, `StatStrip compact`, `PlayerIdentity lead` slot,
global `.filter-box` disclosure, 44 px phone targets, section titles at 16 px.

| Page | Change |
|---|---|
| App bar / nav | Five groups, sub-nav as its own sticky band (scrolls away on phones and short landscape screens), tablet menu, sync status in the menu with a stale dot, labelled phone search field. |
| Team picker | One composite control; viewing another team is flagged in caution text ("read-only, nothing is sent to ESPN"). |
| Overview | "Before kickoff" action list in the wider column with the tool shortcuts inside it; compact season strip; record and rank in the header; injuries, needs and standings in the side column; slot inside the pinned player cell. |
| Start/Sit | Verdict band (submitted, recommended, gain, locks; all labelled projected); each move as Start/Bench cards with ▲/▼, explanation behind "Why this move"; player pinned with slot. |
| Matchup | Phone slot-pair list (mine · slot · theirs, leader highlighted, ESPN-fallback tag, projected/actual announced). |
| Waivers | Claim verdict and cards first; filters behind a summary on phones. |
| Trades | Phone two-team switch with counts; sticky bar names every pick and sticks to the viewport bottom on all widths; result opens with a verdict band (both teams' ROS deltas, no grade). |
| Discovery | Phone trade cards (swap, both deltas, verdict; asset balance, lineup change and checks behind a disclosure); coverage strip moved below results with a visible notice when the list is not a proven top list; collapsible filters; one-row sort strip. |
| Rankings | Phone ranked rows (~105 px each, was ~300), sort shortcuts, filters behind a summary. |
| Player profile | Outlook (forward view) first, then role, then weekly evidence. |
| Onboarding | First-run guide with the real setup (league id, `.env` keys, sync commands); `/leagues` without fake controls, with how this deployment gets its league. |
| Secondary pages | Shared team picker on Team value; section-named eyebrows; glossaries on Injuries, Schedule, Compare; skeletons match the new layouts. |

Unchanged by design: every calculation, endpoint, URL parameter, deep link, team carry-over, projection-source
switch and tab memory. No new dependencies.

## 4. Mobile QA

| Check | Coverage | Result |
|---|---|---|
| Route sweep (status, overflow, console) | 25 routes × 320 / 390 / 768 / 1440 × light and dark (200 loads) | 0 overflow, 0 console errors, all 200 |
| App bar fit | 721, 768, 1024 px | Fixed (was 71 px / 24 px overflow at 721/768 with five links) |
| Device emulation (Chromium with mobile viewport, DPR, touch, UA) | iPhone SE, iPhone 13, iPhone 13 landscape, Pixel 7, Galaxy S9+, iPad Mini, iPad Mini landscape × 13 routes | 0 overflow, tab bar never covers the end of a page, 0 errors |
| Touch interactions | Trade builder taps (switch sides, pick, evaluate), filters, sort, glossary via `hasTouch` | Pass (`e2e/workflows.spec.ts`) |
| Playwright projects | desktop-1440, mobile-375, mobile-320, tablet-768 | See verification |

Not available here: WebKit/Mobile Safari (the Playwright browser download is blocked in this environment), real
devices, VoiceOver/TalkBack runs.

## 5. Verification

`npm run lint` clean · `npm run typecheck` clean · `npm test` 541/541 (adds glossary and first-run tests) ·
`npm run build` OK · `npm run e2e` 224 passed, 16 skipped (viewport-specific skips) across desktop-1440, mobile-375,
mobile-320 and tablet-768, on the final commit. An independent review of the full diff found 5 medium and 9 low issues; all were fixed.

## 6. Screenshots

`docs/screenshots/ui-overhaul/`: phone Overview, Start/Sit, Matchup, Rankings, Waivers, Trade analyzer, Discovery and
desktop Overview and Start/Sit, before and after (dark theme, synthetic e2e league). Full-page captures show the fixed
tab bar part-way down the page; on a device it stays at the bottom of the screen.

## 7. Remaining limitations

- Not tested on WebKit or real phones; iOS zoom fix verified by computed size only.
- The synthetic league has no nflverse usage, so usage columns render as dashes in screenshots (correctly).
- Advanced pages (Luck lab, Playoff race, Projection accuracy, Research lab, Data quality) got the shared kit, nav and
  eyebrows but no bespoke layout work; their charts were not redesigned.
- Phone tables that scroll sideways (Start/Sit lineup, Team value, Schedule) remain scroll-with-pinned-name by choice.
- Discovery on phones is still long with many trades (about 500 px per trade); a "show more" pager would cut it further.
- The multi-league prototype's enabled states (connecting, syncing, connected) were not exercised: they need the
  feature flag and a signed-in user, which this deployment does not have.
