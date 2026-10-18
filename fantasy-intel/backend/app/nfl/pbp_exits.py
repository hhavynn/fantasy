"""In-game injury evidence from nflverse play-by-play (research input for the ifp-2 challenger).

NFL gamebook play descriptions record in-game injuries ("BUF-7-Ta.Johnson was injured during the
play."). For each offensive skill player-game this derives:

  injured_in_game    1 if a play description says the player was injured (0 otherwise)
  injury_sec         game seconds elapsed at the first such play (null without one)
  last_touch_sec     game seconds elapsed at his last pass / rush / target (null without one)
  touches            plays as passer, rusher or targeted receiver (sacks and spikes excluded)
  team_last_sec      game seconds elapsed at his team's last offensive play

Players are identified by team + jersey number on that week's nflverse roster, confirmed by the
abbreviated last name in the description; an unconfirmed mention is dropped, never guessed.
Everything here is known once the game is final, so it may describe completed games in a forecast
for a LATER week; it is never a feature of the game it describes.
"""

from __future__ import annotations

import re

import polars as pl

INJURY_RE = re.compile(r"\b([A-Z]{2,3})-(\d{1,2})-([A-Za-z.'\- ]+?) (?:was|is) injured")
TEAM_ALIASES = {"JAC": "JAX", "LA": "LAR", "SD": "LAC", "STL": "LAR", "OAK": "LV", "WSH": "WAS"}
SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
          "injured_in_game": pl.Int8, "injury_sec": pl.Float64, "last_touch_sec": pl.Float64,
          "touches": pl.Int64, "team_last_sec": pl.Float64}
SKILL = ("QB", "RB", "FB", "WR", "TE")


def _elapsed(qtr: pl.Expr, remaining: pl.Expr) -> pl.Expr:
    # nflverse game_seconds_remaining counts down from 3600 in regulation; overtime is past 3600.
    return pl.when(qtr <= 4).then(3600 - remaining).otherwise(3600 + (600 - (remaining % 600)))


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def derive(pbp: pl.DataFrame, rosters: pl.DataFrame) -> pl.DataFrame:
    """`pbp`: nflverse play-by-play for REG games. `rosters`: nflverse weekly rosters with
    season, week, team, jersey_number, gsis_id, position, last_name."""
    if pbp.height == 0:
        return pl.DataFrame(schema=SCHEMA)
    p = pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp
    p = p.with_columns(_elapsed(pl.col("qtr"), pl.col("game_seconds_remaining")).alias("sec"),
                       pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64))
    team_last = (p.filter(pl.col("posteam").is_not_null()).group_by("season", "week", "posteam")
                 .agg(pl.col("sec").max().alias("team_last_sec")).rename({"posteam": "team"}))
    touch_rows = []
    for col in ("passer_player_id", "rusher_player_id", "receiver_player_id"):
        touch_rows.append(p.filter(pl.col(col).is_not_null() & pl.col("play_type").is_in(["pass", "run"])
                                   & (pl.col("sack").fill_null(0) == 0 if "sack" in p.columns else pl.lit(True)))
                          .select("season", "week", pl.col("posteam").alias("team"), pl.col(col).alias("gsis_id"),
                                  "sec", "play_id", "game_id"))
    touches = (pl.concat(touch_rows).unique(["game_id", "play_id", "gsis_id"])
               .group_by("season", "week", "gsis_id", "team")
               .agg(pl.len().alias("touches"), pl.col("sec").max().alias("last_touch_sec")))
    # injury mentions
    games = p.select("game_id", "season", "week", "posteam", "defteam").drop_nulls().unique()
    teams_in_game = {}
    for g in games.iter_rows(named=True):
        teams_in_game.setdefault(g["game_id"], set()).update({g["posteam"], g["defteam"]})
    ros = {}
    for r in rosters.select("season", "week", "team", "jersey_number", "gsis_id", "position", "last_name") \
            .drop_nulls(["jersey_number", "gsis_id"]).iter_rows(named=True):
        ros[(int(r["season"]), int(r["week"]), r["team"], int(r["jersey_number"]))] = r
    hits = []
    for r in p.filter(pl.col("desc").str.contains("injured")).select(
            "game_id", "season", "week", "sec", "desc").iter_rows(named=True):
        for team, num, name in INJURY_RE.findall(r["desc"]):
            team = team if team in teams_in_game.get(r["game_id"], ()) else TEAM_ALIASES.get(team, team)
            if team not in teams_in_game.get(r["game_id"], ()):
                continue
            who = ros.get((r["season"], r["week"], team, int(num)))
            if who is None or (who["position"] or "") not in SKILL:
                continue
            surname = _norm(name.split(".")[-1])
            if not surname or not (_norm(who["last_name"]).startswith(surname[:4]) or surname.startswith(_norm(who["last_name"])[:4])):
                continue
            hits.append({"season": r["season"], "week": r["week"], "gsis_id": who["gsis_id"], "team": team,
                         "injury_sec": float(r["sec"])})
    inj = (pl.DataFrame(hits, schema={"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                                      "injury_sec": pl.Float64})
           .group_by("season", "week", "gsis_id", "team").agg(pl.col("injury_sec").min()))
    out = (touches.join(inj, on=["season", "week", "gsis_id", "team"], how="full", coalesce=True)
           .join(team_last, on=["season", "week", "team"], how="left")
           .with_columns(pl.col("injury_sec").is_not_null().cast(pl.Int8).alias("injured_in_game"),
                         pl.col("touches").fill_null(0)))
    return out.select(list(SCHEMA)).cast(SCHEMA).sort("season", "week", "gsis_id")


def load_dir(path) -> pl.DataFrame | None:
    """All derived season files (<season>.parquet) under `path`, or None when there are none."""
    from pathlib import Path

    files = sorted(Path(path).glob("*.parquet")) if path and Path(path).exists() else []
    return pl.concat([pl.read_parquet(f) for f in files]).cast(SCHEMA, strict=False) if files else None


def sync_season(season: int, rosters: pl.DataFrame, out_dir, max_week: int | None = None,
                pbp_loader=None) -> dict:
    """Derive one season from nflverse play-by-play (nflreadpy) and write <out_dir>/<season>.parquet.
    `max_week` keeps only completed weeks <= it. Idempotent: the file is a pure function of the pbp."""
    import hashlib
    from pathlib import Path

    import nflreadpy as nfl

    pbp = (pbp_loader or (lambda s: nfl.load_pbp([s])))(season)
    if max_week is not None:
        pbp = pbp.filter(pl.col("week") <= max_week)
    df = derive(pbp, rosters)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{season}.parquet"
    df.write_parquet(path)
    digest = hashlib.sha256(df.sort("season", "week", "gsis_id").write_csv().encode()).hexdigest()[:16]
    return {"season": season, "rows": df.height, "injury_mentions": int(df["injured_in_game"].sum()),
            "max_week": int(df["week"].max()) if df.height else None, "sha": digest, "path": str(path)}
