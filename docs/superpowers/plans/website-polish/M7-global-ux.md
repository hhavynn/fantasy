# M7 — Global UX: command palette, light theme, mobile tables, motion

Branch: `polish/m7-global-ux` · Ports: backend 8001 (shared) · frontend 3107
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M7-report.md` (outside the repo; never commit it)
Runs after M1–M6 are merged; you may touch shared files (`app/layout.tsx`, `globals.css`,
`components/ui/**`, `components/Nav.tsx`) and make small consistency fixes in pages.

## 1. Command palette (⌘K / Ctrl+K, and "/" to focus search)

- `components/CommandPalette.tsx` (client), mounted once in the layout, plus a visible
  search trigger in the app bar ("Search players, pages… ⌘K"; icon-only on phones).
- Sources: all nav pages (`lib/navigation.ts`), quick actions (e.g. "This week's matchup",
  "Evaluate a trade"), and players via `/api/players?q=` (debounced 150ms, abort stale
  requests). The browser can't reach the backend directly (server-only `API_URL`), so add a
  Next route handler `app/api/search/route.ts` that proxies to the backend's players search
  and returns a slim shape. Keyboard: ↑/↓, Enter, Esc, Tab trapping; `role="dialog"`,
  `aria-modal`, listbox/option semantics; focus restored on close. Recent selections kept in
  `localStorage` (wrapped in try/catch).
- Unit-test the pure parts (ranking/merging of results, keyboard index wrap).

## 2. Light theme

- Light token set under `:root[data-theme="light"]` and `@media (prefers-color-scheme: light)`
  for `:root:not([data-theme="dark"])`; every token (incl. chart tokens) redefined; AA
  contrast verified for every text/surface pair (list the ratios in the report).
- Theme toggle in the app bar (System · Light · Dark), persisted in `localStorage`, applied
  before paint with a tiny inline script in the layout to avoid a flash.
- Walk every route in light mode and fix anything that used raw colours.

## 3. Mobile tables

- A shared, additive table treatment: horizontally scrollable `.table-wrap` with a sticky
  first column (player/team name) and a subtle edge shadow when scrolled; apply to the wide
  tables across pages (players game log, waivers, discover, start-sit comparison,
  team value, data quality, managers).
- Touch targets ≥ 40px for nav, chips and table sort links on phones.

## 4. Motion and micro-interactions

- 120–180ms ease-out transitions on hover/focus/active for links, chips, buttons, rows;
  popover/drawer/palette enter animations; skeleton shimmer. All disabled under
  `prefers-reduced-motion: reduce`. No layout-shifting animation.

## Acceptance

Global checks pass; palette works by keyboard only (record the key sequence you tested);
every route checked in light and dark at 1440 and 375; no flash of the wrong theme on load.
