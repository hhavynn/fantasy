# K / D/ST streaming baseline

Code:

* `backend/app/analytics/team_lineups.py`: `STREAM_RANK`, `streaming_baseline`, `StreamingProjections`,
  `with_stream`, `stream_summary`, and the waiver streamer inside `project_week`.
* `backend/app/analytics/trades.py`: `with_streaming(state)` (the opt-in), `streaming_enabled`, `assumptions`.
* `backend/app/analytics/trade_discovery.py`: `_Totals` adds the same streamer, so discovery stays bit-identical
  with the manual evaluator.
* `backend/app/analytics/team_value.py`: `build_team_value(..., kdst_streaming=True)`.
* `backend/app/analytics/waiver_data.py` (`streaming_floor`), `waiver_roster.forward_effect(..., stream)` and
  `waivers.build_waivers(con, kdst_streaming=True)`.
* Tests: `backend/tests/test_kdst_streaming.py`.

## 1. The problem

Every season-long number assumed that each team starts the K and D/ST it rosters today, every remaining week.
Real managers stream both positions from free agents. Under that assumption:

* any defense that out-projects yours looked like a large trade upgrade (it was credited its full edge over your
  defense, every week);
* losing your only defense looked like losing the whole slot (an empty D/ST slot for the rest of the season);
* the best "free addition" and the top add/drop on My Team Value were a D/ST held all season, sometimes paired
  with dropping a starting-calibre WR;
* the waiver page's forward comparison credited a D/ST with a whole season of edge over your defense.

[`v3_trade_intelligence.md`](v3_trade_intelligence.md) §7 listed this as cause 4 ("no streaming credit") and kept
K and D/ST out of automatic discovery. The V3.1 streaming frontier showed it only as an illustration.

## 2. The model

For each position the league starts (K, D/ST) and each week, the **streaming baseline** is the ESPN projection of
the **3rd-best available player** at that position (`STREAM_RANK = 3`).

* Available means ESPN FREEAGENT or WAIVERS, not verified NFL-ineligible that week, and projected that week. If
  fewer than three are projected, the weakest projected one sets it. If nobody is projected, or the value is not
  positive, there is no baseline that week. A missing projection is never a 0 streamer.
* Why not the best available? Other managers stream too, waiver order decides who gets the top option, and nobody
  knows every future matchup projection in advance. On the synced league (weeks 6–17) the #3 D/ST is 5.1–6.7 per
  week, close to the median rostered defense. The #1 would beat most rostered defenses outright.

In the weekly optimizer, a **waiver streamer** worth the baseline can fill the K or D/ST slot:

* A rostered K/D/ST starts only when he projects at least that much. On a tie the rostered player starts.
* The streamer uses no roster spot, is never on the bench and is never a starter id.
* In output it is an assignment row with `player_id: null`, `streamer: true`, the baseline as `points` and the
  name "Waiver streamer (D/ST)". `team_lineups.slot_filled` treats it as a filled slot, not an empty one.

Consequences, all from the one optimizer:

* Trading for a K/D/ST is worth only the weeks he beats the stream, by how much he beats it.
* Trading one away costs only the gap down to the stream.
* A free agent K/D/ST added and held all season is worth only what he adds above streaming.
* A team that rosters none can still fill the slot every week.

The streamer is added to every roster alike (it is one fixed extra element). The weekly lineup total therefore
stays monotone and submodular in the roster, and every stage-A bound and the forced-drop branch and bound remain
valid.

## 3. Where it applies (opt-in)

The engine is unchanged by default. Plain `dict` projections give exactly the old results.
`trades.with_streaming(state)` returns a copy of the state whose projections carry the baseline. The ESPN numbers
themselves are not modified.

| Surface | Default | Opt out |
|---|---|---|
| Trade evaluator `POST /api/trades/evaluate` | on | body `"kdst_streaming": false` |
| Trade discovery `GET /api/trades/discover[/summary\|/evidence\|/context]` | on | `?kdst_streaming=false` |
| My Team Value `GET /api/my-team/value` (overview, marginals, upgrades, trade preview, matchup effects) | on | `?kdst_streaming=false` |
| Waiver recommendations (`build_waivers`, run by the analytics pipeline) | on | `build_waivers(con, kdst_streaming=False)` |
| Lineup projection pages, stored `team_*_lineup_projections`, Playoff Race, matchup, Start/Sit | off | — |

Responses carry `kdst_streaming`, which lists per week the free agent who sets the baseline (`enabled: false` when
off). The trade assumptions replace "current roster for every remaining week" with the streaming statement.

**My Team Value matchup effects.** The "before" margins are re-projected from every team's lineup under the same
model, not taken from the Playoff Race's stored projections. Otherwise the streamer would show up as a fake
matchup swing.

**Waivers.** The upcoming week is left out of the waiver baseline. That week's pickup is the streaming decision
itself, and it is still judged by the existing streaming evidence (projection vs. your incumbent, opponent, implied
totals). Only the later weeks of the forward comparison get the floor, so holding a K or D/ST is credited only for
the weeks he beats the stream.

**Bench reasons.** A rostered K/D/ST benched behind the streamer gets the code `streamable` ("a waiver streamer
projects more").

## 4. Real league before / after

Synced league, weeks 6–17, your team:

| | Without baseline | With baseline |
|---|---|---|
| Practical discovery with K/D/ST included: rows moving a K or D/ST | 3 of 20, incl. Michael Wilson → Bills D/ST + Travis Kelce (+34.9) | 1 of 19 (a Bucs D/ST throw-in that costs ≈2 points) |
| My Team Value best free addition | Jets D/ST +20.95 (paired with dropping Stefon Diggs) | Cade Otton (TE) +20.24 |
| Buccaneers D/ST marginal value | +51.98, starts 12 of 12 weeks | +2.0, starts 3 weeks (8 weeks `streamable`) |
| Waiver forward gain, Jets / Lions / Commanders D/ST | +20.95 / +18.78 / +18.58 | out of the top 12; D/STs rank only on this week's stream gain |

## 5. Limitations

* The baseline assumes one free weekly add/drop at K and D/ST. It does not model roster spots used by holding two
  defenses, FAAB or priority spent on streaming, or the pool thinning when you add one of the top three.
* Rank 3 is a stated heuristic, not calibrated against observed streaming results.
* ESPN's future-week D/ST and K projections are matchup-driven and noisy. The baseline uses them unmodified.
