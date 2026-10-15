# Fantasy Intel

## Register

product

## Users and purpose

A local ESPN league manager reviewing player usage and league behavior to make informed roster decisions. Reports are deterministic, read-only, explainable, and run locally (README).

## Voice

Factual, compact, cautious. Separate current conditions from historical tendencies; show evidence and sample limits with every classification.

## Design principles

- Preserve the existing dark dashboard (Night theme), table vocabulary and restrained blue accent. Typography: Barlow Condensed for page titles and big numbers (`--font-display`, tabular figures), IBM Plex Sans for UI text (`--font-sans`), IBM Plex Mono for code (`--font-mono`).
- Prioritize fast scanning with compact tables and progressive disclosure of detailed evidence.
- Keep unknown data explicit and use text alongside color.
- Support narrow screens, keyboard navigation and readable contrast.

## Anti-references

The supplied brief rules out giant walls of cards and unsupported claims. Avoid decorative dashboard tiles and personality claims based on tiny samples.

## Kit

Build pages from the shared primitives instead of one-off markup. UI primitives live in `components/ui/` (import from `@/components/ui`), chart primitives in `components/charts/` (import from `@/components/charts`). Both are server-component friendly and styled with CSS modules and the tokens in `app/globals.css`.

- Layout: `PageHeader` (title, meta line, actions), `Section` (header bar, `flush` for edge-to-edge tables, `footnote`), `StatStrip` + `Stat`, `Tabs` (URL-backed views), `Method` (collapsible "how it's calculated").
- Data: `SortableTh` with `lib/sort.ts` (`parseSort`, `sortRows`) for URL-backed server-side sorting, `Delta` (signed change with glyph), `Badge`, `Pos`, `Score`, `TrendArrow`, `Sparkline`.
- States: `Notice` (info, caution, negative, positive), `EmptyState`, `Skeleton`, `SkeletonTable`, `SkeletonStatStrip`, `BackendProblem` (backend offline, stale schema, HTTP error). `app/loading.tsx`, `app/error.tsx` and `app/not-found.tsx` cover the route states.
- Charts: `LineChart`, `BarChart`, `ScatterChart`, coloured by the `--chart-1` to `--chart-6` tokens.
- Helpers: `cx()` in `lib/cx.ts`.

Rules: no new inline `style={{...}}` except computed geometry (chart coordinates, meter widths, colours that come from data); page-specific CSS goes in a CSS module beside the page; if the kit lacks a primitive, add it to the kit additively rather than building a parallel one-off.
