# Trade intelligence audit (for public trade-advice Shorts)

Status: 2026-10-15, nflverse 2026 weeks 1-5. Paths are relative to `fantasy-intel/backend/app/analytics/`.

## Bottom line

- The existing trade stack (`trades`, `trade_discovery`, `trade_practical`, `trade_assets`,
  `trade_alternatives`, `team_value`) is a **private-league engine**. Every module takes a `LeagueState`:
  ESPN rosters, slots, free agents, position limits, and ESPN weekly projections keyed by `espn_player_id`.
  The value currency is **ESPN projected lineup points**. None of it can price a trade for an unknown
  public league without inventing rosters and projections, and the channel rules forbid that currency.
- The usage layer (`usage`, `trends`, `context`) is **nflverse-only** and league-agnostic. It is the
  right foundation.
- `player_scores.market_signal` (BUY/SELL) is nflverse-only but **points-denominated**: PPR/game vs xFP/game.
  It conflicts with the channel rule "argue from usage and role, never fantasy points", so public videos
  should not use it.
- What was missing for public advice: a usage-only cross-position value index, a market-price notion
  (what the other manager will pay), offer construction without rosters, regression flags (touchdowns vs
  role, big-play dependence) and teammate-injury context. The new league-agnostic module `trade_market.py`
  adds these and imports the existing pieces rather than copying them (see "What was added").

## Module by module

| Module | What it actually does | Value currency | League-bound inputs | Reusable league-agnostically? |
|---|---|---|---|---|
| `trades.py` | `evaluate_trade(state, team_a, team_b, a_sends, b_sends)`. Change in each side's optimal starting lineup, summed over remaining weeks: ROS, regular-season and playoff deltas kept separate. Exact forced drops (`choose_forced_drops`), injury return scenarios, sign-based `result_label`. States outright: "manager acceptance is not predicted". | ESPN projected points | `LeagueState` (rosters, slots, capacity, position limits, ESPN projections). `availability` is the only nflverse input. | Only with a synthetic `LeagueState`. `legal_roster` / `choose_forced_drops` are pure roster math. |
| `trade_discovery.py` | `TradeSearch(state, anchor, shapes=("1-for-1","2-for-1","1-for-2"))`. Exhaustive candidate trades: submodular upper-bound screen, exact best-first top-K, beam search for 2-for-2..3-for-3, `simpler_equivalent`. Sorts: `my_gain`, `their_gain`, `combined`, `min_side`. Categories are sign-based (MUTUAL, CONSOLIDATION, ...). | ESPN projected lineup points | Anchor team id, opponent rosters, slots, projections | No (needs an anchor roster). Ranks by lineup-point change and ignores what the other manager would accept: the gap the research summary flagged. |
| `trade_data.py` | `load_league_state` builds `LeagueState` from `leagues`, `lineup_snapshots`, `fantasy_players`, `espn_player_weekly_points`. `kickoff_utc`, `week_kickoff_status`, `evaluation_start_week` decide which weeks are still open. `player_intelligence` joins `player_scores` / `player_context` for display. | n/a (loader) | ESPN everywhere except the kickoff helpers (nflverse `nfl_schedules`) | Kickoff / start-week helpers, yes. The rest, no. |
| `trade_assets.py` | `AssetBoard`: points above replacement (PAR) per player (max(0, ESPN proj - best FA at the position that week)), tie-aware position ranks, `starter_caliber`. `package()` labels balance with `BALANCED_RATIO = 2/3`, `SIGNIFICANT_RATIO = 0.4`, a `*_surrender_multiple_strong` guard and a replacement-level floor. `team_needs` / `league_needs`: below-median "short" and "surplus". | ESPN PAR | FA pool, rosters, slots, projections | The **ratios and guard logic** are unit-free and reusable. `trade_market` imports `BALANCED_RATIO` and `SIGNIFICANT_RATIO`. No star premium: value is linear in PAR. |
| `trade_practical.py` | `discover()` tiers trades: `excluded_one_sided` (a lineup doesn't improve), `excluded_lopsided` (asset label in `AGAINST_THEM`), `speculative`, `concerns`, `practical`. `rationale()` evidence lines, e.g. "A roster need is not a willingness to trade". `trade_type()`: consolidation / depth_for_starter / ... | ESPN points via the above | `TradeSearch` rows, `on_ir`, projections | The rule "both sides must improve; exclude lopsided" is the right policy and is mirrored in `trade_market`. The code itself is bound to its row shapes. |
| `trade_alternatives.py` | Trade vs best free-agent pickup: exact branch-and-bound lineups, "most of the gain is available without trading" majority rule, contested-FA flag, K/DST streaming frontier. | ESPN points | ESPN FA pool and waiver status, roster capacity | No. A public video can't see anyone's waiver wire. |
| `team_value.py` | Team dashboard: league ranks, position strength, roster marginal value, consolidation candidates, slot gaps vs best FA, 1-for-1 previews via `TradeSearch`. | ESPN points | Playoff race data, DuckDB league tables | Only trivial helpers (`_rank`, `_median`). |
| `usage.py` | Builds `player_week_usage`, one row per player-game: snap share, target / air-yard share, WOPR, aDOT, team rush share, backfield share, red-zone and goal-line counts, rushing success, explosive runs, PFR contact, NGS. The only ESPN link is an optional `league_points` LEFT JOIN. | none (usage) | none | **Yes. Primary input to `trade_market`.** |
| `trends.py` | `compute_trend`: recent 2-3 games vs up to 5 baseline games, at least 4 games. `composite_trend` averages each metric's delta/threshold. `TREND_METRICS` per position. | none, but `TREND_METRICS` includes `xfp` | none | **Yes.** `trade_market` reuses it and drops `xfp`, which is points-denominated. |
| `player_scores.py` | Opportunity score (xFP percentile), production score (PPR/game percentile), opportunity tier by absolute PPR thresholds, `value_gap` PPR vs xFP, `market_signal` BUY/SELL/HOLD, `value_score = 0.75 opp + 0.25 prod + trend`. | **PPR and xFP points** | none (ESPN id attached at the end) | `percentile_scores`, `played`, `RECENT_GAMES`, `MIN_GAMES_CLASSIFY` are reused. The signal is not reused (points currency). Its conservative policy, "a high-volume role is never a SELL", is mirrored. |
| `context.py` | Implied team totals (sign verified), team PROE / neutral pass rate, season-to-date defense ranks (`rank_generous`), `matchup_label`, player context labels. | none | `leagues.current_week` only, to choose next week (falls back to nflverse) | **Yes.** `trade_market` reuses `matchup_label` for the 4-week schedule. |
| `schedule_intelligence.py` | Fantasy-team schedule view. Its defense-allowed ranks without lookahead are nflverse-based. | ESPN-bound (`NotSynced` without a league) | `fantasy_teams`, `lineup_snapshots` | Ranking logic only; the entry point needs a league. |
| `participation.py` | `ahead_unavailable` / `ahead_questionable` teammate flags, a feature in the play-probability model. | n/a | nflverse | The concept, yes. `trade_market` implements a narrower "vacated role" check on share metrics. |

Projection engine (`projections.py`, `independent_projections.py`, ...) and the trade evaluator were
**not modified**; `trade_market` doesn't import them.

## Metrics: verification status (per `docs/nfl_metrics_verified_contract.md`)

The contract says every rule below was checked against upstream nflverse **and** the 2026 data
(weeks 1-5), except sections it marks unresolved.

| Metric | Status | Definition (contract) | Used by `trade_market` |
|---|---|---|---|
| Snap share | Verified source (PFR `offense_pct`); team snaps derived and validated (section 5) | `offense_snaps / team snaps` | Role (RB 0.20, WR 0.15, TE 0.25); also partial-game detection |
| Target share | Verified (section 3) | targets / team targets (not pass attempts) | Role |
| Air-yard share | Verified (section 3) | receiving air yards / team air yards | Role (WR) |
| WOPR | Verified (section 3) | 1.5 x target share + 0.7 x air-yard share | Role (WR, TE) |
| Backfield share (RB) | Verified (section 2) | designed rushes / team RB designed rushes | Role (RB 0.30); vacated-role check |
| Team rush share | Verified (section 2) | rush plays incl. scrambles / team rush plays | Trend only (via `TREND_METRICS`) |
| Red-zone carries / targets | Verified counts (section 2) | designed rushes / targets at yardline <= 20 | Role as `rz_opp_share` = (rz carries + rz targets) / (team rz carries + team rz targets): a **derived ratio** of two verified counts |
| Explosive run rate | Verified (section 2) | designed rushes of 10+ yards / designed rushes | Big-play-dependence flag (RB) |
| aDOT | Verified (section 3) | air yards / targets | Not used |
| Implied team total | Verified sign (section 5) | home (total + spread)/2, away (total - spread)/2 | Team context |
| Neutral pass rate | Verified (section 5) | mean(pass) with 0.20 <= wp <= 0.80, > 2 min left in half | Team context (WR/TE) |
| Defense generosity ranks | Verified (section 6) | season-to-date EPA, RB/TE target share allowed, ranks 1-32 | Schedule (via `context.matchup_label`) |
| Injuries | Contract section 8: only the **current scoring week's** report affects availability; tie-break Out > Doubtful > Questionable | | Availability uses the current-week report plus reserve lists; last week's Out is a risk flag only |
| TD rate | **No hardcoded TD rate** anywhere (contract) | | Not hardcoded: `TD_HEAVY` / `TD_LIGHT` compare a player's TDs with his red-zone opportunities times the **empirical position conversion rate from the same data** |
| FTN read codes | **UNVERIFIED**, experimental only | | Not used |
| Routes, route participation, TPRR, YPRR, alignment, coverage | **Unavailable in season** (section 9); external imports are display-only | | Not used |
| xFP (`nfl_ff_opportunity`) | nflverse model output, points-denominated | | Not used (channel rule) |
| Fantasy points / PPR / league points | Present in `player_week_usage` | | **Never read.** A test asserts values don't change when these columns change. |

## What was missing for public trade advice, and what is still missing

| Need | Before | Now (`trade_market.py`) | Still missing |
|---|---|---|---|
| League-agnostic value | Only ESPN PAR / lineup points | `usage_value` 0-100: role indexed to the position's startable benchmark, small trend / context / schedule multipliers, convex rank curve against a neutral 12-team pool (RB 30, WR 30, TE 12) | QBs (out of scope: weak usage signal, 1QB trades are rare), superflex, dynasty |
| Market price (what they'd accept) | None: `trade_discovery` ignores it | `market_value` proxy: half usage, half box score (scrimmage yards/game 0.6, TDs/game 0.4; never fantasy points). Pluggable `adp_ranks` and `sleeper_market_ranks()` (free Sleeper `/players/nfl` `search_rank`) | **A real public price.** The Sleeper API is blocked from the cloud session (proxy 403), so the adapter is unit-tested but not run on live data. The box-score proxy underrates stars in a quiet stretch; a reputation guard keeps elite roles out of sell-mode returns. |
| Buy-low / sell-high | `player_scores` signal (points) | Usage vs market gap, regression flags (`TD_HEAVY`, `TD_LIGHT`, `EXPLOSIVE_DEPENDENT`), role trend, vacated teammate role, partial-game and injury handling, elite-role hold rule | Outcome tracking (the recommendation ledger) |
| Offers | Roster-bound trade search | `generate_offer(players, target, mode)`: opening 1-for-1 plus up to two 2-for-1s; qualitative fairness (`opening offer` / `fair range` / `likely high price`); lopsided either way is excluded; must win on usage; position-need and roster-spot checks; starter-caliber anchor (`BALANCED_RATIO`) and no-scraps floor (`SIGNIFICANT_RATIO`); incentive and risk lines | Per-league position need (impossible without rosters; we state the need instead, e.g. "needs TE depth") |
| Evidence for QC | n/a | Every cited number is an `Evidence(gsis_id, metric, weeks, value, text)`; `derive_metric` recomputes it from `player_week_usage` | Studio claim kinds in `studio/stages/facts.py` (see `handoff.md`) |
| Private data isolation | n/a | The loader reads only nflverse-derived tables; a test runs it on a DB with no ESPN league | — |

## Channel-rule notes

- Points: the usage index never reads points, xFP or projections. The **market** proxy uses yards and
  touchdowns, which are box-score stats, not fantasy points; you confirmed usage-only value with a free
  public price. Until a public price is wired in, the yards/TD proxy stands in for it. Treat that as the
  weakest link and replace it with Sleeper ranks when run from a machine that can reach the API.
- Faceless: unchanged for now. Revisit an on-screen host after the two renders.
