# FantasyPros-class roadmap: execution status

Tracks the 40 child issues of [#205](https://github.com/hhavynn/fantasy/issues/205). The issue text is the spec. This
file records state, branches and the next action. Decisions and blockers are in [DECISIONS.md](DECISIONS.md).

States: NOT STARTED · IN PROGRESS · READY FOR REVIEW (branch/PR open, gates recorded) · MERGED · BLOCKED · RESEARCH-ONLY.
"Pushed" is not "merged"; "tested locally" is not "CI-verified".

## Baseline (audit 2026-10-22)

- `origin/main` = `5315c96` (#164), CI green on that SHA. #205 was written against the same commit, and no roadmap
  work existed before this program. The only open PR was #147 (video graphics, unrelated).
- Already shipped (reuse, never rebuild): Start/Sit, Matchup, Waivers + device-local shortlist, trade analyzer +
  discovery, two-player compare, player profiles/usage, rankings, power rankings, playoff race, luck lab, managers,
  weekly recap, kickers, D/ST, injuries, multi-league /v2 (ESPN public/private, Sleeper), capability matrix.
- Local env notes: Postgres tests need `FI_TEST_PG_DSN` (initdb refuses root; start a cluster as `postgres`, see
  DECISIONS.md D-03). E2E uses fixed ports, so only one worktree runs Playwright at a time.

## Issue board

| Issue | Phase | State | Branch / PR | Notes |
|---|---|---|---|---|
| #201 perf budgets | 1 | NOT STARTED | — | wave 2 |
| #202 UX/visual gates | 1 | IN PROGRESS | `feature/fi-202-ux-gates` | wave 1 |
| #168 signal provenance | 1 | IN PROGRESS | `feature/fi-168-signal-provenance` | wave 1; contract reused by #165+ |
| #169 league rules | 1 | IN PROGRESS | `feature/fi-169-league-rules` | wave 1 |
| #167 onboarding | 1 | NOT STARTED | — | wave 2 (after #169 capabilities) |
| #170 desktop shell | 1 | NOT STARTED | — | after wave 2; sole owner of Nav/globals |
| #165 weekly hub | 2 | NOT STARTED | — | needs #168 contract |
| #166 action center | 2 | NOT STARTED | — | after #165 |
| #171 what-if lineup | 2 | NOT STARTED | — | reuse lineup.py locks |
| #177 bye/kickoff planner | 2 | NOT STARTED | — | |
| #186 player news | 2 | NOT STARTED | — | source licensing gate first |
| #187 player profile tabs | 2 | NOT STARTED | — | news tab optional on #186 |
| #193 transactions feed | 2 | NOT STARTED | — | |
| #178 add/drop simulator | 3 | NOT STARTED | — | |
| #180 claim planner | 3 | NOT STARTED | — | extends shortlist |
| #179 FAAB intelligence | 3 | NOT STARTED | — | capability-gated |
| #181 K/DST streaming | 3 | NOT STARTED | — | |
| #182 targeted trade discovery | 4 | NOT STARTED | — | |
| #183 trade scenarios | 4 | NOT STARTED | — | |
| #184 trade-market values | 4 | NOT STARTED | — | |
| #185 draft-pick valuation | 4 | RESEARCH-ONLY | — | |
| #188 SOS heatmaps | 5 | NOT STARTED | — | |
| #189 projection comparison | 5 | NOT STARTED | — | |
| #190 opportunity trees | 5 | NOT STARTED | — | |
| #198 defensive matchups | 5 | NOT STARTED | — | data-rights research first |
| #199 consensus/ADP | 5 | RESEARCH-ONLY | — | |
| #200 saved views/search | 5 | NOT STARTED | — | |
| #191 positional strength | 6 | NOT STARTED | — | |
| #192 playoff what-if | 6 | NOT STARTED | — | |
| #194 post-game feedback | 6 | NOT STARTED | — | |
| #172 confidence/alternatives | 6 | NOT STARTED | — | calibration evidence required |
| #173 live matchup | 6 | NOT STARTED | — | provider cadence limits |
| #174 win probability | 6 | RESEARCH-ONLY | — | backtest gate; PRODUCT.md bans unsupported probabilities |
| #175 multi-player compare | 6 | NOT STARTED | — | two-player compare exists |
| #176 practice timeline | 6 | NOT STARTED | — | |
| #195 alerts/digest | 7 | NOT STARTED | — | consent required |
| #196 cross-device saved | 7 | NOT STARTED | — | Supabase RLS |
| #197 shareable reports | 7 | NOT STARTED | — | |
| #203 lineup submission | 8 | RESEARCH-ONLY | — | no provider writes |
| #204 Draft Wizard-lite | 8 | NOT STARTED | — | after in-season stable |

## Active wave

**Wave 1** (parallel, isolated worktrees under `.worktrees/`, all from `5315c96`):
- #169 rules: owns the new `/league-rules` page and endpoint, plus one entry in `lib/navigation.ts`.
- #168 provenance: owns the descriptor contract, a `components/ui` badge and small edits to six pages.
- #202 gates: owns `e2e/`, the Playwright configs, CI E2E steps and `docs/qa/`.
- Lead integration after wave 1: deep links from Start/Sit, Trades and Waivers to Rules; E2E run; PRs.

**Wave 2 (next):** #201 perf (backend cache/single-flight + timing), #167 onboarding (`app/leagues/*`), then #165
weekly hub using the #168 contract. #170 runs after these, alone, because it owns Nav/layout/globals.

## Resume

1. Read this file and DECISIONS.md.
2. `git fetch origin && git worktree list && git branch -a`; list open PRs; reconcile the board above.
3. Continue the "Active wave", or start the next one.
