# M6 — League pages

Branch: `polish/m6-league` · Ports: backend 8001 (shared) · frontend 3106
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M6-report.md` (outside the repo; never commit it)
Owned files: `app/schedule-intelligence/**`, `app/luck-lab/**`, `app/playoff-race/**`,
`app/managers/**`, `app/data/**`, `lib/luck-types.ts`, `lib/managers.tsx`,
`lib/manager-scouting.ts`. Parallel with M3, M4, M5.

## Goal

Bring the five league pages onto the kit with one charting language. These pages are
explanatory and historical: lead with the headline finding, then the chart, then the table,
then method. Read each page, its API response, and its doc in `fantasy-intel/docs/`
(`v2_schedule_intelligence.md`, `v2_luck_lab.md`, `v2_playoff_race.md`, `manager-scouting.md`)
first. Keep every caveat and sample-size note visible next to the claim it qualifies.

## Per page

- **/schedule-intelligence**: PageHeader + a week-by-week strip of remaining opponents
  (bye, home/away, regular vs playoff) as a compact horizontal timeline; defense-allowed
  table sortable by metric with ranks and sample sizes; insufficient-data reasons visible.
- **/luck-lab**: StatStrip (your expected wins, actual wins, schedule luck, schedule
  difficulty rank). Actual-vs-expected `ScatterChart` (shared labels helper from M1 —
  delete the page-local placement code), luck `BarChart` (diverging) per team, counterfactual
  schedule table (keep `ScheduleSelector` and its 422-avoidance behaviour), brutal losses /
  fortunate wins lists, consistency.
- **/playoff-race**: standings with the cutline drawn as a table divider row ("Playoff
  line"), bubble teams badged; remaining matchups by week with projected totals and the
  winner-override controls (URL-backed, keep exactly); projected standings scenario; high
  leverage games highlighted. No probabilities/clinch language (unchanged policy).
- **/managers**: grid of manager cards is acceptable here (12 managers) — make them
  scannable: name, record, classification badges with confidence, 2–3 key metrics, your card
  marked. **/managers/[teamId]**: PageHeader with manager name/record, Tabs (Overview ·
  Scouting · Transactions) via `?tab=`, evidence tables with sample sizes, weekly composition
  chart on chart tokens.
- **/data**: health summary first (StatStrip: last syncs, mapping coverage %, unmatched
  count, validation warnings), then the tables. Unmatched players table sortable.

## Rules

All hard-coded chart colours (`#4a96ec` etc.) replaced by chart tokens / `components/charts`;
zero inline styles in owned files except geometry; loading.tsx per route; phones work
(cards one column, tables scroll inside sections, charts scale via viewBox). Unit tests for
any new pure helpers (e.g. cutline insertion, timeline building).

## Acceptance

Global checks pass; every owned route (incl. `/managers/<2 different ids>` with each tab,
luck lab with a schedule selection, playoff race with an override) renders at 1440 and 375
with real data. Report lists every old section and where it lives now.
