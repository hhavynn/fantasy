# M1 — Foundation & UI kit

Branch: `polish/m1-foundation` · Ports: backend 8001 (shared, already running) · frontend 3101
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M1-report.md` (outside the repo; never commit it)

## Goal

Give every later milestone one coherent kit to build with, and make the app feel finished at
the edges (loading, errors, empty states, titles, icon). Overview (`app/page.tsx`) is the
reference page that proves the kit. No other page is restyled in this milestone, but every
page must keep rendering exactly as before (the old `components/ui.tsx` exports keep working).

## 1. Kit structure

Create `fantasy-intel/frontend/components/ui/` and move/extend primitives there. Keep
`components/ui.tsx`'s current exports working by turning it into a re-export barrel
(or make `components/ui/index.ts` the barrel and keep `@/components/ui` imports valid —
check how TypeScript resolves `@/components/ui` when both `ui.tsx` and `ui/index.ts` exist and
pick one; there must be exactly one).

Primitives (server-component friendly unless noted; each in its own file; CSS in
`components/ui/ui.module.css` or co-located modules — not globals, except tokens):

| Component | API (props) | Notes |
|---|---|---|
| `PageHeader` | `title: ReactNode; meta?: ReactNode; actions?: ReactNode; children?` | h1 + muted meta line + right-aligned actions slot; wraps on phones. Replaces `.page-head`. |
| `Section` | `title?: ReactNode; actions?: ReactNode; footnote?: ReactNode; flush?: boolean; id?; children` | Renders the existing `.panel` + header bar look. `actions` sit right of the title. `flush` = table runs edge to edge (current `.panel > .table-wrap` behaviour). `footnote` renders muted 13px text below. |
| `Method` | `title?: string = "How it's calculated"; children` | The existing `details.method` collapsible as a component. |
| `StatStrip` + `Stat` | `Stat: label, value: ReactNode, note?: ReactNode, tone?: "positive"|"negative"|"caution"` | The existing one-surface strip with dividers. `value` uses display font. Tone tints the value text only (and pairs with sign/text, never colour alone). |
| `Notice` | `tone?: "info"|"caution"|"negative"|"positive"; title?: ReactNode; children` | Supersedes `Callout`/`ErrorBox` (keep those as thin wrappers). Icon glyph per tone, not just "!". |
| `EmptyState` | `title: string; children?; action?: ReactNode` | Centered, calm, inside a section. |
| `Skeleton` | `w?: number|string; h?: number; radius?` + `SkeletonTable rows cols`, `SkeletonStatStrip n` | Shimmer that is disabled under `prefers-reduced-motion`. |
| `Delta` | `value: number|null|undefined; digits?=1; suffix?; invert?: boolean` | Signed number with ▲/▼ glyph and tone (invert for "lower is better"). Missing → `Missing`. |
| `Badge`, `Pos`, `TrendArrow`, `Score`, `Sparkline` | unchanged props | Move as-is. Sparkline stroke uses `--chart-1`. |
| `Tabs` | `items: {href, label, count?}[]; current: string; label: string` | URL-backed tab bar (links, `aria-current`). For in-page view switching via search params. |
| `SortableTh` (+ `lib/sort.ts`) | see §2 | |

Also export a tiny `cx(...classes)` helper from `lib/cx.ts`.

## 2. URL-backed table sorting

Server components can't pass render functions to client components, so sorting is done on
the server from search params:

- `lib/sort.ts`: `parseSort(sp, allowed: readonly string[], fallback: {key, dir})` →
  `{key, dir}`; `sortRows<T>(rows, accessor: (row) => number|string|null|undefined, dir)` —
  stable, **nulls always last regardless of direction**, strings compared with
  `localeCompare`, numbers numerically. Unit tests in `lib/sort.test.ts` (nulls last both
  directions, stability, strings, mixed).
- `SortableTh`: `{ sortKey, label, current: {key, dir}, searchParams, align?: "left"|"right", title?, defaultDir?: "asc"|"desc" }`
  renders `<th aria-sort=…>` containing a `<Link>` that toggles direction and preserves all
  other search params. Shows a subtle ▲/▼ on the active column. `scroll={false}` on the Link.
- Use it on the Overview standings table (sort by W-L/PF/PA) to prove it.

## 3. Charts

- Add chart tokens to `:root`: `--chart-1` … `--chart-6` (accent blue first; a categorical
  palette distinguishable for common colour-vision deficiencies, all ≥ 3:1 against
  `--surface`), `--chart-grid`, `--chart-axis`, `--chart-positive`, `--chart-negative`.
- `components/charts/` (server-rendered SVG, no dependencies, responsive via `viewBox` +
  `width: 100%`, all text in `--font-sans` 11–12px using `--ink-faint`):
  - `LineChart`: `series: {id, label, color?, points: (number|null)[]}[]`, `xLabels: string[]`,
    `yFormat?`, `height?`, `yDomain?`, `markers?: boolean`, `refLine?: {y, label}`. Null = gap.
    Includes a legend when >1 series and `<title>`/`aria-label` summary for screen readers.
  - `BarChart`: vertical or horizontal bars, `data: {label, value: number|null, tone?}[]`,
    diverging support (negative values below/left of zero axis with `--chart-negative`).
  - `ScatterChart`: `points: {id, x, y, label?, highlight?}[]`, axis labels, optional
    diagonal `y = x` reference, collision-avoiding labels (port the greedy placement from
    `app/luck-lab/page.tsx` into a shared helper `components/charts/labels.ts`; do not change
    the luck lab page itself — M6 migrates it).
  - Shared `components/charts/scale.ts` (linear scale, nice ticks) with unit tests in
    `lib/` or co-located `*.test.ts` that the `npm test` glob picks up (update the `test`
    script glob if you co-locate; keep `lib/*.test.ts` running).
- Do not migrate existing page charts (M3/M6 do). Add one demo use on Overview only if it
  serves the page (e.g. a points-for by week sparkline is fine if the data exists; skip if not).

## 4. Route states

- `app/loading.tsx`: skeleton page (header + stat strip + table skeleton).
- `app/error.tsx` (client): friendly error surface with the message, a "Try again" button
  (`reset`), and a link to Data quality.
- `app/not-found.tsx`: on-brand 404 with links to the four nav groups.
- Backend-offline and schema-out-of-date handling: `lib/api.ts` should classify errors
  (`kind: "offline" | "schema" | "http" | "unknown"`) without changing the existing
  `{ok,data,error}` shape (add an optional `kind`). A `BackendProblem` component renders
  each kind helpfully: offline → how to start the backend; schema (detail contains
  "Database schema") → the exact sync commands; http → status + detail. Use it on Overview
  (replacing `SetupHelp`) and in `error.tsx`. Other pages keep `ErrorBox` for now.
- Root layout: when `/api/status` fails, the app bar must still render cleanly (it does
  today — keep it).

## 5. Metadata and icon

- Root metadata: `title: { default: "Fantasy Intel", template: "%s · Fantasy Intel" }`,
  description "Fantasy football war room for your league".
- Add `export const metadata = { title: "<Page>" }` to every existing page file (one line
  each — this is the only edit allowed in pages other than Overview). Dynamic pages
  (`managers/[teamId]`, players with `?id`) may use a static title for now.
- App icon: replace `app/favicon.ico` usage with `app/icon.svg` — a simple, crisp mark
  (e.g. stylised "fi" monogram or yard-line motif in `--accent` on `--bg`, readable at 16px)
  and `app/apple-icon.png` only if you can generate it without new deps (skip otherwise).
  Keep `favicon.ico` if removing it breaks anything.

## 6. Overview as the reference page

Rebuild `app/page.tsx` on the kit with zero inline styles and the same information:
PageHeader (team name; meta = league · season · week · teams · scoring · waiver type),
StatStrip, roster Section (flush table with Starters/Bench group rows), needs Section with
Method, injuries Section with EmptyState when none, standings Section with SortableTh,
recent transactions Section. Visual result should look at least as good as today and more
consistent. M2 will redesign Overview's content; your job is the structure and kit.

## 7. Docs

Update `fantasy-intel/frontend/PRODUCT.md`: replace "Preserve ... system fonts" with the
current typography (Barlow Condensed display, IBM Plex Sans/Mono) and add a short "Kit"
section listing the primitives and the no-inline-style rule.

## Acceptance

- All checks in global constraints pass; `npm test` includes the new sort/scale tests.
- Every existing route still renders (curl or browser every nav route + `/data` +
  `/managers/<id>` + `/players?id=<id>`), Overview renders with the kit, 404 page works
  (`/nope`), loading skeleton appears (throttle or inspect), error page reachable (stop
  pointing at the backend, e.g. `API_URL=http://127.0.0.1:9` dev run, and check Overview shows the
  offline BackendProblem rather than a crash).
- `grep -c 'style={{' app/page.tsx` → 0.
