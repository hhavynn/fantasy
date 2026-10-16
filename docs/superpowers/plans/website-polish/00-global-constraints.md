# Global constraints for every website-polish milestone

Read this before your milestone prompt. It is binding.

## Repository and stack

- Repo root: the git worktree you were given. App: `fantasy-intel/`.
  - Backend: `fantasy-intel/backend` — FastAPI + DuckDB, Python 3.12 via `uv`. Tests: `uv run pytest`.
  - Frontend: `fantasy-intel/frontend` — **Next.js 16.4 App Router, React 19.3**, TypeScript, plain CSS
    (`app/globals.css` + CSS modules). No Tailwind, no component library, no new runtime
    dependencies unless the milestone prompt explicitly allows one.
- `fantasy-intel/frontend/AGENTS.md`: this Next.js has breaking changes vs. your training data.
  Before using any Next API you are unsure about (`loading.tsx`, `error.tsx`, `not-found.tsx`,
  `generateMetadata`, `PageProps`/`LayoutProps` helpers, `searchParams` as a Promise, icons,
  fonts), read the relevant guide under `fantasy-intel/frontend/node_modules/next/dist/docs/`.
- Pages are **server components** that call `api<T>(path)` from `lib/api.ts` (returns
  `{ok, data, error}`); interactivity uses small `"use client"` islands. Keep it that way.
  State that should survive reloads/sharing lives in the URL (`searchParams`), not React state.
- Read `fantasy-intel/frontend/PRODUCT.md` and
  `docs/superpowers/plans/website-polish/ROADMAP.md` (product principles section).

## Design system (Night theme, established in PR #25)

- Tokens live in `:root` in `app/globals.css`: surfaces `--bg --surface --surface-raised
  --surface-hover`, lines `--line --line-control`, text `--ink --ink-muted --ink-faint`,
  `--accent --on-accent --accent-soft`, status `--positive --negative --caution` (+ `-soft`),
  `--focus`, fonts `--font-display` (Barlow Condensed: titles, big numbers), `--font-sans`
  (IBM Plex Sans: UI), `--font-mono`. Legacy aliases (`--panel`, `--faint`, …) exist; do not
  introduce new uses of the legacy names.
- Use tokens, never raw hex, in new CSS. Every text/background pair must meet WCAG AA.
- Type: page title = `h1` (display font); section header = `.panel > h2:first-child`;
  big numbers use `--font-display` with `font-variant-numeric: tabular-nums`.
- Spacing scale: 4, 8, 12, 16, 24, 32, 48 px. Radius: 6 (controls), 10 (surfaces).
- Status is never colour-only: pair colour with text, sign (+/−) or an icon.
- After M1 lands, build with the kit in `components/ui/` (see M1 for the API). Do not create
  parallel one-off versions of a primitive the kit already has. If the kit lacks something
  you need, add it to the kit **additively** (new file or new export; never change an
  existing primitive's props or behaviour in a way that breaks other pages).
- **No new inline `style={{…}}`** except computed geometry (chart coordinates, meter widths,
  dynamic colours from data). Page-specific CSS goes in a CSS module beside the page.
- Copy: sentence case, factual, compact. No exclamation marks, no emoji, no marketing tone.

## Data honesty rules (from the existing product — never break these)

- `null`/missing renders as a muted `—` with a `title` reason (`Missing`/`num`/`pct` in
  `lib/format.tsx`); `0` is a measured zero and renders as `0`.
- No trade grades, acceptance probabilities, win probabilities, clinching/elimination
  claims, or any number the backend did not compute. The UI may sort, filter, sum and
  compare backend numbers; it must not invent new analytics. If a milestone needs a new
  number, it is computed and unit-tested in the backend.
- Keep every existing explanation of method/assumptions reachable (collapsible
  `details.method` is fine), and keep sample sizes and caveats visible next to claims.

## Scope discipline

- Do exactly your milestone. Don't refactor pages owned by another milestone. Parallel
  milestones M3–M6 own disjoint routes:
  M3 `app/start-sit`, `app/my-team`; M4 `app/players`, `app/waivers`;
  M5 `app/trades` (+ `components/TradeVsFreeAgency.tsx`, `lib/trade-*`);
  M6 `app/schedule-intelligence`, `app/luck-lab`, `app/playoff-race`, `app/managers`, `app/data`.
- `app/globals.css` is shared: in M3–M6 you may only **append** a clearly delimited block if
  something is genuinely global; prefer CSS modules.
- Preserve existing behaviour, URLs, query parameters and tests unless the prompt says otherwise.
- Do not change analytics semantics in the backend. New backend code = new read-only
  endpoints/helpers with pytest coverage, following `app/api/*.py` patterns.

## Environment you can use

- A backend with a fresh, synced database is already running at **http://127.0.0.1:8001**
  (read-only API). Point your dev server at it: `API_URL=http://127.0.0.1:8001`.
- If you change backend code, run your own backend from your worktree on your assigned
  backend port against the shared database (read-only API, safe to share):
  `cd fantasy-intel/backend && uv sync && FANTASY_DB_PATH=/Users/havyn/Documents/CS/Fantasy/fantasy-intel/data/fantasy.duckdb uv run uvicorn app.main:app --port <your backend port>`
  Never run the sync scripts or anything that writes the shared database.
- Frontend: `cd fantasy-intel/frontend && npm ci` once, then
  `API_URL=http://127.0.0.1:<backend port> npx next dev -p <your frontend port>` in the background.
- Each milestone prompt assigns your ports. Stop the servers you started before you finish.
- If browser tools (`mcp__Claude_Browser__*`) are available to you, use them to look at your
  pages at 1440px and 375px. If not, verify by fetching every touched route with `curl` and
  checking for HTTP 200 and the absence of "Couldn't load data" / "Application error" /
  "Unhandled Runtime Error" in the HTML, plus the server log for errors.

## Verification before you open the PR (all must pass; paste results in the report)

From `fantasy-intel/frontend`: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`.
From `fantasy-intel/backend` (only if you touched backend): `uv run pytest -q`.
Live render check of every route you touched (see above), at desktop and phone widths when
you have a browser.

## Git and PR

- Work on the branch named in your milestone prompt (create it from your worktree's HEAD,
  which is current `main`). Small, logical commits with clear messages. End every commit
  message with a blank line and:
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`
- Push with `git push -u origin <branch>` and open a PR against `main` with
  `gh pr create --base main`. PR body: What changed, Why, Screens/routes checked,
  Testing (commands + results), Reviewer notes (anything unsure). End the body with:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- Do not merge. The lead reviews and merges.

## Report

Write a full report to the report path in your prompt (what you built, files, verification
output, deviations, open concerns). Reply with only: Status (DONE | DONE_WITH_CONCERNS |
BLOCKED | NEEDS_CONTEXT), branch, PR URL, commit list, one-line verification summary,
concerns. If blocked, say exactly why.
