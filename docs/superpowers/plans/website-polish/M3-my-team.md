# M3 — My team pages: Start/Sit and Team value

Branch: `polish/m3-my-team` · Ports: backend 8001 (shared) · frontend 3103
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M3-report.md` (outside the repo; never commit it)
Owned files: `app/start-sit/**`, `app/my-team/**`, `lib/start-sit-*`. Runs in parallel with M4–M6.

## Goal

Rebuild `/start-sit` and `/my-team/value` on the kit so each opens with the decision, shows
the evidence compactly, and keeps the method one click away. Same data, same URLs and query
params, same honesty rules. First read both pages end to end, the API responses
(`curl http://127.0.0.1:8001/api/start-sit`, `/api/my-team/value`), and the docs
`fantasy-intel/docs/weekly_start_sit.md`, `fantasy-intel/docs/v2_team_value.md`, so you
understand every field before moving anything.

## /start-sit

- PageHeader: "Start/Sit · Week N" with lock status in meta; controls (`StartSitControls`)
  in the header actions slot; keep their URL behaviour.
- **Verdict band** first: the recommended lineup vs the current lineup — projected total of
  each and the gain (`Delta`), how many swaps, and which slots are locked by kickoff. If no
  changes: positive "Your lineup is optimal for the projections" state. If no submitted
  lineup exists, follow the backend's semantics (no actual total / no gain — recent fixes
  enforce this; do not regress it).
- **Swaps list**: each recommended move as a row "Start A (proj) over B (proj) · +x.x ·
  slot · reason/attribution", with lock badges. Actionability classes from the backend shown
  as text badges.
- **Lineup comparison table**: slot-by-slot current vs recommended (flush Section,
  SortableTh not needed — slot order is meaningful).
- Bench/available alternatives and the unconstrained benchmark as secondary sections.
- Method + caveats in `Method`.
- Replace `start-sit.module.css` (488 lines) with a lean module built on kit primitives;
  delete styles that become unused.

## /my-team/value

- PageHeader + StatStrip (roster strength rank vs league, ROS projected starting points,
  regular vs playoff split, unused bench projection) — use only numbers the endpoint returns.
- **Player marginal value**: sortable table (SortableTh via URL params) of every rostered
  player's exact lineup marginal value (regular/playoff, weekly starts, IR activation upside
  shown separately, as today) plus a horizontal diverging `BarChart` of the top contributors.
- Slot strength and removal-scenario depth: a compact table per slot; upgrade slot-weeks
  and realistic add/drop + 1-for-1 trade preview as their own sections with clear headers.
- Playoff race context: a small linked summary (link to `/playoff-race`).
- Migrate any hard-coded chart colours to chart tokens / `components/charts`.

## Rules

- Zero inline `style={{}}` in owned files except chart geometry. Use `Section`, `StatStrip`,
  `Notice`, `EmptyState`, `Method`, `Delta`, `Badge`.
- Add `app/start-sit/loading.tsx` and `app/my-team/value/loading.tsx` skeletons that mirror
  the new layouts.
- Phones (375px): verdict band and swaps list readable without horizontal scroll; wide
  tables scroll inside their section.
- Keep `lib/start-sit.test.ts` passing; add tests for any new pure helper you extract.

## Acceptance

Global checks pass; both routes (with and without their query params, e.g. a different
week) render at 1440 and 375 with real data; no lost information vs. the old pages (list
in your report every section of the old page and where it lives now).
