# FantasyPros-class roadmap: execution status

Tracks the 40 child issues of [#205](https://github.com/hhavynn/fantasy/issues/205). The issue text is the spec. This
file records state, branches and the next action. Decisions and blockers are in [DECISIONS.md](DECISIONS.md).

States: NOT STARTED · IN PROGRESS · PR OPEN · VERIFIED / MERGE READY (CI green on head, reviews resolved) · MERGED (sha, date)
· BLOCKED · RESEARCH-ONLY.
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
| #201 perf budgets | 1 | IN PROGRESS | `feature/fi-201-perf-budgets` | wave 3 agent |
| #202 UX/visual gates | 1 | VERIFIED / MERGE READY | PR #230 (a7f261a) | visual gate stays PENDING until reviewed baselines are committed (follow-up) |
| #168 signal provenance | 1 | MERGED | PR #208 → `509a45f` (2026-10-22) | contract `signal-provenance.v1` |
| #169 league rules | 1 | PR OPEN | PR #207 | main merged in (62a6463); CI re-running, merge next |
| #167 onboarding | 1 | PR OPEN | PR #233 (609f00f) | security-reviewed; CI running |
| #170 desktop shell | 1 | NOT STARTED | — | after wave 2; sole owner of Nav/globals |
| #165 weekly hub | 2 | PR OPEN | PR #232 (240fdcd, retargeted to main after #208) | CI running |
| #166 action center | 2 | NOT STARTED | — | after #165 |
| #171 what-if lineup | 2 | NOT STARTED | — | reuse lineup.py locks |
| #177 bye/kickoff planner | 2 | NOT STARTED | — | |
| #186 player news | 2 | NOT STARTED | — | source licensing gate first |
| #187 player profile tabs | 2 | NOT STARTED | — | news tab optional on #186 |
| #193 transactions feed | 2 | VERIFIED / MERGE READY | PR #231 | CI green on 6334342; merges after #207 (shared router list/nav) |
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

## Expansion track (#227: #209–#226, plus correctness fixes #228–#229)

Kept separate from #205. It is an additive backlog: nothing here preempts an active #205 issue (D-06).

| Issue | Group | State | Branch / PR | Gate (prerequisite) |
|---|---|---|---|---|
| #228 Sleeper taxi eligibility | fix | IN PROGRESS | `feature/fi-228-sleeper-taxi` | none; agent running |
| #229 superflex replacement level | fix | NOT STARTED | — | next free slot after #228 (D-05) |
| #212 Breakout Radar | A | NOT STARTED | — | #168 merged; backtest gate |
| #210 Game Splits | A | NOT STARTED | — | backend now; UI after #170 |
| #226 Advanced screener | A | NOT STARTED | — | backend now; UI after #170/#200; feeds #219 |
| #225 Projection change attribution | A | NOT STARTED | — | reuses projection_movers; audit-restricted modules |
| #216 Marginal roster utility | B | NOT STARTED | — | #169 + #178 lineup contract |
| #218 Stash/handcuff planner | B | NOT STARTED | — | #216 |
| #209 Cross-league portfolio | B | NOT STARTED | — | #166 |
| #211 Pace / game-script lab | B | NOT STARTED | — | #188 |
| #213 NFL what-if lab | C | NOT STARTED | — | #190; never alters production forecasts |
| #214 Trade counteroffers | C | NOT STARTED | — | #182, #183 |
| #217 Playoff rooting guide | C | NOT STARTED | — | #192 |
| #215 Manager trade tendencies | C | NOT STARTED | — | #193 (PR #231), #182 |
| #222 Custom scoring sensitivity | C | NOT STARTED | — | #169 (PR #207) |
| #219 Ask Fantasy Intel | D | NOT STARTED | — | #226, #200; no paid LLM |
| #221 Fantasy Wrapped | D | NOT STARTED | — | #194, #197 |
| #224 Offline / PWA | D | RESEARCH-ONLY | — | #196 tenant-safe persistence |
| #220 Three-team sandbox | E | RESEARCH-ONLY | — | stable trade engines |
| #223 Keeper/dynasty readiness | E | RESEARCH-ONLY | — | #185, #204; taxi via #228 |

## Out-of-roadmap fixes

- MERGED PR #206 → `c18374a` (2026-10-22): Next 16.4.0 → 16.4.2 for GHSA-2464-fhr4-w9xw, GHSA-p3m5-59mp-jv5r and
  GHSA-673q-mjm6-r5xr. It also stops the intermittent `SecurityFatalError` build abort.
- #228 and #229 are tracked in the expansion table above.
- Known #202 triage to clear: matchup heading order, player-note links, trade checkbox size, Sleeper Start/Sit
  "(null)", and the ESPN_MY_TEAM_ID hint shown to users.

## Active wave

**Wave 1** (parallel, isolated worktrees under `.worktrees/`, all from `5315c96`):
- #169 rules: owns the new `/league-rules` page and endpoint, plus one entry in `lib/navigation.ts`.
- #168 provenance: owns the descriptor contract, a `components/ui` badge and small edits to six pages.
- #202 gates: owns `e2e/`, the Playwright configs, CI E2E steps and `docs/qa/`.
- Lead integration after wave 1: deep links from Start/Sit, Trades and Waivers to Rules; E2E run; PRs.

**Wave 2 (next):** #201 perf (backend cache/single-flight + timing), #167 onboarding (`app/leagues/*`), then #165
weekly hub using the #168 contract. #170 runs after these, alone, because it owns Nav/layout/globals.

## Lessons

- `agent_check affected` misses the app-wide source audits (test_weekly_learning, test_dst_research,
  test_independent_projections). Before pushing a branch, run the full backend suite in 4 local shards.
- CI job logs can't be downloaded here (blob host blocked), so reproduce a failing shard locally with
  `pytest --shard=N/8`.

## Resume

1. Read this file and DECISIONS.md.
2. `git fetch origin && git worktree list && git branch -a`; list open PRs; reconcile the board above.
3. Continue the "Active wave", or start the next one.
