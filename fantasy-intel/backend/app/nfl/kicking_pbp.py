"""Kicker opportunity tables derived from nflverse play-by-play (regular season only).

Two tables, both rebuilt per season by the live sync (app/nfl/sync.py), the history sync
(app/nfl/history.py) and scripts/sync_kicking_pbp.py:

  nfl_kick_attempts      one row per FG or PAT try with a recorded result (penalty-nullified tries,
                         `no_play`, carry no result and are excluded). FG result made | missed |
                         blocked; PAT result made | missed | blocked ('aborted' snaps are a failed
                         try the kicker never kicked: kept as kick_type 'PAT' with result 'aborted').
  nfl_team_game_drives   one row per (game, offense): drive outcomes, scoring-range trips,
                         red-zone trips and conversion, FG/PAT/2-pt tries, 4th-down choices in FG
                         range, start field position, efficiency, and the game's kicker.

Drive segmentation uses nflverse's `fixed_drive` / `fixed_drive_result`, which already splits at
halftime, turnovers and special-teams changes of possession (the raw `drive` column does not). A
drive belongs to the team in `posteam` on its scrimmage plays. Kickoff plays carry the RECEIVING team
as posteam, so they are attributed to the receiving team's drive but never count as a scrimmage
play or set its field position. Defense numbers are not stored separately: a defense's allowed
values are its opponent's offensive row of the same game.

Game-day conditions (`roof`, `temp`, `wind`, `surface`) are copied as nflverse publishes them. They
are OBSERVED game conditions (from the game book), never a pregame forecast: models may use them
only in experiments labeled retrospective (docs/kicker_projection_engine.md, "Weather").
"""

from __future__ import annotations

import polars as pl

FG_RANGE_YARDLINE = 35      # yards from the opponent's goal line: a drive reaching here is in FG range (52-yd try)
RED_ZONE = 20
FOURTH_DOWN_RANGE = 40      # 4th downs at or inside the opponent's 40: the FG-or-go-for-it decision region

KICK_COLUMNS = ("season", "week", "game_id", "play_id", "posteam", "defteam", "home_team", "away_team",
                "kicker_player_id", "kicker_player_name", "kick_type", "kick_distance", "result", "qtr",
                "half_seconds_remaining", "score_differential", "roof", "surface", "temp", "wind",
                "stadium_id", "location")


def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


def _reg(pbp: pl.DataFrame) -> pl.DataFrame:
    return pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp


def _str(col: str, pbp: pl.DataFrame) -> pl.Expr:
    return pl.col(col).cast(pl.String) if col in pbp.columns else pl.lit(None, dtype=pl.String)


def _num(col: str, pbp: pl.DataFrame) -> pl.Expr:
    return pl.col(col).cast(pl.Float64) if col in pbp.columns else pl.lit(None, dtype=pl.Float64)


def derive_kicks(pbp: pl.DataFrame, season: int) -> list[dict]:
    p = _reg(pbp)
    fg = p.filter(pl.col("field_goal_result").is_not_null()).with_columns(
        pl.lit("FG").alias("kick_type"), pl.col("field_goal_result").alias("result"))
    pat_result = (pl.when(pl.col("extra_point_result") == "good").then(pl.lit("made"))
                  .when(pl.col("extra_point_result") == "failed").then(pl.lit("missed"))
                  .otherwise(pl.col("extra_point_result")))
    pat = p.filter(pl.col("extra_point_result").is_not_null()).with_columns(
        pl.lit("PAT").alias("kick_type"), pat_result.alias("result"))
    both = pl.concat([fg, pat], how="diagonal_relaxed")
    if both.height == 0:
        return []
    out = both.select(
        pl.lit(season).cast(pl.Int32).alias("season"), pl.col("week").cast(pl.Int32), "game_id",
        pl.col("play_id").cast(pl.Float64), "posteam", "defteam", "home_team", "away_team", "kicker_player_id",
        "kicker_player_name", "kick_type", _num("kick_distance", both).alias("kick_distance"), "result",
        _num("qtr", both).alias("qtr"), _num("half_seconds_remaining", both).alias("half_seconds_remaining"),
        _num("score_differential", both).alias("score_differential"), _str("roof", both).alias("roof"),
        _str("surface", both).alias("surface"), _num("temp", both).alias("temp"), _num("wind", both).alias("wind"),
        _str("stadium_id", both).alias("stadium_id"), _str("location", both).alias("location"),
    ).sort(["week", "game_id", "play_id"])
    return out.to_dicts()


def derive_team_drives(pbp: pl.DataFrame, season: int) -> list[dict]:
    p = _reg(pbp)
    if p.height == 0:
        return []
    scrimmage = pl.col("play_type").is_in(["run", "pass", "qb_kneel", "qb_spike", "field_goal", "punt"])
    p = p.with_columns(scrimmage.alias("_scrim"))
    # drive owner: the posteam of the drive's scrimmage plays (kickoffs carry the receiving team too)
    drives = (p.filter(pl.col("fixed_drive").is_not_null() & pl.col("posteam").is_not_null()
                       & (pl.col("play_type") != "kickoff"))
              .group_by(["game_id", "fixed_drive"]).agg(
                  pl.col("posteam").first().alias("team"),
                  pl.col("fixed_drive_result").drop_nulls().first().alias("result"),
                  pl.col("yardline_100").filter(pl.col("_scrim")).min().alias("min_yardline"),
                  pl.col("yardline_100").filter(pl.col("_scrim")).first().alias("start_yardline"),
                  pl.col("_scrim").sum().alias("scrim_plays")))
    drives = drives.filter(pl.col("scrim_plays") > 0)
    res = pl.col("result")
    reached_rz = pl.col("min_yardline") <= RED_ZONE
    d = drives.group_by(["game_id", "team"]).agg(
        pl.len().alias("drives"),
        (res == "Touchdown").sum().alias("td_drives"),
        res.is_in(["Field goal", "Missed field goal"]).sum().alias("fg_drives"),
        (res == "Punt").sum().alias("punt_drives"),
        res.is_in(["Turnover", "Opp touchdown"]).sum().alias("turnover_drives"),
        (res == "Turnover on downs").sum().alias("downs_drives"),
        (res == "End of half").sum().alias("end_half_drives"),
        (pl.col("min_yardline") <= FG_RANGE_YARDLINE).sum().alias("fg_range_drives"),
        reached_rz.sum().alias("rz_drives"),
        (reached_rz & (res == "Touchdown")).sum().alias("rz_td_drives"),
        (reached_rz & res.is_in(["Field goal", "Missed field goal"])).sum().alias("rz_fg_drives"),
        pl.col("start_yardline").mean().alias("avg_start_yardline"),
    )
    real = ~(pl.col("play_type") == "no_play")
    off = p.filter(pl.col("posteam").is_not_null()).group_by(["game_id", "posteam"]).agg(
        pl.col("week").first().cast(pl.Int32).alias("week"),
        pl.col("defteam").drop_nulls().first().alias("opponent"),
        pl.col("home_team").first(), pl.col("location").cast(pl.String).first().alias("location"),
        pl.col("roof").cast(pl.String).first().alias("roof"), pl.col("surface").cast(pl.String).first().alias("surface"),
        pl.col("temp").cast(pl.Float64).first().alias("temp"), pl.col("wind").cast(pl.Float64).first().alias("wind"),
        pl.col("stadium_id").cast(pl.String).first().alias("stadium_id"),
        pl.col("field_goal_result").is_not_null().sum().alias("fg_att"),
        (pl.col("field_goal_result") == "made").sum().alias("fg_made"),
        (pl.col("field_goal_result").is_not_null() & (pl.col("kick_distance") >= 50)).sum().alias("fg_att_50"),
        pl.col("extra_point_result").is_not_null().sum().alias("pat_att"),
        (pl.col("extra_point_result") == "good").sum().alias("pat_made"),
        pl.col("two_point_conv_result").is_not_null().sum().alias("two_pt_att"),
        ((pl.col("down") == 4) & (pl.col("yardline_100") <= FOURTH_DOWN_RANGE) & real
         & pl.col("play_type").is_in(["run", "pass"])).sum().alias("fourth_go_in_range"),
        ((pl.col("down") == 4) & (pl.col("yardline_100") <= FOURTH_DOWN_RANGE) & real
         & pl.col("play_type").is_in(["run", "pass", "field_goal", "punt"])).sum().alias("fourth_in_range"),
        (pl.col("play_type").is_in(["run", "pass"]) & real).sum().alias("plays"),
        pl.col("epa").filter(pl.col("play_type").is_in(["run", "pass"]) & real).mean().alias("epa_per_play"),
        pl.col("success").filter(pl.col("play_type").is_in(["run", "pass"]) & real).mean().alias("success_rate"),
        (_flag("interception") | _flag("fumble_lost")).sum().alias("turnovers"),
        pl.col("posteam_score_post").max().alias("points"),
        pl.col("kicker_player_id").filter(pl.col("field_goal_result").is_not_null()
                                          | pl.col("extra_point_result").is_not_null()).mode().first()
          .alias("kicker_id"),
    ).rename({"posteam": "team"})
    # kickoffs: posteam is the receiver, so the kicking team is defteam
    ko = p.filter(pl.col("play_type") == "kickoff").group_by(["game_id", "defteam"]).agg(
        pl.col("kicker_player_id").drop_nulls().mode().first().alias("kickoff_kicker_id")).rename({"defteam": "team"})
    out = off.join(d, on=["game_id", "team"], how="left").join(ko, on=["game_id", "team"], how="left")
    out = out.with_columns(
        pl.lit(season).cast(pl.Int32).alias("season"),
        (pl.col("team") == pl.col("home_team")).alias("is_home"),
        pl.coalesce("kicker_id", "kickoff_kicker_id").alias("game_kicker_id"),
    ).drop("home_team")
    return out.sort(["week", "game_id", "team"]).to_dicts()


def drive_reconciliation(kicks: list[dict], drives: list[dict]) -> dict:
    """FG tries by kick row vs by drive outcome. They differ legitimately (a fake FG, a FG on the last
    play of a half whose drive is labeled 'End of half', a blocked FG returned for a TD = 'Opp touchdown'),
    so this is a validation REPORT, not an equality: the share of team-games that agree."""
    fg_by_game: dict[tuple, int] = {}
    for k in kicks:
        if k["kick_type"] == "FG":
            fg_by_game[(k["game_id"], k["posteam"])] = fg_by_game.get((k["game_id"], k["posteam"]), 0) + 1
    agree = total = 0
    for r in drives:
        total += 1
        agree += fg_by_game.get((r["game_id"], r["team"]), 0) == (r.get("fg_drives") or 0)
    tries = sum(fg_by_game.values())
    return {"team_games": total, "fg_tries": tries, "fg_drives": sum(r.get("fg_drives") or 0 for r in drives),
            "team_games_agreeing": agree, "agree_rate": round(agree / total, 4) if total else None}
