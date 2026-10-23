# Game splits (issue #210)

`/research/splits` compares one WR, RB or TE's past games with a chosen teammate on the field against games without
him. It is descriptive: no forecast, no causal claim, no probability or confidence number. The engine is
`backend/app/analytics/game_splits.py`; the API is `GET /api/research/game-splits` and
`GET /api/research/game-splits/players` on the research router, mounted like the rest of the Research lab (legacy
`/api/research/...`, and `/v2/leagues/{lg}/api/research/...` for platform admins only).

## Data inventory

Only nflverse tables the NFL sync already stores in the global file are read. Nothing is fetched, scraped or bought,
and no league roster, lineup or scoring rule feeds a number.

| Table | Used for | Notes |
|---|---|---|
| `player_week_usage` | Subject metrics (snaps, targets, carries, routes, xFP, PPR); teammate appearance and snap share; each team's starting QB (most offensive snaps) | Rows for QB/RB/WR/TE. `offense_snaps` is NULL without a snap record; `stats_source` says whether a box-score row exists. `routes` only from an authorized external import. |
| `nfl_rosters_weekly` | Teammate status per week: ACT, INA, RES, DEV, SUS and other statuses, and the team he was listed on | REG rows (or rows without a game type). A player listed twice in a week is ambiguous unless an appearance resolves it. |
| `nfl_schedules` | Game existence (byes), final score, game day for the cutoff, home/away, opponent, team points | A game counts only when final. |
| `nfl_team_week` (optional) | Neutral pass rate, offensive plays | Missing values stay missing. |
| `team_game_lines` (optional) | Implied team total | Missing values stay missing. |
| `nfl_players` | Names and positions | |
| `player_id_map` | The ESPN id behind the player-profile link only (a high-confidence single match; two ids link to neither) | Same use as the Research lab. |

## Definitions

* **Window**: an inclusive range from (season, week) to (season, week), regular season weeks 1-18, at most three
  seasons. **Cutoff** (`as_of`, a date): a week whose first scheduled game is on or after it is not read at all; a
  game counts only if its game day is before it and it is final.
* **Subject game**: the player played (an offensive snap, target or carry) with nflverse xFP and PPR, listed at WR,
  RB or TE that week, in a counted game. Byes, missed games, games without xFP and weeks at another position are
  listed as "not a sample".
* **Teammates**: games count only for a team the two shared (a roster row of any status, or an appearance) in a
  counted week of the window.
* **With**: the teammate appeared for that team (a box-score row or offensive snaps) with a snap record, and it was
  not a partial game (below 50% of his median snap share for the team over counted games, once that median rests on
  three games).
* **Without**: game-day inactive (INA), reserve list (RES), practice squad (DEV), suspended (SUS), another
  non-active status on the team, on another team that week, or a snap record with no offensive snap.
* **Excluded (unknown, never "without")**: listed on two teams that week, ACT with no box-score or snap record, an
  appearance without a snap record, a partial game, a roster record with a blank team or status, no roster record
  although the team's roster is stored, or no roster data for the team-week. Counted by reason.
* **Sufficiency**: differences (without minus with) are shown only when both groups have 3 games; below that the
  split abstains with the counts. Under 6 games in a group a caution is shown. Multiple starting QBs, a teammate on
  another team and multi-season windows add their own cautions.

## Reproducibility

Every input is in the query string, so the league-scoped response cache keys on it, and the response echoes the
inputs and a `cache_key`. The week list gives each week's outcome, reason, the teammate's roster rows and snap
share, the subject's numbers and the starting QB, so every count can be rebuilt by hand. Results are sorted and
contain no wall-clock values.
