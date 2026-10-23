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
| #201 perf budgets | 1 | MERGED | PR #235 → 7fea582 | security review: 1 MEDIUM + 3 LOW fixed; Codex P2s fixed |
| #202 UX/visual gates | 1 | MERGED | PR #230 → `3526fb1` (2026-10-22) | visual gate PENDING: run "Visual baselines" workflow, review, commit PNGs |
| #168 signal provenance | 1 | MERGED | PR #208 → `509a45f` (2026-10-22) | contract `signal-provenance.v1` |
| #169 league rules | 1 | MERGED | PR #207 → `7574d6e` (2026-10-22) | |
| #167 onboarding | 1 | MERGED | PR #233 → `2a6c47d` (2026-10-22) | real-league demo needs a backend demo concept (follow-up) |
| #170 desktop shell | 1 | MERGED | PR #239 → abe7664 | rail + team picker + stale SyncPill (#201); Codex P2 fixed; CI E2E green |
| #165 weekly hub | 2 | MERGED | PR #232 → fecb029 | Codex fixes in |
| #166 action center | 2 | MERGED | PR #241 → 07484b1 | `GET /v2/action-center`; security review MEDIUM + 6 LOW fixed over 2 rounds |
| #171 what-if lineup | 2 | MERGED | PR #242 → 5b4fe14 | phone tap fixed; security review clean (pre-existing LOW → #243) |
| #177 bye/kickoff planner | 2 | PR OPEN | PR #244 (766044f) | MEDIUM fixed + re-check RESOLVED; e2e league-boundary race fix ported from #245; CI running |
| #186 player news | 2 | NOT STARTED | — | source licensing gate first |
| #187 player profile tabs | 2 | MERGED | PR #245 → 82ecade | 5 tabs, no News (#186 gate); review LOWs + e2e race fixed |
| #193 transactions feed | 2 | MERGED | PR #231 → `c1bd2f7` (2026-10-22) | |
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
| #228 Sleeper taxi eligibility | fix | MERGED | PR #234 → 3917f7a | Codex P2s fixed |
| #229 superflex replacement level | fix | MERGED | PR #238 → c419856 (2 Codex P2s fixed) | a 632 + audits/golden 194; rules row → HANDLED (verified waiver needs + flex reference count OP) |
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
- MERGED #246 → 9faee55: quick budget timeout → UNVERIFIED unless a failure was already printed; typed `timed_out` flag; test_schedule_intelligence (73 s) deferred. Codex P2 + review LOWs fixed.
- MERGED #243 → ef21c42: response-cache key re-encoded so `%3D`/`%26` can't forge another URL's key (LOW from #242 review).
- MERGED #240 → e234416: pg_harness advisory lock for cluster-wide role setup (CI race `tuple concurrently updated` in
  test_overlapping_pytest_sessions_isolation, reproduced locally); ported into #238.
- MERGED #236 → 9d68883 (`affected` adds the app-wide audits). MERGED #237 → e6bb3f3 ( quick defers `test_league_scope` /
  `test_v2_authorization_matrix`, D-08 budget and D-09 cleanup rules reach `docs/agent-workflow.md`). Retarget #237
  to main after #236 squash-merges; CI runs only on PRs to main.
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

## Verification receipts (merged)
`q`=quick, `a`=affected, `s`=security tier, `L`=full local suite, CI=exact-SHA GitHub CI.
| PR | Final SHA | Local tiers | CI | Notes |
|---|---|---|---|---|
| #206 | 11b40f2 | lint/tc/test/build | green | |
| #208 | d42cee4 | q a s | green | sec review |
| #207 | 62a6463 | q a s + targeted after main merge | green | 2 CI-only audit failures fixed (cause of D-08) |
| #233 | 3c53ad6 | q a s + L (1 timing flake) | green | L was redundant with CI (D-08) |
| #231 | 03d3460 | q a s + L + targeted after main merge | green | L was redundant with CI (D-08) |
| #230 | 9bce26c | local E2E subsets | green | visual gate pending |
| #232 | (head at merge) | targeted + UX gates on main+hub | green | |
| #234 | b81a17e | targeted 125 + fe 639 after main merge | green | |
| #245 | 2e267dc | fe 686 + player-tabs/ux-gates e2e + multileague 2 locally | green | D-10 correctness review, Codex stalled |
| #246 | 625b7ca | test_agent_check 40 (real run_command timeout) | green | Codex P2 + D-10 review LOWs fixed |
| #241 | 248ea21 | action_center 25, a 689, s 627, fe 665 + combined 95 after #242 merge | green | D-10 review + re-check |
| #243 | 6153556 | a 106 + s 623; test fails on main | green | D-10 review: no collisions (9.4k fuzz) |
| #242 | d275d1d | q/a/s + 28 + 17 league_scope + what-if e2e 8 + ux-gates start-sit | green | D-10 security review clean |
| #239 | b2cfe8e | lint/tc/unit 652/build + nav/ux-gates specs | green | tablet networkidle timeout local-only; passed in CI |
| #240 | a29829d | race repro 1300→0, pg suites 22 | green | Codex stalled, no findings |
| #238 | 1d1cf41 | a 632 + 369 trade/golden/audits | green | |
| #237 | d393429 | test_agent_check 35, quick 2.6 s | green | |
| #236 | 87b7c7c | test_agent_check 32 | green | |
| #235 | 6164e84 | targeted 60 + s (623) | green | stale base, no file overlap with main → merged as is (D-07) |

## Lessons

- `agent_check affected` used to miss the app-wide source audits; PR #236 adds them. Do not run full local suites
  instead (D-08): CI on the exact SHA is the full gate.
- Since #241 the action center streams same-league `?team=` links above the league list: e2e link locators on
  `/leagues` must scope to the region named exactly "Your leagues" (fixed in #245, ported to #244).
- Agents must not wipe shared scratchpads or other shared state (the #229 agent ran `rm -rf <scratchpad>/*`). Rule in
  `docs/agent-workflow.md` via PR #237 (D-09).
- CI job logs can't be downloaded here (blob host blocked); `get_job_logs` with a small tail shows the failing test
  list. To reproduce, run that shard locally with `pytest --shard=N/8`.
- Local Playwright: alias the installed Chromium 1194 as the expected 1243 under /tmp/claude-0/pwalias (headless shell
  dir `chrome-headless-shell-linux64/chrome-headless-shell` → `headless_shell`), then run with
  `PLAYWRIGHT_BROWSERS_PATH=/tmp/claude-0/pwalias`.
- Disk: /tmp/pytest-of-root grows by GBs per full run, so prune dirs older than 30 min.

## Resume

1. Read this file and DECISIONS.md.
2. `git fetch origin && git worktree list && git branch -a`; list open PRs; reconcile the board above.
3. Continue the "Active wave", or start the next one.
