# Player Rankings & Value Explorer

`/rankings` answers one question: **who are the strongest fantasy options right now, and why?** It is a
scan-first table (cards on phones) of every NFL QB / RB / WR / TE in the league's ESPN pool, ranked by
projected fantasy points, with the position-relative ratings, opportunity, production, consistency, role
trend, availability, matchup and league roster status beside each player. One click opens the existing
player profile (`/players?id=`); the page does not duplicate the profile or the Research Lab.

Code: `backend/app/analytics/rankings.py` (every calculation), `backend/app/analytics/ratings_adapter.py`
(the 0–100.0 public rating adapter), `backend/app/api/rankings.py` (endpoint), `frontend/app/rankings/`
(page), `frontend/lib/rankings.ts` (URL state, formatting; pure, unit-tested). No schema change, no new
dependency, nothing is written.

## Ranking methodology

Two different things are shown and never mixed:

| | What it is | Scale | Used to rank? |
|---|---|---|---|
| **Projected points** | Absolute fantasy points in **league scoring** for the chosen horizon (ESPN by default; Ours opt-in) | points, never rescaled | **Yes**: the overall and position rank |
| **Ratings** (overall, opportunity, production) | Position-relative percentile ratings from `player_scores`, on the public 0–100.0 display scale | 0–100.0 | No (sortable column only) |

* **Rank** = competition rank ("1, 2, 2, 4") of the ranked value, descending, over the whole skill-player
  pool (ALL) and within each position (`WR3`). Players with exactly equal values (to 0.01 pt) share a rank
  and carry `tied: true`. Display order inside a tie: higher overall rating, then name, then ESPN id, so
  the order is deterministic. Ranks are computed **before** the search / roster filters, so a filter never
  renumbers players ("WR12" stays WR12 when you show only free agents).
* **Unranked** players (`rank.overall = null`) sort after every ranked player and say why (`projection.status`):
  `missing` (no projection for the week), `bye` (NFL bye in the week; never treated as a projected 0),
  `incomplete` (rest of season: at least one week in the window has no projection, so no partial total is
  shown), `no_ours` (Ours opted in but there is no independent estimate for him).
* **Weekly** horizon = the league's current scoring period. Value = ESPN's weekly projection
  (`espn_player_weekly_points.projected_points`, ESPN's number unmodified, a projected 0 is real). Bye comes
  from `nfl_schedules`, never from a 0.
* **Rest of season** horizon = the **remaining regular-season weeks** (league `current_week` ..
  last regular-season scoring period), the same window for every player and both sources, so totals are
  comparable. A total is null unless every week in the window is available (ESPN: a projection row, byes
  included as ESPN's own value; Ours: `forward_ours` complete-or-null rule). When the end of the regular
  season can't be derived, ESPN uses the full window (`current .. final_scoring_period`), the response says
  so, and Ours is unavailable. Playoff weeks are not ranked.
* **Source**: ESPN is the default everywhere (the ESPN-first policy). `source=independent` (UI "Ours", also
  the header switch cookie) ranks by Fantasy Intel's production model (ifp-2.0.0) through the same run
  selection as the weekly switch (`forward_ours._select_batch`, bounded queries, no per-player query).
  ESPN's column stays visible. A player without an Ours value is `no_ours`, never silently ranked on ESPN's
  number. Ours rest-of-season (horizon > 0) is **experimental** and labeled so. Ours requires a supported
  league scoring; otherwise it is unavailable with the switch's own reason and ESPN ranks.

## Ratings (0–100.0 public contract, adapter)

The public 0–100.0 rating contract belongs to the separate ratings workstream, which is not merged. This page
builds against an adapter interface (`ratings_adapter.public_rating(row) -> PublicRating`) and does **not**
define any rating formula. Today's adapter maps the existing `player_scores` 0–10 scores linearly:
`round(score × 10, 1)`, clamped to [0, 100], null stays null. When the ratings branch merges, only the adapter
changes.

* `overall` ← `player_scores.value_score` (existing blended value), `opportunity` ← `opportunity_score`
  (position percentile of recent xFP, or targets+carries when xFP is missing: `opportunity_basis`),
  `production` ← `production_score` (position percentile of recent PPR/game).
* Ratings are **position-relative** percentiles: a 90 RB and a 90 WR are both near the top of their own
  position; it is not a calibrated trade price and not comparable to points.
* Production and opportunity are in **full PPR (nflverse)**, not league scoring; projections are in league
  scoring. The response's `scoring` block says this.
* Fewer than 2 games → null rating ("too few games"), shown as "—", never 0.

## Other columns

* **Expected opportunity**: `player_scores.xfp_per_game` (season) and the recent per-game opportunity with its
  basis (`xfp` or `targets+carries`) and absolute tier (HIGH / MEDIUM / LOW).
* **Production**: PPR/game, recent PPR/game, PPR − xFP per game and the sustainability label.
* **Consistency**: weekly PPR standard deviation and games (from `player_week_usage`, played games only, one
  aggregate query). Raw points, not a rating.
* **Role trend**: `player_scores.role_trend` (UP / FLAT / DOWN / INSUFFICIENT).
* **Availability**: `analytics.availability.availability()` from ESPN's designation and the NFL report for the
  ranked week (most severe wins), plus bye.
* **Team / opponent**: the player's NFL team and the ranked week's opponent (home/away) from `nfl_schedules`.
* **Roster**: ESPN `availability_status` → `ROSTERED` (with the fantasy team, `is_mine`), `FREE_AGENT`,
  `WAIVERS`, or `UNKNOWN` when the league context isn't synced.

## API

`GET /api/rankings`

| param | values | default |
|---|---|---|
| `position` | `ALL` `QB` `RB` `WR` `TE` | `ALL` |
| `horizon` | `week` `ros` | `week` |
| `source` | `espn` `independent` | `espn` |
| `sort` | `rank` `name` `espn` `ours` `rating` `opportunity` `production` `xfp` `ppr` `consistency` `trend` | `rank` |
| `dir` | `asc` `desc` | `asc` for rank/name, else `desc` |
| `q` | name search (accent- and case-insensitive substring) | `""` |
| `status` | `all` `rostered` `available` (FREE_AGENT or WAIVERS) | `all` |
| `limit` / `offset` | 1..300 / ≥0 | 100 / 0 |

422 on a bad value; 503 when the league isn't synced (or its current week is unknown). Nulls always sort last
in both directions; ties keep rank order. Query count is constant in the number of players.

Response:

```jsonc
{
  "season": 2026, "week": 7, "horizon": "week", "source": "espn", "position": "ALL",
  "sort": {"key": "rank", "dir": "asc"},
  "window": {"weeks": [7], "first": 7, "last": 7, "label": "Week 7", "regular_season_only": true,
             "last_regular_season_week": 14, "note": null},
  "projection": {
    "basis": "espn",                        // what ranks: "espn" | "ours"
    "label": "ESPN weekly projection, week 7 (league scoring)",
    "espn": {"synced_at": "…", "label": "…"},
    "ours": null | {"available": true, "reason": null, "model_version": "ifp-2.0.0", "model_versions": [...],
                    "run_ids": [...], "run_created_at": "…", "experimental": false, "label": "…",
                    "governance_note": "…"}
  },
  "rating": {"scale": "0-100", "decimals": 1, "contract": "adapter", "adapter": "player_scores 0-10 x 10",
             "semantics": "position-relative percentile; not a trade price; not comparable to points",
             "as_of_week": 6, "computed_at": "…"},
  "scoring": {"projection_units": "league scoring", "rating_units": "full PPR (nflverse)",
              "ours_supported": true, "ours_reason": null},
  "league_context": {"available": true, "league_name": "…", "teams": 10, "my_team_id": 3},
  "freshness": {"espn_projections_synced_at": "…", "player_scores_computed_at": "…",
                "player_scores_as_of_week": 6, "players_synced_at": "…", "ours_run_created_at": null},
  "counts": {"pool": 412, "ranked": 380, "unranked": {"missing": 20, "bye": 12, "incomplete": 0, "no_ours": 0},
             "matched": 412, "returned": 100},     // matched = after position/q/status filters
  "notes": ["…"],
  "players": [{
    "espn_player_id": 1, "gsis_id": "00-…", "full_name": "…", "position": "WR", "pro_team": "KC",
    "rank": {"overall": 3, "position": 1, "position_label": "WR1", "tied": false},
    "projection": {"value": 18.4, "espn": 18.4, "ours": null, "ours_origin": null, "status": "ok",
                   "weeks_expected": 1, "weeks_available": 1, "missing_weeks": [], "bye_weeks": []},
    "rating": {"overall": 87.5, "opportunity": 91.0, "production": 72.3},
    "opportunity": {"basis": "xfp", "xfp_per_game": 15.2, "recent_per_game": 16.1, "tier": "HIGH"},
    "production": {"ppr_per_game": 14.0, "recent_ppr_per_game": 12.9, "fp_minus_xfp": -1.2,
                   "sustainability": "ALIGNED"},
    "consistency": {"ppr_sd": 6.1, "games": 6},
    "games": 6, "role_trend": "UP", "usage_trend_score": 0.4, "market_signal": "BUY",
    "availability": {"status": "AVAILABLE", "weight": 1.0, "sources": [], "espn_injury_status": "ACTIVE",
                     "nfl_report_status": null},
    "game": {"week": 7, "opponent": "BUF", "is_home": true, "on_bye": false},
    "roster": {"status": "ROSTERED", "fantasy_team_id": 3, "fantasy_team": "…", "is_mine": true},
    "why": ["High-volume role (15.2 xFP/g)", "Role trending up", "Production in line with role"]
  }]
}
```

`why` is 1–3 short, data-derived strings (role tier, trend, production vs xFP, availability, bye); never advice
beyond what the data says.

## Frontend

* URL state: `pos`, `horizon` (`week` | `ros`), `source` (`espn` | `ours`; absent = the header switch cookie,
  which defaults to ESPN), `status`, `q`, `sort`, `dir`, `offset`. Every filter is a link or GET form; back /
  forward and sharing work.
* Desktop: table with `SortableTh` (aria-sort) — rank, player (photo, name → profile, team, injury badge), opp,
  projected (ESPN, and Ours when opted in), rating 0–100.0, opportunity, production, consistency, trend,
  roster. Phones (≤ 720px): one card per player with rank, identity, projection, rating, trend and the warning.
* Header: freshness line and the exact projection source; Ours shows the experimental / governance notice
  (`ProjectionNotice`).
* `loading.tsx` skeleton; empty state for no league, no players, a filter with no match.
