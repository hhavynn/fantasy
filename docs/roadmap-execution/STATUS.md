# FantasyPros-class roadmap: execution status

Tracks 62 issues in four separate workstreams. The issue text is the spec. This file records state, branches and the
next action. Decisions and blockers are in [DECISIONS.md](DECISIONS.md).

| Workstream | Issues | Count |
|---|---|---:|
| Original FantasyPros-class roadmap ([#205](https://github.com/hhavynn/fantasy/issues/205)) | #165–#204 | 40 |
| Competitive expansion ([#227](https://github.com/hhavynn/fantasy/issues/227)) | #209–#226 | 18 |
| Correctness additions | #228, #229 | 2 |
| League Compatibility & Scoring Correctness (added 2026-10-23, D-12) | #253, #254 | 2 |
| **Total** | | **62** |

#253 and #254 are not part of #205 or #227; those two roadmaps keep their original scope and numbering.

States: NOT STARTED · IN PROGRESS · PR OPEN · VERIFIED / MERGE READY (CI green on head, reviews resolved) · MERGED (sha, date)
· BLOCKED · RESEARCH-ONLY.
"Pushed" is not "merged"; "tested locally" is not "CI-verified". "Issue created" is not "started".

## Progress (reconciled with GitHub 2026-10-23 03:20Z)

Counted from merged PRs (checked via the GitHub API), not from issue open/closed state.

| Workstream | Implementation merged | Research merged | PR open | Not started (incl. research-only) | Total |
|---|---:|---:|---:|---:|---:|
| #205 roadmap | 16 | 1 (#186) | 0 | 23 (1 in progress) | 40 |
| #227 expansion | 0 | 0 | 0 | 18 | 18 |
| Correctness #228–#229 | 2 | 0 | 0 | 0 | 2 |
| Compatibility #253–#254 | 0 (phase 2 of #253 and phase 1 of #254 merged; issues stay open) | 0 | 0 | 2 (follow-up phases) | 2 |
| **Total** | **18** | **1** | **0** | **43** | **62** |

GitHub hygiene: 7 issues whose PRs merged are still open on GitHub (#165, #167, #168, #169, #193, #201, #202). Their
PRs did not use a closing keyword. Closing them is an owner/coordinator action, not done here.

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
| #177 bye/kickoff planner | 2 | MERGED | PR #244 → 8f57407 | bye/coverage planner; security MEDIUM (exponential solve) fixed via bipartite matching |
| #186 player news | 2 | RESEARCH MERGED | PR #247 → 2d2c1c4 | NO-GO headlines; GO "Availability events" from nflverse (D-11); implementation not started |
| #187 player profile tabs | 2 | MERGED | PR #245 → 82ecade | 5 tabs, no News (#186 gate); review LOWs + e2e race fixed |
| #193 transactions feed | 2 | MERGED | PR #231 → `c1bd2f7` (2026-10-22) | |
| #178 add/drop simulator | 3 | MERGED | PR #249 → 17d26e2 (2026-10-23) | security MEDIUM resolved by #252; Codex P2 fixed; LineupTooLargeError 422 test |
| #180 claim planner | 3 | MERGED | PR #255 → e11ff5d (2026-10-23) | review MEDIUM×2 (open-spot/position limits = Codex P2; Sleeper waiver status) + 3 LOW fixed |
| #179 FAAB intelligence | 3 | MERGED #264 → b5ce6c3 | `feature/fi-179-faab-intelligence` from c6ecf8c | capability-gated; owns waiver files after #260 |
| #181 K/DST streaming | 3 | MERGED | PR #250 → 1706634 (2026-10-23) | DP planner; review MEDIUM (hold downgrade) + Codex P2 + 4 LOW fixed |
| #182 targeted trade discovery | 4 | MERGED | PR #258 → 386e546 (2026-10-23) | Codex P1×2 + P2, review MEDIUM + 2 LOW fixed; evidence fetch league-scoped (no exposure) |
| #183 trade scenarios | 4 | MERGED | #263 → 042e229 | |
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

## League Compatibility & Scoring Correctness (#253, #254; D-12)

A separate workstream, added 2026-10-23. It does not replace or re-scope #205 or #227 and does not pause active work.

| Issue | Priority | Workstream | State | Branch / PR |
|---|---|---|---|---|
| [#253](https://github.com/hhavynn/fantasy/issues/253) certify supported league formats, per-tool compatibility diagnostics | P0 | League compatibility certification | PHASE 2 MERGED (PR #256 → 668b198); phase 3 (G2–G4 unsupported-slot integrity) + UI/E2E remain | — |
| [#254](https://github.com/hhavynn/fantasy/issues/254) certify actual custom league scoring, gate unsupported projections | P1 | Actual scoring fidelity | PHASE 1 MERGED (PR #259 → 261728e); phase 2: S3 per-row source, ESPN-fallback label under source=independent (L2), doc fixes D1–D3, cache/fingerprint audit | — |

**Roster support is not scoring fidelity.** A league can be fully supported structurally (every slot parsed, filled
once, flex and superflex solved exactly) while its scoring is only partly reproduced. #253 certifies structure and
reports per-tool readiness. #254 owns whether each analysis computes points under the league's real scoring rules.
Neither issue may report "connected" as "all analytics trustworthy".

**What the workstream tracks** (owner in brackets):
1. Provider connection/import compatibility: ESPN public/private, Sleeper; Yahoo stays unavailable (provider access,
   terms, OAuth). Verified per integration and deployment, never assumed. [#253]
2. Roster configuration and slot eligibility: multi-QB, superflex/OP, multiple FLEX, bench, IR, taxi; atypical and IDP
   slots labelled unsupported per engine, never silently dropped from full-lineup claims. [#253]
3. Actual scoring-rule compatibility: mapped vs unmapped modifiers, slot overrides (TE premium), kicker distance
   buckets, D/ST buckets, bonuses, negative modifiers, missing components. [#254]
4. Analytics-engine support: league rules, starters and flex optimization, Start/Sit, team values, waivers, add/drop,
   trade valuation/discovery, bye coverage, K/DST streaming. Real consuming engines are exercised, not just slot
   parsing. [#253 structure, #254 scoring]
5. Per-feature diagnostics: ready (`handled`) / partial / unsupported / unverified, each with a reason, the missing
   input, the provider boundary and an action. Four separate dimensions: import readiness, roster legality,
   scoring/projection fidelity, feature availability. Contract owned by #253, consumed by #254 (D-12).
6. Authorization and tenant isolation: everything behind `league_scope` / `authorize_league`; no private tenant data
   from global endpoints; scoring fingerprints and caches never mix leagues. [both]
7. Synthetic regression fixtures only: fake league ids, fake team names, synthetic players and scoring. [both]

**Reuse, do not rebuild:** #169 (PR #207: `analytics/league_rules.py` already has the `handled/partial/unsupported/
unverified` vocabulary and per-slot engine status), #167 onboarding and the multileague stack, #229 (PR #238,
OP-aware replacement level), #252 (exact polynomial flex solver, `LineupTooLargeError` → 422), `league_scoring.py`
(already rejects unsupported ESPN scoring and slot overrides), the ESPN provider's mapped/unmapped modifier record,
`signal-provenance.v1`, `docs/multileague/provider-capability-matrix.md`.
Related, not duplicated: #222 (hypothetical scoring sandbox; it should consume #254's support table), #223
(keeper/dynasty readiness), #178 (add/drop, a consumer to certify), #181 (K/DST streaming, a consumer to certify).

**Synthetic regression scenario FX-17.** Motivated by a real six-team ESPN H2H-points redraft league owned by the user
(name, ids, members and rosters are private and never appear in the repo). Its structure is reproduced synthetically:
2 QB, 3 RB, 4 WR, 2 TE, 1 OP, 3 FLEX, 1 K, 1 D/ST (17 starters), 10 bench, 3 IR. Its individual scoring modifiers are
**not verified** (the league-info snapshot does not expose them). FX-17 is a test fixture, not evidence that the live
league has been tested. Public tests and docs use fake ids, fake team names, synthetic players and synthetic scoring.

**#253 completion criteria (P0, cross-cutting release-confidence gate):**
- [ ] Per-tool gap inventory (read-only audit) committed, covering every consumer engine listed above.
- [ ] FX-17 plus the fixture matrix (standard redraft; superflex/multi-FLEX with ties and sparse pools; half/full PPR;
  known and unmapped custom scoring; IR/taxi; IDP/atypical slots) exercised by automated import-to-analysis tests.
- [ ] FX-17 slot counts unchanged by normalization; no player starts twice; 3 FLEX are three interchangeable slots; no
  bench/IR player starts; totals and displacement correct in tied, sparse and multi-flex pools.
- [ ] Every applicable engine answers from supported, source-grounded data or abstains with an actionable reason.
- [ ] Missing or unsupported scoring never becomes a measured zero or a faux league-scored total.
- [ ] Per-league/per-tool diagnostic contract (D-12) implemented with reproducible tests and readable explanations.
- [ ] `league_scope`/tenant-isolation tests pass on the fixtures; no provider writes, no credentials.
- [ ] One responsive/accessible E2E: connect/demo → league rules → lineup/waivers/trades, including an unsupported case.
- [ ] Exact-head CI green, independent security review (D-10) resolved, merged.
P0 means unsupported behavior is reliably identified and disclosed. It does not promise every exotic rule will work.

**#254 completion criteria (P1, incremental):**
- [ ] Documented actual-scoring support table: QB/RB/WR/TE/K/DST × provider × rule category × data source × tool;
  unknowns labelled.
- [ ] Synthetic scoring fixtures with exact expected outcomes for supported cases (zero/negative results, unusual QB
  scoring, multiple FLEX).
- [ ] TE premium/slot overrides, kicker distance buckets, IDP bonuses and missing components never yield fabricated
  league-scored independent results.
- [ ] Provider projections used and labelled as provider values only in the right scope/season/scoring context; no
  rescaling without per-stat inputs.
- [ ] Standard ESPN/Sleeper output and goldens unchanged except reviewed bug fixes.
- [ ] Scoring fingerprints and caches cannot leak wrong-league rules or values.
- [ ] Unsupported rule ids/reasons surface through the D-12 contract, not duplicated scoring logic.
- [ ] Focused tests, app-wide audits, security tier and exact-head CI pass; source-rights unknowns stay blocked.
Research-only outcomes and explicit abstention are acceptable for rules without usable component data.

**Dependencies and order:**
1. Current integration wave: #252 merged (ea9e664); #249/#178 merge reconciliation in progress.
2. #253 starts after #249 merges: read-only gap inventory and FX-17 fixture matrix first, then the D-12 diagnostic
   contract, in one small backend PR; minimal UI on the existing League Rules page only if needed.
3. #254 scheduled from #253's confirmed gaps. An isolated read-only scoring audit may overlap #253 only with disjoint
   file ownership and a free slot. Implementation waits for the D-12 contract.
4. Other work continues in the second slot when file ownership is disjoint (#180/#179 waivers, then Phase 4 trades).
   #222 waits for #254's support table.

**Audit triage (#253 inventory 784b89b, 2026-10-23).** A diagnostic label alone does not resolve a finding while a tool
still shows a misleading number. "Blocking" means a fix or a gate ships before the next release.
| Finding | Security | Wrong-recommendation risk | Who sees it today | Diagnostic enough? | Disposition |
|---|---|---|---|---|---|
| G1 Start/Sit empty-slot total 0.0 "complete" | no | high | all leagues when projections are missing; Sleeper with abstaining Ours | no | **blocking**: fixed in #253 phase 2 (FX-17 + ordinary-league tests) |
| S8 source labelled "Ours" with no numbers | no | medium (attribution) | Sleeper with abstaining Ours | no | **blocking**: fixed in #253 phase 2 ("Unavailable") |
| S1a Ours re-score labelled "scored with this league's rules" | no | medium (false exactness claim) | non-reference-scoring leagues | no | **blocking label fix** in #253 phase 2; algorithm → #254 |
| S1b re-score counts missing stat components as 0 | no | medium | non-reference-scoring leagues | partial | #254 first PR (missing ≠ zero) |
| G5 waivers: missing value → 0.0; OP/FLEX by percentile | no | high (missing-as-zero in a ranking) | all leagues with unprojected players | no | **blocking**, own bounded PR after #255 merges (waiver owner); widen the S2 warning in the same PR |
| G2 Start/Sit gain negative with IDP/P/HC starters | no | high | leagues with slots no engine fills | no | **blocking**, #253 phase 3 (unsupported-slot integrity) |
| G3 totals omit unsupported slots yet say "complete" | no | medium | same leagues | no | **blocking**, #253 phase 3: mark totals partial |
| G4 Sleeper IDP players missing; phantom roster spot | no | high (illegal add/trade shown legal) | Sleeper IDP leagues | no | **blocking**, #253 phase 3: legality `unverified` when IDP slots exist |
| S2 waiver values full-PPR; warning only when PPR ≠ 1 | no | medium | custom-scoring leagues | partial | warning widened with G5; basis → #254 |
| S3 Sleeper actual points mix sources per row | no | low–medium (attribution) | Sleeper leagues | partial | #254 |
| G8 sparse replacement level 0 → PAR inflated | no | low (rare in practice) | very sparse superflex | yes, short term | defer: #182-area follow-up |
| G6, G7, G9, G10 | no | low | specific formats | yes | defer with diagnostics (#253 follow-ups) |
| G11, G12, G14 | no | low | rare | yes | defer (#253 follow-ups) |
| Trade-evidence fetch without `/l/<lg>/` (from #180) | **no exposure**: the backend authorizes by path only; the classic path serves the operator's own league or 404 | wrong-league display | league trade-discovery page | — | fixed in #182 (229cc42) + tenant-boundary tests (f52ff56) |
| `/api/search` without `/l/<lg>/` (CommandPalette, ComparePicker) | no exposure (same analysis) | wrong-league search results | all league pages | — | bounded fix after #182 merges (reuses `leagueRouteHref`) |
| `league_scope._response_key` sorts whole (key, value) pairs | LOW: same scope only | low (playoff-race overrides order) | duplicated params only | — | **MERGED** #257 → 902f38e (review approve; e2e override test = LOW follow-up) |
| Sleeper sync marks every unrostered player FREEAGENT (`sleeper/write.py:355`), so add/drop `claim_required` is wrong | no | medium (claim shown as unneeded) | Sleeper leagues | no | #255 gates it in the planner; add/drop simulator follow-up |

**Likely file overlap:** #253 → `analytics/league_rules.py`, `api/league_rules.py`, `app/league-rules/*`, new synthetic
fixture builders/tests, `provider-capability-matrix.md` (tests read it, so it is never docs-only). #254 →
`league_scoring.py`, `kicker_scoring.py`, D/ST scoring, `rescore.py`, `leagues/providers/espn.py`/`sleeper.py`.
Shared: the D-12 contract and League Rules response. Overlap with #179/#180 (waivers) is tests only; with #222 it is
`league_scoring.py` (so #222 follows #254).

## Out-of-roadmap fixes

- MERGED PR #206 → `c18374a` (2026-10-22): Next 16.4.0 → 16.4.2 for GHSA-2464-fhr4-w9xw, GHSA-p3m5-59mp-jv5r and
  GHSA-673q-mjm6-r5xr. It also stops the intermittent `SecurityFatalError` build abort.
- #228 and #229 are tracked in the expansion table above.
- MERGED #248 → af94873: CI critical path (parallel E2E jobs, docs-only classifier with credential scan, browser cache, apt only when needed). PR CI 9.5–12 min → 6.5–7.5 min; docs-only 33 s (#251 → a438e1c).
- MERGED #252 → ea9e664: shared `_best_flex` polynomial with identical assignments (security MEDIUM from the #249 review).
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

**Lanes (D-13), 2026-10-23 03:25Z:**
| Lane | Item | Owner | Worktree | Slots |
|---|---|---|---|---|
| A | #254 phase 1 (#255 merged 04:39Z) | agent | `.worktrees/fi-254-scoring` | targeted until granted |
| A2 | G5 → PR #260 (efa43cb): local a/s/e2e pass; CI + independent review running | agent | `.worktrees/fix-waiver-missing` | released |
| B | #182 → PR #258 (f91f5b2): local a/s/e2e pass; Codex P1×2 + P2 (streaming cache key) held for batch with security review | agent | `.worktrees/fi-182-trade-discovery` | released |
| C | #253 phase 2 = PR #256: fixing review MEDIUM (G2 wording) + Codex P2 + 4 LOW (incl. Start/Sit '0.0*') | agent | `.worktrees/fi-253-compat` | targeted |

**Work queue (ready, in order; re-ranked when an item finishes):**
1. #253 follow-ups G2–G4, G6–G10 (after phase 2 merges); #254 starts from S1–S3 + G10.
1c. Follow-up: add/drop simulator treats Sleeper FREEAGENT as no-claim-needed.
1d. Tooling: `agent_check.py` backend step hard-coded 300 s (~line 1044) turns large `affected` selections into FAIL with no failures (seen on #258: direct run 756 passed in 6m05s).
1e. Follow-up: waiver OP/FLEX picks compare position percentiles, not points (needs #254 basis); labelled in G5 PR.
1g. Follow-up (security-adjacent, unreachable on self-hosted Next 16.4): `splitLeaguePath` lets `/l/<lg>//evil.example/x` rewrite to a protocol-relative path; reject rest paths starting `//` or containing `\`. Also e2e for forged `x-fi-league` on `/api/search` and `Cache-Control: private` on `trade-evidence/route.ts`.
1h. DONE: part 1 #262 (f35dacc), part 2 #265 (91b361e: LeagueLink everywhere, lint guard, `?league=clear` 307 redirect on scoped URLs, `//host` rejection + proxy same-origin check — also closes 1g's rewrite item). Remaining in `fix/waiver-links-route-errors`: waiver ClaimPlanner/page/FaabGuidance + drop lint exemption + widen guard (review R1). Original note: Link → LeagueLink swap by section + a static guard (ESLint `no-restricted-imports` / no bare `action="/`), which also closes review LOW-2. Deferred items wait on #263 (trades/discover, TradeResult) and #179 (waivers/Evidence).
1l. Follow-up (LOW-MEDIUM, hosted mode): server actions `app/account/actions.ts` (deleteAccount) and `app/leagues/actions.ts` (invite/link, `failed()` via `connectProblem`) return `apiRequest`'s `res.error`, which names API_URL + uvicorn hint when the backend is offline; `BackendProblem` renders the same text by design for local dev — gate it on non-hosted mode. Lint guard can't see URLs pushed via variables or `NextResponse.redirect(new URL(...))`.
1m. Investigate: #267 backend shard 3/8 took 43 min (07:33→08:16) vs ~6 min elsewhere on a frontend-only PR; logs not downloadable here. PR #269 adds 20-min job timeouts, so a repeat now fails fast instead of holding CI — then read that job's log in the Actions UI to find the slow test.
1k. Follow-up (LOW, systemic): league_scope `ResponseCache` has no TTL; time-dependent GETs (`/api/trades/options`, `/api/trades/values`) can keep a kicked-off week in their window until the next data version. Fix: `valid_until` = next scheduled kickoff (pattern in `action_center.py:94-117`) or mark such responses uncacheable; test advancing the clock past kickoff.
1j. DONE #267 (593fd4b: `lib/route-error.ts`, waiver links, lint exemption removed, guard widened). Was (LOW-MEDIUM): `app/api/waiver-claim-plan/route.ts` and `trade-evidence/route.ts` forward offline `api()` errors containing the internal API_URL; #263/#264 fixed their own routes (`compareRouteError` pattern). Unify into one helper.
1i. Follow-up (LOW, pre-existing): `/l/<lg>/<page>?league=clear` is rewritten into the league, but page builders (`perspectiveHref`, `rankingsHref`, …) carry `league=clear` into links, which then leave the league with that league's team id. Fix in proxy: drop `league` from a scoped rewrite's query.
1f. Follow-up: `players/ProfileTabs.tsx:144` and `my-team/value/sections.tsx:452` print `waiver_score` without the partial flag (#260 scope limit).
1a. DONE #261 (af57a57). Was: `/api/search` callers (`CommandPalette.tsx:126`, `compare/ComparePicker.tsx:82`) and `TradeBuilder.tsx:66` `router.push('/trades?…')` lack `/l/<lg>/` (wrong-league display, no exposure). Reuses `leagueRouteHref()` from #182, so it starts after #182 merges.
3. #179 FAAB intelligence (P1). Capability audit first; shares `app/waivers/*` with #180, so not in parallel with it.
4. DONE #183 trade scenarios → #263 (042e229).
5. #254 scoring fidelity (P1). After #253's contract and confirmed gaps; an isolated read-only audit can go earlier.
6. #184 trade-market values → PR #266 (review + Codex P1/2×P2 pending fixes), then Phase 5 (#188, #189, #190, #200).
Blocked or gated: #185, #199, #174, #203 (research-only), #198 (data rights), #222 (after #254), #227 group
prerequisites (see table).


**Wave 1** (parallel, isolated worktrees under `.worktrees/`, all from `5315c96`):
- #169 rules: owns the new `/league-rules` page and endpoint, plus one entry in `lib/navigation.ts`.
- #168 provenance: owns the descriptor contract, a `components/ui` badge and small edits to six pages.
- #202 gates: owns `e2e/`, the Playwright configs, CI E2E steps and `docs/qa/`.
- Lead integration after wave 1: deep links from Start/Sit, Trades and Waivers to Rules; E2E run; PRs.

**Wave 2 (next):** #201 perf (backend cache/single-flight + timing), #167 onboarding (`app/leagues/*`), then #165
weekly hub using the #168 contract. #170 runs after these, alone, because it owns Nav/layout/globals.

## Throughput (D-13 measurements)
Baseline from GitHub timestamps, PRs #230–#252 (22 merged): open→merge mean 33 min, median 29 min; 22 merges in a
5.4 h span (first to last merge). Per-PR split of waiting on CI vs review vs merge was not recorded before D-13.
From now on, log per merged PR: opened, CI green on final head, review done, merged; plus reruns and parallel-caused
conflicts.
| PR | Opened | CI green (final head) | Review done | Merged | Full CI runs | Parallel conflicts |
|---|---|---|---|---|---|---|
| #267 | 07:25 | 08:16 (1e3674e; shard 3 ran 43 min) | 07:29 approve 4 LOW → 3 fixed | 08:16 (51 min) | 2 | 0 |
| #265 | 06:48 | 07:12 (463fa05) | 06:55 approve 4 LOW + Codex P2 → re-check approve 07:09 | 07:13 (25 min) | 2 | 0 |
| #264 | 06:26 | 07:14 (cc431ae) | 06:38 MEDIUM+2 LOW + Codex P2 → re-checks 06:55, 07:00 | 07:15 (49 min) | 4 | 1 (main.py/golden.py vs #263) |
| #263 | 06:12 | 06:39 (2e70562) | 06:20 5 LOW + Codex 3×P2 → re-check approve 06:38 | 06:40 (28 min) | 2 | 0 |
| #262 | 06:08 | 06:36 (0d75df5; e2e budget fix) | 06:19 approve, 3 LOW | 06:36 (28 min) | 3 | 0 |
| #261 | 05:50 | 06:17 (0d64966) | approve (before final CI) | 06:17 (27 min) | 2 | 0 |
| #255 | 04:09 | 04:39 (8cb9ae3) | 04:17 MEDIUM×2 → re-check approve 04:34 | 04:39 (30 min) | 0 | 0 |
| #256 | 04:25 | 05:12 (99c96de, 3rd full CI) | 04:46 MEDIUM → re-check 05:00 → G1 remainder | 05:13 (48 min) | 3 | 0 |
| #259 | 05:12 | 05:18 (46331f9) | 05:23 approve (Codex quota out) | 05:24 (12 min) | 1 | 0 |
| #258 | 04:44 | 05:36 (38f99c6; first CI blocked by conflict) | 05:13 → re-check approve 05:40 | 05:41 (57 min) | 1 | 1 (test_league_scope vs #257) |
| #260 | 05:12 | 05:48 (64718c2) | 05:38 HIGH+2 MEDIUM → re-check approve 06:05 | 06:06 (54 min) | 2 | 0 |
| #257 | 04:30 | 04:36 | 04:34 (approve) | 04:37 (7 min) | 0 | 0 |
| #249 | 02:01 | 03:22 | 02:17 (MEDIUM → fixed by #252, merged 03:07) | 03:22 (81 min) | 0 | 1 (waivers/page.tsx vs #244/#250) |

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
| #247 | c5cc957 | docs only (full CI ~10 min) | green | Codex P1 (ESPN events leak) fixed |
| #250 | 90b8e5f | q a s + kdst e2e | green | security re-check RESOLVED; withLeague hash-drop fixed |
| #248 | 8ce062a | ci_changes/agent_check 104 + actionlint | green, 6m30s | CI 9.5–12 min → 6.5–7.5 min; review HIGH (docs-only skipped credential scan) + MEDIUM (always()) fixed |
| #251 | 3f83350 | docs only | green, 33 s (first live docs-only run) | release-review wording |
| #252 | 69fd25d | quick + lineup suites (657) | green | shared `_best_flex` polynomial, oracle parity 0/100k; review approve; `LineupTooLargeError` → 422 app-wide |
| #244 | 766044f | bye_planner 17 + a 546 + s 627 + fe + bye-planner e2e 6 | green | security review + re-check; Codex clean |
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
- Shared scratchpad: an agent's `rm -rf $S/tmp*/` (#183) also deleted 6 temp folders from other runs. Agents must delete only paths they created, by exact name (D-09); no wildcards in shared dirs.
- CI (#248): the 6–8 min E2E outliers were apt `install-deps`, not the browser download; it now runs only when `ldd` or the Liberation Sans check finds something missing. Backend shards are the critical path now (~6 min).

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
