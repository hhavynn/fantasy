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
- Identity: deep navy foundations with one electric-blue accent; the wordmark is uppercase Barlow Condensed beside the bar mark (`BrandMark` in `components/Nav.tsx`). Page titles sit under a small accent eyebrow (the section or week). Table headers, stat labels and eyebrows are small uppercase labels; values carry the weight.
- Colour has fixed meanings: accent for the selected thing and links; positive, negative and caution for direction and availability; `--chart-1` (blue) for your side and `--chart-2` (orange) for the other side in every head-to-head view (matchup card, trade builder, trade result); position accents (QB `--chart-4`, RB `--chart-3`, WR `--chart-1`, TE `--chart-2`, K `--chart-6`, D/ST `--chart-5`) only as the thin edge of the `Pos` tag.
- Shape and frame: radii from `--radius-xs` to `--radius-lg`, one page frame (`--page-max`, `--gutter`) and one gap between page sections (`--section-gap`).
- Copy is sentence case and factual: no exclamation marks, no emoji, no "(s)" plural hedges (use `plural()` from `lib/plural.ts`), and no engine identifiers in prose (`tidyReason()` in `lib/reason-text.ts` formats the engine's reason sentences for display).

## Anti-references

The supplied brief rules out giant walls of cards and unsupported claims. Avoid decorative dashboard tiles and personality claims based on tiny samples.

## Kit

Build pages from the shared primitives instead of one-off markup. UI primitives live in `components/ui/` (import from `@/components/ui`), chart primitives in `components/charts/` (import from `@/components/charts`). Both are server-component friendly and styled with CSS modules and the tokens in `app/globals.css`.

- Layout: `PageHeader` (eyebrow, title, meta line, actions), `Verdict` + `VerdictFigure` (decision headline), `Glossary`, `Section` (header bar with optional `subtitle`, `flush` for edge-to-edge tables, `footnote`), `StatStrip` + `Stat` (`compact` for supporting context), `Tabs` (URL-backed views), `Method` (collapsible "how it's calculated").
- Players: `PlayerIdentity` (`components/PlayerIdentity.tsx`: photo, linked name, position and NFL team line, extra `meta`) is the player cell for tables and cards; `PlayerLink` stays for running text.
- Data: `Meter` (a number with a 0-max data bar for table cells), `SortableTh` with `lib/sort.ts` (`parseSort`, `sortRows`) for URL-backed server-side sorting, `Delta` (signed change with glyph), `Badge`, `Pos`, `Score`, `TrendArrow`, `Sparkline`.
- Provenance: `ProvenanceBadge` (signal-provenance.v1 from `lib/signal-provenance.ts`): a compact badge that says in words whether a headline's data is current, partial, stale, of unknown age or unavailable, with a native disclosure listing each signal's source, capture time, coverage, snapshot and reason. Pass it to `PageHeader`'s `provenance` slot (or under a `Verdict`); the status is derived at render, never stored.
- States: `Notice` (info, caution, negative, positive), `EmptyState`, `Skeleton`, `SkeletonTable`, `SkeletonStatStrip`, `BackendProblem` (backend offline, stale schema, HTTP error). `app/loading.tsx`, `app/error.tsx` and `app/not-found.tsx` cover the route states.
- Charts: `LineChart`, `BarChart`, `ScatterChart`, coloured by the `--chart-1` to `--chart-6` tokens. Each takes an optional `title` and `caption` (source, sample weeks or freshness), drawn by `ChartFrame`. Hovering a line series brings it forward and dims the others.
- Interactive charts: `InteractiveScatter` (client) for scatter and bubble plots that need hover, focus and tap tooltips, keyboard-selectable markers and URL-backed selection (the Research lab). It measures its container so text and markers keep real pixel sizes on phones; `y = x` only when both axes share a unit. Server-rendered `ScatterChart` stays for static plots.
- Helpers: `cx()` in `lib/cx.ts`, `plural()` in `lib/plural.ts`, number formatting in `lib/format.tsx` (`num`, `pct`, `signed`, `Missing`).
- Also exported: `ToneIcon`, `VisuallyHidden`, `Callout` and `ErrorBox` (legacy wrappers over `Notice`).

Rules: no new inline `style={{...}}` except computed geometry (chart coordinates, meter widths, colours that come from data); page-specific CSS goes in a CSS module beside the page; if the kit lacks a primitive, add it to the kit additively rather than building a parallel one-off.

## Loading and streaming

- Every route has a `loading.tsx` skeleton (the root one is the fallback) and the root `error.tsx` and `not-found.tsx`.
- A page that awaits one slow source beside fast ones starts the slow request without awaiting it, and renders the slow part inside `<Suspense>` with a skeleton of the same footprint, so the header and first section stream at once. Examples: the Overview week band (`app/WeekBand.tsx`) and the Trade analyzer result (`app/trades/EvaluatedTrade.tsx`). Do this only where the wait is real; a section that needs the same payload as the page header cannot stream.
- Fonts come from `next/font` (self-hosted, size-adjusted fallbacks), so loading them does not shift the layout. Player photos render at fixed square sizes with an initials placeholder, so they reserve their space and never shift the layout.

## Quality bar

Before a page ships: no horizontal page scroll from 320 to 1600px in both themes; one `h1`; every text pair at least WCAG AA (`lib/theme-contrast.test.ts` guards the tokens); status never by colour alone; long names wrap or ellipsise with a `title`; no `style={{...}}` except computed geometry; no raw colours outside the token blocks in `globals.css`; zero console errors.

## Global UX (M7)

- Navigation: five groups organised by the decision a manager came to make, one registry (`NAV_GROUPS` in `lib/navigation.ts`) for the app bar, sub-nav, phone menu, tab bar and command palette. My team holds this week's lineup decisions (Overview, Start/Sit, Matchup, Injuries, Team value); Waivers is its own destination (Waivers, Kickers) because it is the other weekly job; Players leads with Rankings and keeps the model and opportunity tools in a labelled `Research` cluster (`section: "Research"` on a page; the sub-nav and menu draw it as a divider, never a second level); Trades is find then evaluate; League is what happened and where the league stands. Five groups map one-to-one onto the phone tab bar. A page may carry a one-line `summary` (link title and palette search). Page eyebrows name the section ("Players · Research"). Desktop shows the groups with an accent underline and a pill sub-nav, a separate sticky band under the app bar labelled with the group; the section links never shrink. Below 1100px the theme and projection switches and the data status move into the menu button (it carries a dot when data is stale or offline), so the five links always fit. Phones (720px and below) drop the section links for a bottom tab bar (`TabBar` in `components/Nav.tsx`) and let the sub-nav scroll away with the page, so only the top bar and the tab bar are fixed (landscape phones do the same with the sub-nav); search becomes a labelled field. A tab reopens the page last used in its group with its filters (per browser tab; a page remembered under another team keeps only its path), and the current group's tab goes back to the group's first page. The menu lists every page plus data freshness, the theme and projection switches and Data quality, and closes on any URL change, Escape, an outside tap or tabbing out.
- Team context: every link to a team-scoped page (nav, palette, brand, `TeamLink` in pages, trade Evaluate) keeps the analyzed team via `withTeam`. League-wide pages never write `team` into their own URL; links from them use the team last analyzed in this browser tab (`useTeam()` in `components/useTeam.ts`), so Start/Sit for team 4 → Rankings → My team stays on team 4. "Back to default" on a team-scoped page is remembered too.
- Filters: chip rows (`.chip`, active via `.active` or `aria-current`) are filled when selected; a `.chip.reset` link appears beside active filters. On phones a chip row scrolls sideways rather than wrapping into a tall panel, and a page's secondary filters sit in a `details.filter-box` whose summary says what is selected (open, with no summary row, on wider screens).
- Decisions: a decision page opens with a `Verdict` (`components/ui/Verdict.tsx`): one headline sentence, a line of context and the two to four numbers behind it (`VerdictFigure`), then the evidence, then the method. Start/Sit, Waivers and the trade result use it.
- Explanations: `Glossary` (`components/ui/Glossary.tsx`) lists the terms a page shows ("Terms on this page"), from one dictionary in `lib/glossary.ts`; a definition never lives only in a hover tooltip.
- Touch: primary targets are at least 44px on phones (`--tap`), fields use 16px text so iOS does not zoom, and phone layouts keep dense, comparable rows (Rankings list, Matchup slot pairs) rather than one tall card per item.
- First run: a backend with no imported league shows `FirstRunGuide` (in `components/BackendProblem.tsx`), the real setup steps, never an error box.
- Command palette (`components/CommandPalette.tsx`, Ctrl/Cmd+K or `/`): pages and quick actions from `lib/navigation.ts` and `lib/palette.ts`, players from `app/api/search/route.ts`, which reaches the backend through `api()` so the hosted-mode key stays on the server.
- Themes: tokens live in `:root` (Night), `:root[data-theme="light"]` and `@media (prefers-color-scheme: light)`; the two light blocks must be identical and every pair must pass AA (`lib/theme-contrast.test.ts` checks both). Define any new colour token in all three places. Never write raw colours in components.
- Tables: `.table-wrap` scrolls sideways and pins its first column (opt out with `.table-wrap.flat` for tables that restack into cards). Sticky offsets under the app bar use `var(--appbar-h)`.
- Motion: 120-180ms ease-out on paint properties only (`--ease-out`, `--dur-fast`, `--dur`, `--dur-slow`); the closing `prefers-reduced-motion` block in `globals.css` disables all of it, so keep it last. Rows of links that scroll sideways (tabs, sub-nav) include `<ScrollToActive />`.
