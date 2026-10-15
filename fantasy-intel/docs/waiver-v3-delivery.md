# Waiver Intelligence V3 — delivery and validation

Base: `6b3993e2448533b98bcf46d9d151cba2b117734f` (fetched origin/main). Branch: `codex/waiver-intelligence-v3`. Independent managed worktree; no changes to the user's original checkout. No automatic merge.

## Confirmed original problems

The audit in `waiver-v3-audit.md` preceded implementation. Drops were selected before checking capacity; null drop IDs appeared as “open spot”; positional caps were ignored; unknown drop values became zero. CLAIM relied on a blended score rather than a complete transaction. The ownership floor and first-300 global cap could hide emerging players. Own priority/FAAB did not determine action advice. K/D/ST lacked a distinct forward streaming comparison.

## Decision methodology

The player scoring model, classifications and shared injury weights are unchanged. Existing opportunity/fit/waiver scores remain compatibility diagnostics. V3 recommendations use separate evidence:

1. **Player quality:** existing historical opportunity/production, role trend, stored forward ESPN projections and context headlines. Unknown value stays unknown in decision comparisons.
2. **Roster benefit:** compare optimal current and fully comparable forward starting lineups after the actual add/drop. Positional need and observed depth support stash advice. A missing projection is never zero; a supplied zero is real. Future lineups use unmodified ESPN points and known byes, with IR excluded; no invented recovery dates or injury probabilities.
3. **Move feasibility and sacrifice:** active capacity, positional caps, IR eligibility, snapshot freshness, explicit lock/undroppable flags when supplied, current/future starting-slot coverage. Consider every active single-player drop; report rejected choices and defensible alternatives. Rank choices by a same-position short-term stream replacement, projected net roster effect, historical net roster effect, removal loss, future starts, redundancy and value. Preserve useful skill depth over redundant streaming assets when other effects tie.
4. **Resources:** distinguish FREEAGENT, priority claims, FAAB and unknown rules. Rolling top-three priority warrants preservation on marginal gains or comparable feasible free agents. Try the next ranked alternative if one cannot support a legal move. Unknown FAAB spending is unknown, not an untouched budget. An exhausted budget requires verifying zero-dollar claim rules. No precise bids or claim-success probabilities.

### Explicit heuristic thresholds

These are decision aids, not calibrated probabilities: historical starter gain ≥1 on the existing 0–10 value scale; fully comparable mean forward gain ≥1 ESPN point/week; stash value ≥6 and same-position depth improvement ≥1 with need ≥0.35 or an observed rising role. Strong but unproven value ≥4/opportunity ≥6 remains MONITOR. Comparable alternatives are within 0.5 current projection/value and 0.5 final roster utility, with legal/fresh/healthy evidence. Rolling priority ranks 1–3 receive preservation diagnostics.

Internal ordering uses max(positive historical lineup gain, complete forward gain/week), plus 2×need for meaningful moves and 1 for a stash; stream gain can supply the ranking benefit. This mixes heuristic evidence scales and is not displayed as definitive player value. Existing competition remains a coarse behavioral bucket, never a forecast.

The freshness boundary is 72 hours: dated current-week roster and candidate details are required for affirmative advice; stale/undated future player-week projections are excluded. This is a conservative refresh policy, not an injury model. Every recommendation exposes limitations.

## Roster/drop outcomes

| Outcome | Meaning |
|---|---|
| ADD_WITHOUT_DROP | Known current capacity and caps permit the add; no drop suggested |
| ADD_WITH_DROP | A defensible single drop makes the transaction feasible |
| MOVE_REQUIRED | A move is required, but no defensible legal drop is established |
| UNKNOWN | Incomplete/stale settings or roster, unsupported slots, or unresolved IR eligibility |
| INCOMPATIBLE | No single active drop satisfies known restrictions |

IR occupants do not consume active capacity. Positional caps conservatively count all rostered players, including IR; ESPN's exact application should be confirmed for unusual leagues. Provider nonpositive positionLimits retain the existing repository convention of “unlimited.” Existing OUT/IR/injury-reserve placements are accepted; healthy, questionable, doubtful or unknown-status IR occupants force verification. No null drop ID implies an opening. A beneficial transaction cannot hide a newly unfilled future starting slot behind a large season-total gain.

## D/ST and kicker streaming

Use the earliest full actionable scoring week. Compare stored league-scoring projected points against the weakest required incumbent starter, respecting multiple K/D/ST starters. Require opponent/schedule and comparable incumbent evidence. Show schedule, other available projected options, implied team totals, stored opponent offense/defense context, timestamps and missing data. Lines/context are supporting facts, never converted into invented points.

A ≥1 upcoming-point improvement is a short-term streamer; improvement in at least two of the next three supplied matchup/projection comparisons is a multiweek hold candidate. No league starting requirement → unnecessary stash; no demonstrated improvement → pass; missing evidence → incomplete/monitor. A short-term same-position swap may sacrifice full-horizon points only with that sacrifice explicitly explained. Prefer replacing the incumbent streamer over dropping useful RB/WR depth. An open roster still produces no required drop; adding a second streamer is a short-term scenario, not an endorsement to hold it all season.

## Candidate coverage and performance design

No ownership minimum. Evaluate up to 60 per position: 15 each from forward projection, depth-chart role, historical value and ownership, filling duplicate places from the next candidate. Max 360 detailed evaluations. Alternatives come from that evaluated pool and share a per-build move cache. Report eligible/evaluated/cap exclusions by position and stored roster conflicts. Coverage is only the locally stored ESPN pool; no claim of a complete NFL universe or immunity from upstream sync truncation.

All data access is batched during analytics. Page/API reads use stored results. No new ESPN/NFL requests during rendering, schema migration, persistent cache, or repeated roster simulation per page request.

## Reproducible quality examples

Run `.venv/bin/python ../scripts/waiver_examples.py` from backend. These are synthetic examples, not current player recommendations.

| Scenario | Old weakness | V3 result |
|---|---|---|
| Rising RB, full roster, useful backup RB | Lowest bench value lacks transaction/coverage explanation | Claim/stash; drop surplus WR, preserve backup RB and explain sacrifice |
| Strong add with open capacity and an expendable bench player | Automatic suggested drop despite an opening | ADD_NOW as a free agent; no drop, explicit active count/capacity |
| #1 rolling priority, +0.4 historical gain | Opportunity 9 could dominate the ranking | MONITOR, retain priority for a clearer upgrade |
| D/ST projected 10 vs incumbent 6, opponent NYJ, implied total 16.5 | Generic historical percentile/depth logic | +4 upcoming points; short-term free add replacing current D/ST |
| 95%-owned WR behind value-9/value-8 depth | Prominence does not establish roster improvement | NOT_AN_UPGRADE |
| 0.1%-owned rising RB / projection-only rookie | Ownership floor and global cap can exclude emerging roles | Eligible through role/projection lanes; add if utility and legality support it |

Additional regressions cover preserving a value-6 backup RB over a redundant value-6.1 D/ST, projection-only starter replacements, bye coverage, unknown incumbent/drop quality, stale injury tokens, exhausted FAAB, and trying a second viable free-agent alternative.

## Validation and review

Final commands/results:

- Backend `.venv/bin/python -m pytest -q --tb=short`: **496 passed, 1 skipped, 3 pre-existing failures** in 55.38s. Existing Starlette/httpx deprecation warning.
- Focused waiver/pipeline/API run: **71 passed**.
- Frontend `npm run lint`, `npm run build`: pass.
- `git diff --check`: pass.
- Six asserted examples: `scripts/waiver_examples.py`: pass.

`scripts/benchmark_waivers.py` seeds full 16-player rosters, 900 available players, all 32 NFL teams, synthetic schedules/byes/game lines, and 11 future projection weeks. One measured run per league size; timings are local wall-clock observations, not service guarantees. API times include TestClient startup and JSON serialization. Fixture construction is excluded.

| Teams | Rostered | Eligible / evaluated | Analytics build | Stored-results API |
|---|---:|---:|---:|---:|
| 10 | 160 | 900 / 360 | 6.390s | 108.79ms |
| 12 | 192 | 900 / 360 | 7.277s | 79.14ms |
| 14 | 224 | 900 / 360 | 6.173s | 79.02ms |

Each run reports 540 cap exclusions. Adding the validated alternative checks initially doubled rebuild time; a per-build roster-plan cache removed repeated candidate/alternative simulations. Page reads perform no rebuild.

Full backend pytest was run on the isolated branch. Three failures already reproduced on untouched origin/main remain in projection backtesting: `test_exact_kickoff_and_post_start_predictions_are_excluded[0]`, `[-1]`, and `test_valid_earlier_snapshot_survives_later_postgame_snapshot`. Their expected results are 0/0/4 but existing results are 1/1/86. This PR does not modify that unrelated module or weaken those tests.

Two existing assertions were intentionally updated to match the requested stronger behavior: an open roster no longer recommends dropping player 13; forward projections can now change waiver decisions, while player scores/market signals, usage, needs and manager metrics retain exact isolation assertions. All trade tests remain in the full suite. No protected trade files or shared availability/scoring logic changed.

Frontend lint and production build passed (Next.js 16.4.0). Browser checks used a synthetic fixture DB: empty-state presentation, ALL view, evidence expansion, player/manager links, explicit open capacity, and 390px phone layout (350px recommendation rows, no row overflow). No live ESPN mutation was attempted.

Independent review found IR eligibility, stale roster/projection evidence and free-agent comparison gaps. Each was reproduced and fixed with regression tests. A final additional finding—trying another alternative after the first fails—was also reproduced and fixed. GitHub Codex review of c63939d returned two findings: accept existing OUT placements in IR, and tolerate malformed kickoff values. Both were reproduced RED→GREEN and addressed. Malformed/missing kickoff timing now yields warnings and uncertainty rather than aborting analytics.

## Files and integration boundary

- `backend/app/analytics/waiver_roster.py`: pure constraints, legality/drop planning and forward roster effects.
- `backend/app/analytics/waiver_decisions.py`: benefit, resource, streaming and action decisions.
- `backend/app/analytics/waiver_data.py`: batched stored-data adapter, evidence lanes, freshness and free-agent candidates.
- `backend/app/analytics/waivers.py`: existing loader/diagnostics integrated with V3; competitor unknown-budget handling.
- `backend/app/api/routes.py`: waiver endpoint only; utility ordering, filtered views, pagination and coverage.
- `frontend/app/waivers/{page.tsx,types.ts,waivers.module.css}`: compact decision list, local types/styles, progressive disclosures, view/position filters and pagination.
- `backend/tests/{test_waiver_decisions.py,test_waiver_data.py,test_waivers.py,test_api.py,test_espn_projections.py}`: behavioral and pipeline/API regressions.
- `scripts/{benchmark_waivers.py,waiver_examples.py}`; audit, delivery report and implementation ledger.

Claude's Trade Intelligence work can merge independently: no changes to trades.py, trade_discovery.py, team_lineups.py, trade_data.py, availability.py, player_scores.py, lineup.py, projections.py, schema, frontend/lib/types.ts or frontend/lib/api.ts. The sole shared-file integration is the isolated `/waivers` handler in routes.py; resolve that region if another PR changes the same handler. Versioned `components.decision` requires analytics refresh; old rows show refresh guidance. A later integration may consolidate adapters only after agreeing on acquisition/IR/lock settings semantics.

## Limitations

Advice is snapshot-based and read-only. ESPN live acceptance, player lock state, undocumented waiver enums, acquisition timing, unusual IR eligibility and external undroppable-list membership cannot be guaranteed from current storage. Unknown raw restrictions or enabled undroppable lists suppress unsupported advice. Current-period comparisons conservatively begin next week once its first stored kickoff has occurred; partial-week optimization is not attempted. No FAAB bidding calibration, fictional injury recovery, alternative future roster transactions or multi-claim queue optimization is modeled. Historical scores remain PPR-based in non-PPR leagues and are labeled; ESPN projections remain league scoring. Missing NFL/forecast data can leave no actionable recommendation, which is preferable to manufacturing one.
