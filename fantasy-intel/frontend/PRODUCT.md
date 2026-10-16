# Fantasy Intel

## Register

product

## Users and purpose

A local ESPN league manager reviewing player usage and league behavior to make informed roster decisions. Reports are deterministic, read-only, explainable, and run locally (README).

## Voice

Factual, compact, cautious. Separate current conditions from historical tendencies; show evidence and sample limits with every classification.

## Design principles

- Preserve the dark dashboard (Night theme), table vocabulary and restrained blue accent. A light theme (System, Light or Dark, chosen in the app bar) uses the same token names. Typography: Barlow Condensed for page titles and big numbers (`--font-display`, tabular figures), IBM Plex Sans for UI text (`--font-sans`), IBM Plex Mono for code (`--font-mono`).
- Prioritize fast scanning with compact tables and progressive disclosure of detailed evidence.
- Keep unknown data explicit and use text alongside color.
- Support narrow screens, keyboard navigation and readable contrast.
- Decision first, evidence second, method last: a page opens with the answer, then the table or chart behind it, then a collapsible `Method` with the assumptions.
- Honest data: a muted `—` with a `title` reason means missing, `0` is a measured zero, and the UI never invents a number. No trade grades, acceptance or win probabilities, or clinching and elimination claims; the UI may sort, filter, sum and compare numbers the backend computed. Sample sizes and caveats stay next to the claims they limit.
- Dense but calm: compact tables, restrained surfaces, hierarchy from type scale and spacing rather than boxes.
- Copy is sentence case and factual: no exclamation marks, no emoji, no "(s)" plural hedges (use `plural()` from `lib/plural.ts`), and no engine identifiers in prose (`tidyReason()` in `lib/reason-text.ts` formats the engine's reason sentences for display).

## Anti-references

The supplied brief rules out giant walls of cards and unsupported claims. Avoid decorative dashboard tiles and personality claims based on tiny samples.

## Kit

Build pages from the shared primitives instead of one-off markup. UI primitives live in `components/ui/` (import from `@/components/ui`), chart primitives in `components/charts/` (import from `@/components/charts`). Both are server-component friendly and styled with CSS modules and the tokens in `app/globals.css`.

- Layout: `PageHeader` (title, meta line, actions), `Section` (header bar, `flush` for edge-to-edge tables, `footnote`), `StatStrip` + `Stat`, `Tabs` (URL-backed views), `Method` (collapsible "how it's calculated").
- Data: `SortableTh` with `lib/sort.ts` (`parseSort`, `sortRows`) for URL-backed server-side sorting, `Delta` (signed change with glyph), `Badge`, `Pos`, `Score`, `TrendArrow`, `Sparkline`.
- States: `Notice` (info, caution, negative, positive), `EmptyState`, `Skeleton`, `SkeletonTable`, `SkeletonStatStrip`, `BackendProblem` (backend offline, stale schema, HTTP error). `app/loading.tsx`, `app/error.tsx` and `app/not-found.tsx` cover the route states.
- Charts: `LineChart`, `BarChart`, `ScatterChart`, coloured by the `--chart-1` to `--chart-6` tokens.
- Interactive charts: `InteractiveScatter` (client) for scatter and bubble plots that need hover, focus and tap tooltips, keyboard-selectable markers and URL-backed selection (the Research lab). It measures its container so text and markers keep real pixel sizes on phones; `y = x` only when both axes share a unit. Server-rendered `ScatterChart` stays for static plots.
- Helpers: `cx()` in `lib/cx.ts`, `plural()` in `lib/plural.ts`, number formatting in `lib/format.tsx` (`num`, `pct`, `signed`, `Missing`).
- Also exported: `ToneIcon`, `VisuallyHidden`, `Callout` and `ErrorBox` (legacy wrappers over `Notice`).

Rules: no new inline `style={{...}}` except computed geometry (chart coordinates, meter widths, colours that come from data); page-specific CSS goes in a CSS module beside the page; if the kit lacks a primitive, add it to the kit additively rather than building a parallel one-off.

## Loading and streaming

- Every route has a `loading.tsx` skeleton (the root one is the fallback) and the root `error.tsx` and `not-found.tsx`.
- A page that awaits one slow source beside fast ones starts the slow request without awaiting it, and renders the slow part inside `<Suspense>` with a skeleton of the same footprint, so the header and first section stream at once. Examples: the Overview week band (`app/WeekBand.tsx`) and the Trade analyzer result (`app/trades/EvaluatedTrade.tsx`). Do this only where the wait is real; a section that needs the same payload as the page header cannot stream.
- Fonts come from `next/font` (self-hosted, size-adjusted fallbacks), so loading them does not shift the layout. The pages use no images (only the generated app icons), so there is nothing to reserve space for.

## Quality bar

Before a page ships: no horizontal page scroll from 320 to 1600px in both themes; one `h1`; every text pair at least WCAG AA (`lib/theme-contrast.test.ts` guards the tokens); status never by colour alone; long names wrap or ellipsise with a `title`; no `style={{...}}` except computed geometry; no raw colours outside the token blocks in `globals.css`; zero console errors.

## Global UX (M7)

- Command palette (`components/CommandPalette.tsx`, Ctrl/Cmd+K or `/`): pages and quick actions from `lib/navigation.ts` and `lib/palette.ts`, players from `app/api/search/route.ts`, which reaches the backend through `api()` so the hosted-mode key stays on the server.
- Themes: tokens live in `:root` (Night), `:root[data-theme="light"]` and `@media (prefers-color-scheme: light)`; the two light blocks must be identical and every pair must pass AA (`lib/theme-contrast.test.ts` checks both). Define any new colour token in all three places. Never write raw colours in components.
- Tables: `.table-wrap` scrolls sideways and pins its first column (opt out with `.table-wrap.flat` for tables that restack into cards). Sticky offsets under the app bar use `var(--appbar-h)`.
- Motion: 120-180ms ease-out on paint properties only (`--ease-out`, `--dur-fast`, `--dur`, `--dur-slow`); the closing `prefers-reduced-motion` block in `globals.css` disables all of it, so keep it last. Rows of links that scroll sideways (tabs, sub-nav) include `<ScrollToActive />`.
