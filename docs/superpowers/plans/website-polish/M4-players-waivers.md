# M4 — Players & waivers

Branch: `polish/m4-players-waivers` · Ports: backend 8001 (shared) · frontend 3104
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M4-report.md` (outside the repo; never commit it)
Owned files: `app/players/**`, `app/waivers/**`, `components/PlayerLink.tsx`. Parallel with M3, M5, M6.

## Goal

Make player research feel like a product: fast search, a real player profile with visual
usage trends, and a waiver page that leads with the claims worth making. Read both pages,
their API responses (`/api/players?q=`, `/api/players/{id}`, `/api/players/{id}/forward`,
`/api/waivers` with its filters) and `fantasy-intel/docs/waiver-v3-delivery.md` first.

## /players (search) — `?q=`

- PageHeader "Players"; a prominent search input (GET form, keeps `?q=`), results as a
  flush table: name (PlayerLink), Pos, NFL team, fantasy team or "Free agent" badge,
  status badge, key season numbers the endpoint returns. Empty query: show a helpful
  EmptyState (and, if the endpoint supports an empty query listing, the top results).
- No results: EmptyState with the query echoed.

## Player profile — `?id=` (keep the URL; do not add a new route)

- **Profile header**: name (display font), position/team/bye, fantasy owner (or FA),
  injury/availability badges with sources, ID-match confidence when not high.
- **Key numbers StatStrip**: PPR/g, xFP/g, value score, market signal, trend — whatever the
  detail endpoint already returns.
- **Usage over time**: `LineChart`s from `components/charts` by week: snap share; target
  share + air-yard share (receivers) / team rush share + backfield share (RBs); fantasy
  points vs xFP. Null weeks are gaps (bye/inactive), never zeros — the chart primitive
  already treats null as a gap. Choose charts by position (no empty RB charts for a WR).
- **Forward view** section (from `/forward`): upcoming weeks, opponent context, projections.
- **Weekly game log**: the existing weekly table, flush, horizontally scrollable, sticky
  first column on phones; group advanced columns in a "More columns" toggle (URL param or
  `details`) so the default view is scannable.
- Keep every metric currently shown reachable.

## /waivers

- PageHeader with filters (position chips, recommendation chips — keep existing query
  params) in the actions/filters row.
- **Top claims band**: the top 3 recommended adds as compact cards (this is one of the few
  places cards are right): player, position/team, recommendation badge, waiver score, role
  trend, league competition (LOW/MED/HIGH + likely competitors), suggested drop.
- **Full table**: sortable (SortableTh, URL-backed) by waiver score, trend, % owned change,
  competition; reasons collapsible per row.
- Replace `waivers.module.css` with a lean module.
- Method section keeps the scoring explanation.

## Rules

Zero inline styles in owned files except chart geometry; loading.tsx for `/players` and
`/waivers`; phones without page-level horizontal scroll; add unit tests for any new pure
helper (e.g. choosing charts by position, building series from weekly rows).

## Acceptance

Global checks pass; `/players`, `/players?q=<common name>`, `/players?q=zzzz`,
`/players?id=<a WR>`, `?id=<an RB>`, `?id=<a QB>`, `?id=<a D/ST or K>` and `/waivers` with
and without filters all render at 1440 and 375 with real data. Report lists every old
section and where it lives now.
