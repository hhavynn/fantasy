# NFL metrics: verified data contract (authoritative)

**This document is the source of truth for every NFL metric the app computes.**
`nfl_fantasy_metrics_research.md` is background research; where the two differ,
this file wins. Every rule below was checked against upstream nflverse source or
documentation **and** against the 2026 data actually returned by `nflreadpy`
(weeks 1-5, checked 2026-10-13), except where a section is explicitly marked
UNRESOLVED (FTN `read_thrown` numeric codes). Code: `backend/app/nfl/sync.py`
(`classify_plays`, `derive_pbp`) and `backend/app/analytics/*`.

Conventions: `NULL` = unknown/not available, `0` = measured zero. A game that a
source did not cover produces `NULL`, never `0`.

## 1. Play classes (play-by-play)

| Class | Definition | Notes |
|---|---|---|
| rush play | `play_type == 'run'` AND `qb_kneel != 1` AND `two_point_attempt != 1` AND rusher present | Kneels are their own `play_type` (`qb_kneel`) in nflfastR; the explicit `qb_kneel` filter is belt-and-braces. **Scrambles are included** (`play_type == 'run'`, `qb_scramble == 1`). |
| designed rush | rush play AND `qb_scramble != 1` | Used for RB backfield share, red-zone / goal-line carries, rushing success, explosive runs. |
| target | `play_type == 'pass'` AND `pass_attempt == 1` AND `sack != 1` AND receiver present AND not 2-pt | Matches nflverse target counting (team targets = Σ player targets). |
| dropback | `qb_dropback == 1` AND not 2-pt | Includes sacks and scrambles (verified: scrambles carry `qb_dropback = 1`, `pass = 1`, `rush = 0`). |

Verified: nflverse `player_stats.carries` = run plays **including scrambles and
kneels** (team sums equal PBP run plays + kneels in 158/158 team-weeks). The app
keeps the official `carries` for display but computes rushing **shares** from the
PBP rush-play definition above so numerator and denominator match.

## 2. Rushing usage

- `team_rush_share = player rush plays / team rush plays` (scrambles included,
  kneels and two-point tries excluded). Measures share of the whole rushing
  offense, so RBs next to a mobile QB look lower here.
- `backfield_rush_share` (RBs only) `= player designed rushes / team designed
  rushes by RBs`. Scrambles never enter it. An RB who gets 85% of RB carries
  next to a running QB shows ~85% here even if his team share is ~55%.
- `rz_carries`: designed rushes with `yardline_100 <= 20`; `gl_carries`: `<= 5`.
  QB scrambles are excluded, so they don't distort goal-line RB workload. (QB
  designed runs and sneaks are designed rushes and do count for the QB.)
- `rushing_success_rate = designed rushes with success == 1 / designed rushes`
  (`success` = EPA > 0).
- `explosive_run_rate = designed rushes with yards_gained >= 10 / designed rushes`.
- `ybc_per_att`, `yac_per_att` = PFR `rushing_yards_before_contact` /
  `rushing_yards_after_contact` ÷ PFR `carries` (totals, not PFR's rounded averages).
  `broken_tackles` = PFR `rushing_broken_tackles` + `receiving_broken_tackles`.
  PFR rows join via `nfl_players.pfr_id` (512/512 rows mapped in 2026). PFR lags
  a few days; unpublished weeks are `NULL`.

There is **no hardcoded touchdown rate per goal-line carry** anywhere in the app.
(The earlier "~0.55 TDs per carry" figure is not used and not supported here.)

## 3. Receiving usage

- `target_share = player targets / team targets`, **not** team pass attempts.
  Verified in nflfastR `calculate_stats.R`: `target_share = targets /
  team_targets`; `air_yards_share = receiving_air_yards / team_air_yards`;
  `wopr = 1.5 * target_share + 0.7 * air_yards_share`. The provided columns are
  used; the fallback (only if missing) divides by team targets. Validation warns
  if a team-week's shares don't sum to ~1.0 (2026: every team-week sums to 1.000).
- `adot = receiving_air_yards / targets`, `NULL` when targets = 0.

### FTN read progression (`read_thrown`): **UNRESOLVED, experimental only**

**Status: `UNVERIFIED`.** The official nflreadr dictionary currently documents
`"0"` = first (primary) read, `"1"` = second read, `"2"` = third read or later.
**Observed data conflicts with that**, and upstream clarification is pending in
[nflverse/nflreadr#325](https://github.com/nflverse/nflreadr/issues/325).
The app preserves the data but **does not treat the numeric semantics as
authoritative**.

What we observed (cross-tab joined to PBP on `(game_id, play_id)`):

| season | run plays | pass plays: "0" | "1" | "2" | CHK | DES | SD |
|---|---|---|---|---|---|---|---|
| 2026 | 3,815 coded `"0"` (+285 SD) | 507 | 2,812 | 675 | 658 | 498 | 443 |
| 2024 | 14,739 coded `"0"` | 1,935 | 10,162 | 1,561 | 2,634 | 2,113 | 1,602 |

On targets, `"1"` covers roughly 50-60% and `"0"` about 2% (it mostly tags
non-pass plays). That pattern *suggests* `"1"` behaves like a primary read, but
the literal codes aren't confirmed, so nothing here is called "first read".

How the app handles it:

- Raw per-code target counts are stored: `ftn_read0_targets`,
  `ftn_read1_targets`, `ftn_read2_targets`; `ftn_charted_targets` counts every
  coded target. `DES` (designed), `CHK` (checkdown) and `SD` (scramble drill) are
  not in dispute and keep normal names (`designed_targets`, `checkdown_targets`,
  `scramble_drill_targets`).
- `ftn_read1_target_share = player code-"1" targets / team code-"1" targets` is
  shown only under **Advanced / experimental** on the player page as
  "FTN progression read-code 1 share (experimental)", with metadata
  `read_semantics_status = "UNVERIFIED"`, `assumed_primary_read_code = "1"`.
- It is **not** used in any player score, opportunity tier, role trend, market
  signal (BUY/SELL/WATCH), waiver score/explanation, or (later) trade value.
  Tests enforce this.
- Validation emits a **descriptive** warning (`ftn_read_thrown_semantics_unresolved`)
  with the observed code distribution and the upstream reference. It never fails
  the run and never concludes which code is primary.
- Games FTN hasn't charted give `NULL`, not `0`.
- **Not route participation.** Routes, route participation, TPRR and YPRR are
  **unavailable** in-season (nflverse participation data is published after the
  season). The app never approximates them.

## 4. Pressure / pass protection

- `sack_rate = sacks / dropbacks`.
- `hit_or_sack = dropbacks with (sack == 1 OR qb_hit == 1)`;
  `hit_or_sack_rate = hit_or_sack / dropbacks`. **Never `qb_hit + sack`.**
  Verified 2026: 333 of 357 sacks also carry `qb_hit = 1`, so summing would
  nearly double-count sacks. Validation errors if `hit_or_sack < sacks` or
  `> dropbacks`.
- `pfr_pressure_rate = Σ PFR times_pressured (team QBs) / PBP dropbacks`. PFR
  pressures include hurries; PBP has no hurry flag. Weeks PFR hasn't published
  are excluded from both numerator and denominator.

## 5. Offensive environment (team-week, aggregated season-to-date)

- `proe` = mean of `pass_oe` over scrimmage run/pass plays (percentage points;
  nflfastR `pass_oe` is already pass-over-expected × 100). Season value is
  weighted by plays.
- `neutral_pass_rate` = mean of `pass` (dropback incl. scrambles) on plays with
  `0.20 <= wp <= 0.80` and more than 120 seconds left in the half.
- `team_offense_snaps` = PFR `offense_snaps / offense_pct` for players with
  ≥ 50% snap share, median, rounded. Rejected (`NULL` + warning) if implied
  totals disagree by more than 2 snaps or a player exceeds the estimate. This
  replaces the earlier `max(offense_snaps)` proxy, which assumed some player was
  on the field for every snap.

### Implied team totals (spread sign verified)

nflverse `spread_line`: **positive = home team favored** (nflreadr dictionary;
correlation of `spread_line` with `result = home - away` is +0.33 over 2026
games). So:

```
home_implied = (total_line + spread_line) / 2
away_implied = (total_line - spread_line) / 2
```

Example: NE @ SEA, `spread_line = 3.0`, `total_line = 44.5` → SEA 23.75, NE 20.75.
(The research doc had the sign backwards; corrected there.) Validation checks
that home + away = total and that the favored side has the higher total.

## 6. Defensive matchup environment

Per defense-week counts, aggregated season-to-date as **sums** (never means of
weekly rates), with sample sizes stored:

- `pass_epa_allowed` = Σ EPA / plays where `pass == 1` (dropbacks incl. sacks/scrambles)
- `rush_epa_allowed`, `rush_success_allowed`, `explosive_run_rate_allowed`: designed rushes
- `rb_target_share_allowed` / `te_target_share_allowed` = targets to RBs/TEs ÷
  targets allowed (receiver position from weekly rosters → nflverse players)

Defenses are ranked 1-32 (1 = most generous). The matchup label for a player
uses the next opponent's mean rank on position-relevant metrics (RB: rush EPA +
RB target share; WR/QB: pass EPA; TE: pass EPA + TE target share):
`<= 10` FAVORABLE, `>= 23` TOUGH, else NEUTRAL; INSUFFICIENT under 3 games.
Fantasy points allowed are **not** used.

## 7. Next Gen Stats

NGS only publishes rows for players who meet its attempt/target minimums.
NGS fields (`ngs_rush_yoe_per_att`, `ngs_avg_separation`) are **LEFT JOINed and
never coalesced**: missing = not qualified/unavailable, and a real `0.0` is kept
as `0.0`. No qualification thresholds are encoded as business logic. Week 0
(season aggregate) rows are excluded.

## 8. Injuries

nflverse documents `date_modified` on `load_injuries`, but the 2025 and 2026
files as returned by nflreadpy don't include it, and there's one row per
player-week. Selection order everywhere (roster, waivers, manager injury
context): `week DESC`, then `date_modified DESC` **when the column exists**, then
a deterministic conservative tie-break (Out > Doubtful > Questionable, then
practice status). A warning is logged if duplicates exist without timestamps.
Only a report for the **current** scoring week affects availability.

Availability model (`analytics/availability.py`), most severe signal wins
across ESPN status, ESPN IR slot and the current-week NFL report:

| status | weight | |
|---|---|---|
| AVAILABLE | 1.00 | |
| QUESTIONABLE | 0.75 | risk-adjusted, still counted |
| DOUBTFUL | 0.15 | effectively unavailable for immediate-week needs |
| OUT / IR / SUSPENDED | 0.00 | |

Weights are explicit risk discounts, **not** play probabilities.

## 9. Unavailable in-season (by design)

Routes run, route participation, TPRR, YPRR, slot/wide alignment, personnel
groupings, man/zone coverage, defensive personnel packages, PFF-style grades.
The UI labels these unavailable instead of approximating them.
