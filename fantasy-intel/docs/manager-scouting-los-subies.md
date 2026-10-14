# Los Subies manager scouting audit

Checked 2026-10-14 against the existing local league, season 2026, week 6. ESPN snapshot: 2026-10-14 00:53 PDT. All 12 reports built using a read-only connection; no sync or database writes.

Strongest signal is an audit ordering: supported HIGH/LOW classifications first, then stored confidence and absolute z-score. Streaming classifications use absolute starter-rotation thresholds and have no z-score. This ordering is not a new manager score. Current needs below use the stored score multiplied by 100 for display; a largest need can still be small.

| Team | Strongest supported signal (value vs league mean) | Signal confidence / sample | Biggest current need / 100 | Completed trades | Trade behavior confidence |
|---|---|---|---|---:|---|
| Cabbage Patch | Historical RB roster size: HIGH (6.000 vs 4.569) | HIGH / 6 | QB 45.0 | 0 | INSUFFICIENT |
| To Infinity and Bijan | QB add interest: HIGH (0.200 vs 0.101) | HIGH / 10 | D/ST 31.8 | 1 | INSUFFICIENT |
| jc_folk | Historical RB roster size: LOW (3.167 vs 4.569) | HIGH / 6 | D/ST 18.3 | 2 | INSUFFICIENT |
| JOEEE BRRRR | Waiver aggression: LOW (0.500 vs 2.194) | HIGH / 6 | D/ST 38.6 | 1 | INSUFFICIENT |
| Njigbas in Paris | Roster churn: HIGH (2.125 vs 0.823) | HIGH / 6 | QB 1.8 | 5 | LOW |
| Jonnie Blue (you) | Roster churn: HIGH (2.125 vs 0.823) | HIGH / 6 | D/ST 55.0 | 4 | INSUFFICIENT |
| los changitos | Injury replacement: LOW (0.000 vs 0.237) | HIGH / 10 | WR 10.0 | 1 | INSUFFICIENT |
| Tank Dell | RB add interest: HIGH (0.391 vs 0.188) | HIGH / 23 | D/ST 55.0 | 2 | INSUFFICIENT |
| Tortas | WR add interest: HIGH (0.400 vs 0.242) | HIGH / 10 | QB 69.8 | 0 | INSUFFICIENT |
| Carted Off Field | QB add interest: LOW (0.000 vs 0.101) | MEDIUM / 6 | D/ST 43.1 | 2 | INSUFFICIENT |
| Hasbi’s ballers | TE add interest: HIGH (0.263 vs 0.119) | HIGH / 19 | QB 12.7 | 2 | INSUFFICIENT |
| Casa de Pintor | Historical RB roster size: HIGH (6.000 vs 4.569) | HIGH / 6 | RB 23.4 | 0 | INSUFFICIENT |

Trade preferences remain unknown for every manager. The confidence column measures completed-trade sample sufficiency only: <5 insufficient, 5–9 low, 10–19 medium, >=20 high. No trade predictions are produced.

## Required spot checks

- **Jonnie Blue (your team, 6):** 17 current players: QB 2, RB 6, WR 7, TE 1, K 1, D/ST 0; IR 1 is included within those counts. Historical RB average is 6.33; current RB count is 6, keeping history and current construction separate. Highest stored need is D/ST 55.0/100 (zero current D/ST). Four completed trades, eight players sent, six received, two with unequal counts; trade behavior confidence INSUFFICIENT.
- **Most active waiver manager:** Njigbas in Paris (5) and Jonnie Blue tie at 34 observed adds over six weeks (5.67/week vs league mean 2.19). ESPN counters differ: 33 and 31 acquisitions. Both sources are shown independently. Njigbas has five completed trades and LOW trade-history confidence, not a strong preference claim.
- **Fewest moves:** Cabbage Patch (1), one observed add and no observed drops; Casa de Pintor (12) ties in visible metrics. ESPN counters distinguish them (1 vs 2 total add/drop moves). Position add tendencies remain INSUFFICIENT with just one add, even though six weekly snapshots support their historical RB roster-size signal. Both have zero completed trades and INSUFFICIENT trade behavior.
- **Most completed trades:** Njigbas in Paris (5), five deduplicated completed trades, three with unequal player counts. No claim about accepting two-for-one offers is made.

## Data coverage

- League acquisition benchmark: 158 known-position adds across 12 teams. Weighted position shares are distinct from the stored engine's unweighted, eligible-manager comparison used for its classification.
- Every current roster is available for week 6. Natural position counts include IR; missing snapshots are treated as unknown by regression tests.
- Trade facts reuse the existing activity/transaction merger. Visible source coverage may be incomplete; unequal counts describe visible legs only.
- All four required spot checks were inspected, including the two active-waiver co-leaders and zero-trade managers.

## Validation

- Baseline: 213 backend tests passed.
- New tests cover labels, insufficient/missing/stale evidence, composition, ratios and zero denominators, need passthrough, streaming, FAAB, completed trade facts and conservative confidence boundaries, scope, read-only connections and HTTP errors.
- Independent review found stale classification/metric pairing; four regression cases cover missing metrics and mismatched values, samples and timestamps. The active signal becomes INSUFFICIENT while the original stored classification remains inspectable.
- Desktop and 390px browser checks confirmed evidence disclosure, zero-trade state, preserved roster detail, no page overflow and no browser console warnings/errors.
- Final full suite: **242 passed**, including 29 new scouting cases. One baseline Starlette/httpx deprecation warning remains. `npm run lint` and `npm run build` both pass.

## Parallel-merge safety

Branch remains based on `origin/main` commit `39b1a0f60ce6669219e915821cb9c731e739f833`. No rebase onto or merge from the trade branch was performed. At the pre-push comparison, `feature/v2-lineup-trade-evaluator` was `4c7b35e00e035e565da91f19135d425492b06d9a`; the changed-path intersection was empty.

Only two existing files change here: `backend/app/main.py` (router registration) and `frontend/app/managers/[teamId]/page.tsx` (scouting fetch/view and evidence disclosures). All other implementation, types, styles, tests and documentation are new files. Future changes to those two existing files could require reconciliation. This feature leaves schema/connection, lineup/projections/waivers, shared frontend types/styles/API, the large API routes file and all trade UI untouched.
