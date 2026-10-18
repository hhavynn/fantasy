# Fantasy Intel: website polish roadmap

Date: 2026-10-14 · Owner: Havyn · Lead/reviewer: Claude Opus · Implementers: Claude Sonnet

## Where the site is today

Fantasy Intel is a local, read-only fantasy football war room: FastAPI + DuckDB backend
(`fantasy-intel/backend`) and a Next.js 16 App Router dashboard (`fantasy-intel/frontend`).
The analytics are deep and well tested (start/sit, waivers, trade analyzer and discovery,
luck lab, playoff race, team value, schedule intelligence, manager scouting, independent
projections). The interface has had one overhaul (PR #25: Night-theme tokens, Barlow
Condensed + IBM Plex type, grouped nav, stat strips) but is still an engineer's dashboard:

| Area | State |
|---|---|
| Design system | Tokens exist in `globals.css`; UI primitives are a single 100-line `components/ui.tsx`. ~150 inline `style={{…}}` blocks across pages. Chart colours hard-coded per page (`#4a96ec`, …). |
| Home | A roster table plus needs/injuries/standings. No answer to "what do I do this week?" and nothing about **this week's matchup**. |
| Matchup | No page. The data exists (`matchups`, `team_week_lineup_projections`). |
| Tables | Static; not sortable; wide tables scroll the whole panel on phones. |
| States | No `loading.tsx`, `error.tsx` or `not-found.tsx`. A backend error prints a raw string; a stale DB schema shows a raw 500 detail. |
| Charts | Ad-hoc SVG per page (luck lab scatter, playoff race, team value), inconsistent axes/labels. |
| Player profile | `/players?id=` renders a long table dump; no visual usage trends. |
| Navigation | Grouped nav is good. No global search, no keyboard shortcuts, no per-page `<title>`. |
| Theme | Dark only. |

## Product principles (binding for every milestone)

1. **Decision first, evidence second.** Every page opens with the answer ("Start X over Y,
   +4.1 pts"), then the evidence, then the method (collapsible).
2. **Honest data.** Keep the house rules: missing data renders as a muted `—` with a reason;
   `0` is a measured zero; no trade grades, acceptance probabilities, win probabilities or
   claims the analytics don't make. Never invent numbers in the UI.
3. **Dense but calm.** This is a data product for one power user. Compact tables are right;
   walls of identical cards and decorative tiles are wrong. Hierarchy comes from type scale,
   spacing and restraint, not boxes.
4. **One system.** Every page is built from the same primitives (page header, section,
   stat strip, data table, notice, chart). No new inline styles; page-specific CSS goes in a
   CSS module next to the page.
5. **Works everywhere.** 375px phones to 1440px+ desktops, keyboard navigable, WCAG AA
   contrast, visible focus, `prefers-reduced-motion` respected.

## Milestones

Dependency graph: `M1 → M2 → {M3, M4, M5, M6 in parallel} → M7 → M8`.

| # | Milestone | Outcome | Depends on |
|---|---|---|---|
| M1 | Foundation & UI kit | Component library (`components/ui/*`), URL-backed sortable tables, chart primitives + chart tokens, route loading/error/not-found states, friendly backend-offline / schema-migration screens, per-page titles, app icon. Overview adopts the kit as the reference page. | — |
| M2 | This Week: home + matchup | New `/api/matchup` endpoint and `/matchup` page (head-to-head lineups slot by slot, projected totals, opponent context). Home becomes a weekly command center: matchup hero, prioritized action items (start/sit swaps, injured starters, top waiver claim), roster. | M1 |
| M3 | My team pages | Start/Sit and Team value rebuilt on the kit: verdict-first layouts, charts for projections/marginal value, no inline styles. | M2 |
| M4 | Players & waivers | Player search + a real player profile (header, usage charts by week, forward view), Waivers with a top-recommendations band and sortable table. | M2 |
| M5 | Trades | Analyzer builder UX (team pickers, selected-player chips, sticky evaluate bar), verdict-first results with weekly delta chart; Discover as a sortable, filterable opportunity table. | M2 |
| M6 | League pages | Schedule, Luck lab, Playoff race, Managers (+ detail/scouting), Data quality on the kit; every chart on the shared chart primitives and tokens. | M2 |
| M7 | Global UX | ⌘K command palette (pages + player search), keyboard shortcuts, light theme with toggle (system default), mobile pass on every table (sticky first column), reduced-motion-aware micro-interactions. | M3–M6 |
| M8 | Final QA & polish | Route-by-route audit at 1440/768/375 in both themes, accessibility and copy pass, performance pass (Suspense streaming for slow sections), README/PRODUCT.md refresh. Ship list = zero known visual defects. | M7 |

## Process per milestone

1. Lead writes the milestone prompt (this folder, `Mx-*.md`) on top of
   [`00-global-constraints.md`](00-global-constraints.md).
2. A Sonnet implementer works in an isolated git worktree on branch `polish/mX-<slug>`,
   verifies (lint, typecheck, unit tests, build, backend pytest when touched, and a live
   render check of every route it touched), pushes and opens a PR against `main`.
3. Lead reviews the PR diff and renders the branch in the browser at desktop and phone
   widths. Findings go back to the same implementer; repeat until clean.
4. Lead merges (merge commit) once CI is green, then pulls `main` before the next wave.
   Parallel milestones (M3–M6) own disjoint files; shared-kit changes are additive only
   and conflicts are resolved by the lead at merge time.

## Definition of done (site level)

- Every route renders without console errors at 375/768/1440 in dark and light themes.
- Every page follows the decision → evidence → method structure and uses the kit.
- Zero inline `style={{…}}` except computed geometry (chart coordinates, meter widths).
- All tables sortable where ordering is meaningful; sticky headers; phone-friendly.
- Loading skeletons on every route; friendly error and empty states everywhere.
- CI green: backend pytest, frontend lint, typecheck, unit tests, production build.
