# M5 — Trades: analyzer and discovery

Branch: `polish/m5-trades` · Ports: backend 8001 (shared) · frontend 3105
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M5-report.md` (outside the repo; never commit it)
Owned files: `app/trades/**`, `components/TradeVsFreeAgency.tsx`, `components/MovesTable.tsx`
(only if needed), `lib/trade-builder.ts`, `lib/trade-types.ts`. Parallel with M3, M4, M6.

## Goal

The trade analyzer should feel like a tool, not a form: build a trade in seconds, read the
verdict in one glance, dig into evidence only if you want. Read `app/trades/page.tsx`,
`TradeBuilder.tsx`, `app/trades/discover/page.tsx`, `components/TradeVsFreeAgency.tsx`,
`lib/trade-builder.ts` (+ test), the API (`/api/trades/options`, `POST /api/trades/evaluate`,
`/api/discover`), and `fantasy-intel/docs/v3_trade_intelligence.md` +
`v3_1_trade_vs_free_agency.md` before changing anything. The URL-backed selection and the
"no trade grade, no acceptance probability" policy are non-negotiable.

## /trades (analyzer)

- **Builder**: two columns (Your team | Partner). Partner selector at the top of the right
  column. Each side: searchable/filterable roster list grouped by position with checkboxes;
  selected players appear as removable chips above the list. Respect the per-side limit
  (`trade_max_players_per_side`) with a clear inline message. A sticky action bar (bottom on
  phones, top of results on desktop) shows "You give N · You get M · Evaluate". Keep the
  current URL-backed state so a trade is shareable/reloadable.
- **Results, verdict first**: StatStrip of ROS / regular-season / playoff lineup-point deltas
  for **both** teams (`Delta`), mandatory drop / open roster spot notices, then a weekly
  delta `BarChart` (diverging, both teams or yours with partner toggle), starter changes,
  hypothetical FA replacement, trade-vs-free-agency section, player context.
- Empty state before any evaluation: brief explanation and a "try discovery" link.

## /trades/discover

- PageHeader + filter row (partner, trade shape 1-for-1/2-for-1/1-for-2, category) using
  existing params.
- Sortable (SortableTh, URL-backed) results table: give/get players, your Δ, their Δ,
  categories as badges, manager context; each row links into the analyzer with the trade
  preloaded (existing behaviour).
- Remove the ~25 inline styles; lean CSS module.

## Rules

Zero inline styles in owned files except chart geometry; loading.tsx for both routes; phone
layout works (builder columns stack; chips wrap; sticky bar doesn't cover content);
`lib/trade-builder.test.ts` keeps passing and gains tests for any new pure helper.

## Acceptance

Global checks pass; at 1440 and 375: empty analyzer, a 1-for-1 evaluated trade, a 2-for-1
trade that forces a drop, a trade at the per-side limit, `/trades/discover` default and
sorted by another column, and a discover row opening the analyzer preloaded. Report lists
every old section and where it lives now.
