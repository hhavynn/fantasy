# Player Comparison Lab (`/compare`)

Two NFL fantasy players side by side, built only from data the app already verifies: ESPN's weekly
projections (`player_forward`), the opt-in Fantasy Intel series (`forward_ours`), nflverse weekly usage
(`player_week_usage`), `player_scores` (ratings, trend, role evidence), ESPN weekly league points and the
latest injury designations. **No new forecasting engine**: nothing here estimates points; it only reads,
aligns and compares stored numbers.

## Endpoint

`GET /api/compare?a=<espn_player_id>&b=<espn_player_id>&source=espn|independent`

* `a`, `b`: ESPN player ids (D/ST ids are negative). Non-integers → 422. Unknown id → 404 with
  `detail = "Unknown ESPN player id <id> (a)"` (or `(b)`); `a` is checked first.
* `source`: `espn` (default) or `independent`, validated with `forward_ours.normalize` → 422 otherwise.
  ESPN stays the default; `independent` only adds the Ours fields.
* `a == b` is allowed: 200 with `identical: true`, both cards equal, every row `edge: "n/a"` and one summary
  finding saying the same player was picked twice.
* No league synced → 503 (same as every endpoint, via `_league`).

### Leakage rule (pregame only)

`as_of_week` = league `current_week` (the first unplayed fantasy week); `completed_through_week = as_of_week - 1`.
Every historical number (usage, league points per game, weekly series, recent games, stability) uses only
weeks `<= completed_through_week`, for **both** players, even when one of them already played this week
(Thursday game). Projections are the stored pregame projections (`projected_points`), never actuals.
ESPN's `season_avg_points` is **not** used (it can include the in-progress week). When `player_scores.as_of_week
>= as_of_week` (ratings/trend computed after a game this week) a limitation says so.

### Response

```jsonc
{
  "as_of_week": 7, "completed_through_week": 6, "final_week": 17, "last_regular_season_week": 14,
  "source": "espn",                // or "independent"
  "identical": false, "same_position": true,
  "scoring": {"scoring_type": "PPR", "reception_points": 1.0, "settings_synced": true,
              "label": "PPR (1 pt per reception)", "note": "..."},
  "players": [Card, Card],          // [a, b]
  "rows": [Row, ...],               // compact advantages table, fixed order (below)
  "summary": [Finding, ...],        // fixed keys, fixed order (below)
  "limitations": ["..."],
  "disclaimer": "Research comparison, not a start/sit or trade verdict. ...",
  "freshness": {"espn_synced_at": iso|null, "nfl_synced_at": iso|null}
}
```

`scoring.label`: from `leagues.reception_points` — 1 → "PPR (1 pt per reception)", 0.5 → "Half PPR (0.5 pt per
reception)", 0 → "Standard (no reception points)", other → "<x> pt per reception". `settings_synced` is false when
the league row has no `scoring_items` (or `reception_points` is NULL); then `label = "League scoring (settings not
synced)"` and the note says ESPN league points are still ESPN's own numbers in league scoring, while nflverse
PPR/g is always full PPR. Nothing fails because scoring settings are missing.

**Card**

```jsonc
{
  "player": {"espn_player_id", "full_name", "position", "pro_team", "fantasy_team", "availability_status",
             "percent_owned", "gsis_id", "match_method", "match_confidence"},
  "projection": {                   // null when the league window is unknown (limitation says why)
    "next_week": {"week", "on_bye": bool|null, "opponent", "is_home",
                  "espn": float|null,                         // ESPN projected_points for as_of_week
                  "ours": float|null,                         // only origin ours|ours_zero for as_of_week
                  "ours_origin": "ours"|"ours_zero"|"espn"|"bye"|null,   // null when source=espn
                  "ours_horizon": int|null},
    "ros": {"espn": float|null, "espn_weeks_available", "espn_weeks_expected",
            "ours": float|null,                               // ours.ros_projected_points (null unless complete)
            "ours_available": bool|null,                      // null when source=espn
            "ours_reason": str|null, "ours_weeks_available": int|null, "ours_weeks_expected": int|null,
            "ours_experimental": true},
    "bye_weeks": [int], "bye_weeks_remaining": int|null,
    "games_remaining": int|null,      // remaining fantasy weeks with an NFL game; null if any week's schedule unknown
    "weeks": [{"week", "opponent", "is_home", "on_bye", "phase", "espn": float|null,
               "ours": float|null, "ours_origin": str|null}],
    "projection_synced_at": iso|null
  },
  "usage": {
    "games": int, "weeks": [int],      // completed games with a usage record (played, week <= completed_through)
    "metrics": {
      "<key>": {"value": float|null, "applicable": bool, "why": str|null}
    },
    "trend": {"label": str|null, "score": float|null},          // player_scores trend_label / usage_trend_score (-1..+1)
    "ratings": {
      "opportunity": {"value": float, "scale": 100|10, "legacy": bool} | null,
      "production":  {"value": float, "scale": 100|10, "legacy": bool} | null,
      "basis": str|null, "version": str|null, "as_of_week": int|null
    },
    "stability": {"cv": float|null, "games": int, "metric": "xfp"|"opportunities"|null}
  },
  "weekly": [{"week", "opponent", "league_points", "ppr", "xfp", "snap_share", "carries", "targets"}],
  "availability": {
    "espn_status": str|null, "has_designation": bool,
    "nfl_report": {"week", "report_status", "report_primary_injury", "practice_status"} | null,
    "report_is_current": bool|null,   // report week == as_of_week
    "label": str,                     // "Questionable (NFL report, week 7)", "Out (ESPN)", "No designation reported"
    "note": str                       // with no designation: "No injury designation is listed. That is not
                                      //  confirmation he will play; reports can lag."
  },
  "role": {"depth_chart": {"pos_abb", "pos_rank", "dt", "team"}|null, "role_trend": str|null,
           "opportunity_tier": str|null,
           "evidence": [{"metric", "label", "baseline_avg", "recent_avg", "delta", "unit": "pct"|"per_game",
                         "baseline_weeks", "recent_weeks"}]},   // player_scores trends with label UP or DOWN
  "recent_games": [ ...last 4 entries of weekly, newest first... ]
}
```

**Usage metric keys and position validity** (`applicable=false` rows carry `why`, `value=null`):

| key | meaning | QB | RB | WR/TE | K, D/ST |
|---|---|---|---|---|---|
| `snap_share` | mean weekly offense_pct (0–1) | ✓ | ✓ | ✓ | – |
| `carries_pg` | rush attempts / game (QB scrambles included) | ✓ | ✓ | – | – |
| `targets_pg` | targets / game | – | ✓ | ✓ | – |
| `target_share` | mean weekly target_share (0–1) | – | ✓ | ✓ | – |
| `air_yards_pg` | air yards / game | – | – | ✓ | – |
| `xfp_pg` | nflverse expected PPR / game | ✓ | ✓ | ✓ | – |
| `ppr_pg` | nflverse PPR points / game | ✓ | ✓ | ✓ | – |
| `league_pts_pg` | ESPN league points / week with an ESPN score, bye weeks excluded | ✓ | ✓ | ✓ | ✓ |

QB `why` for receiving rows: "Receiving usage is not a QB role measure; passing volume (dropbacks) is not in the
usage data." RB `air_yards_pg`: "Air yards describe downfield receiving roles; not meaningful for a running back."
WR/TE `carries_pg`: "Carries are not a receiver role measure." K/DST: "No NFL usage data for kickers and team defenses."
Applicable but unavailable (no usage match / no games) → `value=null`, `why="No usage record"`.

**Row** (`rows`, fixed order): `next_week_espn`, `next_week_ours` (only when `source=independent`), `ros_espn`,
`ros_ours` (only when independent; note "Experimental beyond next week"), `games_remaining`, then each usage key in the
table order, then `trend_score`, `opportunity_rating`, `production_rating`.

```jsonc
{"key", "label", "group": "projection"|"usage"|"rating",
 "unit": "pts"|"pts_per_game"|"per_game"|"yards_per_game"|"share"|"games"|"trend"|"rating",
 "a": float|null, "b": float|null, "edge": "a"|"b"|"even"|"n/a", "note": str|null}
```

Edges are only claimed past a materiality threshold; otherwise `even`. `n/a` when either side is null, not
applicable, identical players, or not comparable:

| unit | threshold |
|---|---|
| `pts` next week | 0.5 |
| `pts` ROS | max(3.0, 3% of the larger) |
| `per_game`, `pts_per_game`, `yards_per_game` | 0.5 (air yards: 5.0) |
| `share` | 0.02 |
| `games` | 1 |
| `trend` | 0.15 |
| `rating` | 5.0 |

* Usage rows need ≥3 games for both players, else `n/a` with note "Fewer than 3 games for <name>".
* Rating rows: `n/a` across positions (note "Position-relative ratings: a WR 80 and an RB 80 are each 80th
  percentile of their own position, not equal value"), when either is a legacy 0–10 value (never compared with a
  0–100 one), or when the opportunity bases differ.
* Higher is better for every row (games remaining included).

**Finding** (`summary`, keys in this order): `next_week`, `stability`, `ros`, `trend`.

```jsonc
{"key", "title", "leader": "a"|"b"|"even"|null, "text": str, "basis": str}
```

* `next_week` "Higher current projected points" — ESPN next-week projection (Ours mentioned too when both have an
  Ours number). Bye/missing projections are stated, never treated as 0.
* `stability` "More stable opportunity profile" — coefficient of variation of weekly xFP (opportunities when xFP is
  missing for either) over completed games; needs ≥4 games each and the same metric; leader is the lower CV when the
  gap ≥ 0.10, else `even`; otherwise `leader=null` and the text says the data does not support a call.
* `ros` "Stronger rest-of-season projection" — ESPN ROS totals when both are complete (also names games remaining);
  otherwise `null` with the reason.
* `trend` "Bigger apparent usage trend" — usage_trend_score when both trends are known (not INSUFFICIENT).
No win probability, no "better trade asset" language.

## Frontend

`/compare?a=<id>&b=<id>` (server page). Shareable: every state is in the query string. `lib/compare.ts` holds the
pure helpers (URL parsing/building, unit formatting, chart series), `app/compare/*` the page. Links into the page
come from the player profile, Start/Sit, the trade analyzer and waivers via `compareHref()`. There is no Rankings
page on `main` yet; it should link with `compareHref()` when it lands.
