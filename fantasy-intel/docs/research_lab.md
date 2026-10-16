# Fantasy Research Lab

`/research` (overview), `/research/wr`, `/research/rb`, `/research/te`. Read-only research over the whole NFL
WR / RB / TE pool, built on `player_week_usage` and `player_scores`. Code: `backend/app/analytics/research.py`
(every calculation), `backend/app/api/research.py` (endpoints), `frontend/app/research/` (pages),
`frontend/lib/research.ts` (URL state, configs, formatting). No schema change and no new dependency.

## Data audit (what the lab uses)

| Metric | Source | Status |
|---|---|---|
| xFP | `player_week_usage.xfp` (nflverse ffopportunity `total_fantasy_points_exp`, full PPR) | used as is |
| Actual PPR | `player_week_usage.fantasy_points_ppr` (nflverse) | used as is; ESPN projections are never used |
| Targets, receptions, air yards, carries, designed rushes, RZ/GL work, snaps | `player_week_usage` | used as is |
| Team targets / air yards | `nfl_weekly_stats` summed per team-week (fallback: skill-player usage rows, reported as `team_totals_source`) | derived |
| Target share, air-yard share, WOPR, aDOT over a window | Σ numerator / Σ denominator over the window's games | derived |
| Backfield share, team rush share over a window | Σ designed rushes / Σ team RB designed rushes; Σ rush plays / Σ team rush plays | derived |
| xFP/G, PPR/G, FPOE/G per window | paired qualifying games | derived |
| Momentum | `trends.compute_trend` (existing windows) on qualifying games | reused |
| Opportunity tier, role trend, market signal, value-gap label | `player_scores` | reused, displayed with caveats |
| Value-gap margin, high-opportunity threshold | `player_scores.GAP_ABS/GAP_REL`, `TIER_THRESHOLDS` | reused |
| Injury | `nfl_injuries` latest report, ESPN status via a verified ID map | reused |
| Byes vs missed games | `nfl_schedules` | derived per week |
| Routes, route participation, TPRR, YPRR, alignment | only an authorized external import (`routes` column) | never estimated; not charted in this release |
| FTN read codes | unverified | not used |
| ADP, trade values, acceptance odds | not stored | not shown |

## Definitions

- **Qualifying game**: the player played (offensive snap, target or carry; the same test as
  `player_scores.played`) and the game has both nflverse xFP and PPR points. A played game with no xFP
  (ffopportunity lags the box score) is excluded from every average and listed under `excluded_games`.
- **Windows**: `season` = every qualifying game; `last3` / `last5` = the player's 3 / 5 most recent
  qualifying games (player games, not calendar weeks; the weeks used are returned).
- **xFP/G, PPR/G** = sums over the window's games / number of games. **FPOE/G** = mean of (PPR − xFP)
  over the same games (equal to PPR/G − xFP/G only because both use one game set).
- **Per-game counts** divide by the games where the count is known; `coverage` reports a metric whose
  source covered fewer games than the window. Missing denominators give `null`, never 0%.
- **Snap share** is the mean of weekly offense_pct (team snap totals can be rejected as NULL).
- **Groups** (descriptive): high opportunity = window xFP/G ≥ the existing HIGH tier (WR/RB 12, TE 9);
  production above / below / in line uses `value_gap` (margin max(2.5, 20% of xFP) per game, 3+ games).
- **Research lists**: underperforming opportunity leaders (high opportunity, below expectation, role
  trend not down); emerging role increases (momentum xFP label UP, or existing role trend UP with a
  measurable sample); production ahead of measured opportunity (above expectation, below the high line).
  These are prompts to investigate, separate from the market signal and never a trade call.
- **Momentum confidence**: HIGH = 3 recent vs 4+ baseline games, MEDIUM = 3 vs 3, LOW = 2 recent (4–5
  games), INSUFFICIENT under 4. Sample coverage, not statistical certainty.
- **Week states** (player timeline): played, played without published xFP, no offensive snaps, bye,
  did not play (with the NFL report status when one exists), unknown (no schedule).

## Default pool filters

Volume is a per-game average over the window.

| Position | Default | Why |
|---|---|---|
| WR | 2+ games, 4+ targets/G | keeps emerging receivers visible; the reference video's 7+ targets is one select away |
| TE | 2+ games, 3+ targets/G | tight ends see fewer targets; a blocking TE has no fantasy question |
| RB | 2+ games, 8+ carries+targets/G | keeps committee backs, drops change-of-pace and special-teams backs |

`min_games` above the window size (e.g. 4 with `last3`) is rejected with 422.

## Endpoints

- `GET /api/research/opportunity?position=WR|RB|TE&window=season|last3|last5&season=&min_games=&min_volume=&team=`
  → filters (resolved), defaults, data freshness (latest week, xFP week, weeks missing xFP, sync times),
  pool counts with exclusion reasons, and one row per player: window summary, group, momentum, market,
  injury, research groups, position percentiles (0–100 within the shown pool; missing values unranked).
- `GET /api/research/players/{gsis_id}?season=` → season / last3 / last5 windows, momentum, weekly
  timeline with week states, and league context (availability, owner team) only to build trade / waiver
  links. 404 when the player has no WR/RB/TE usage.

Two to five queries per request regardless of pool size (no per-player queries). ESPN ids link only
through a high-confidence `player_id_map` match; two ESPN ids for one gsis id link to neither.

## Not in this release

Routes-based TE comparison (needs an external route import), per-league custom scoring, multi-season
history, and saved player watchlists.
