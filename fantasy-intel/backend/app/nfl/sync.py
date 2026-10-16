"""nflverse sync via nflreadpy (current season only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import nflreadpy as nfl
import polars as pl

from ..analytics import injury_freshness
from ..db.connection import SyncRun, replace_frame_partition, replace_where, upsert_rows, utcnow
# FTN `read_thrown` semantics live in ftn.py so the API can describe them without importing polars.
from .kicking_pbp import derive_kicks, derive_team_drives
from .ftn import (FTN_ASSUMED_PRIMARY_READ_CODE, FTN_NUMERIC_CODES, FTN_READ_SEMANTICS_STATUS,  # noqa: F401
                  FTN_SEMANTICS_REFERENCE)

log = logging.getLogger("nfl.sync")

# table name -> (loader, season column or None for non-seasonal tables)
RAW_DATASETS: dict[str, tuple[Callable[[int], pl.DataFrame], str | None]] = {
    "nfl_weekly_stats": (lambda s: nfl.load_player_stats([s]), "season"),
    "nfl_snap_counts": (lambda s: nfl.load_snap_counts([s]), "season"),
    "nfl_injuries": (lambda s: nfl.load_injuries([s]), "season"),
    "nfl_schedules": (lambda s: nfl.load_schedules([s]), "season"),
    "nfl_rosters_weekly": (lambda s: nfl.load_rosters_weekly([s]), "season"),
    "nfl_ff_opportunity": (
        lambda s: nfl.load_ff_opportunity([s]).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)),
        "season",
    ),
    "nfl_depth_charts": (
        lambda s: nfl.load_depth_charts([s]).with_columns(pl.lit(s, dtype=pl.Int32).alias("season")),
        "season",
    ),
    "nfl_ff_playerids": (lambda s: nfl.load_ff_playerids(), None),
    "nfl_pfr_rush": (lambda s: nfl.load_pfr_advstats([s], stat_type="rush", summary_level="week"), "season"),
    "nfl_pfr_rec": (lambda s: nfl.load_pfr_advstats([s], stat_type="rec", summary_level="week"), "season"),
    "nfl_pfr_pass": (lambda s: nfl.load_pfr_advstats([s], stat_type="pass", summary_level="week"), "season"),
    "nfl_ngs_rushing": (lambda s: nfl.load_nextgen_stats([s], stat_type="rushing"), "season"),
    "nfl_ngs_receiving": (lambda s: nfl.load_nextgen_stats([s], stat_type="receiving"), "season"),
}


@dataclass
class NflPayloads:
    season: int
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    players: pl.DataFrame | None = None
    pbp: pl.DataFrame | None = None
    ftn: pl.DataFrame | None = None
    warnings: list[str] = field(default_factory=list)
    # table -> (download started, download returned), naive UTC from our clock; read by the injury
    # retrieval log (analytics/injury_freshness.py). Only datasets that downloaded are present.
    fetch_times: dict[str, tuple] = field(default_factory=dict)


def _try(warnings: list[str], label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as err:
        msg = f"{label} failed: {type(err).__name__}: {err}"
        log.warning(msg)
        warnings.append(msg)
        return None


def fetch_all(season: int) -> NflPayloads:
    p = NflPayloads(season=season)
    for table, (loader, _) in RAW_DATASETS.items():
        log.info("loading %s", table)
        started = utcnow()
        df = _try(p.warnings, table, lambda: loader(season))
        if df is not None:
            p.frames[table] = df
            p.fetch_times[table] = (started, utcnow())
    p.players = _try(p.warnings, "players", nfl.load_players)
    log.info("loading play-by-play")
    p.pbp = _try(p.warnings, "pbp", lambda: nfl.load_pbp([season]))
    log.info("loading FTN charting")
    p.ftn = _try(p.warnings, "ftn charting", lambda: nfl.load_ftn_charting([season]))
    return p


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def curate_players(players: pl.DataFrame) -> list[dict]:
    rows = []
    for r in players.iter_rows(named=True):
        if not r.get("gsis_id"):
            continue
        rows.append({
            "gsis_id": r["gsis_id"],
            "display_name": r.get("display_name"),
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "position": r.get("position"),
            "position_group": r.get("position_group"),
            "latest_team": r.get("latest_team"),
            "status": r.get("status"),
            "espn_id": _int(r.get("espn_id")),
            "pfr_id": r.get("pfr_id"),
            "sleeper_id": None,
            "birth_date": r.get("birth_date") or None,
            "rookie_season": _int(r.get("rookie_season")),
            "years_of_experience": _int(r.get("years_of_experience")),
            "draft_year": _int(r.get("draft_year")),
            "draft_round": _int(r.get("draft_round")),
            "draft_pick": _int(r.get("draft_pick")),
        })
    return rows


def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


def classify_plays(pbp: pl.DataFrame) -> pl.DataFrame:
    """Adds explicit boolean play classes used by every PBP aggregate.

    rush_play     : play_type == 'run', not a kneel, not a two-point try, has a rusher.
                    Scrambles ARE included (they consume team rushing volume).
    designed_rush : rush_play and not a scramble (used for RB backfield share, RZ/GL work).
    target        : play_type == 'pass', pass_attempt, not a sack, has a receiver, not 2-pt.
    dropback      : qb_dropback == 1, not 2-pt (includes sacks and scrambles).
    hit_or_sack   : dropback and (sack == 1 OR qb_hit == 1) -- never qb_hit + sack.
    """
    not_2pt = ~_flag("two_point_attempt")
    rush_play = ((pl.col("play_type") == "run") & ~_flag("qb_kneel") & not_2pt
                 & pl.col("rusher_player_id").is_not_null())
    return pbp.with_columns(
        rush_play.alias("rush_play"),
        (rush_play & ~_flag("qb_scramble")).alias("designed_rush"),
        (rush_play & _flag("qb_scramble")).alias("scramble"),
        ((pl.col("play_type") == "pass") & _flag("pass_attempt") & ~_flag("sack") & not_2pt
         & pl.col("receiver_player_id").is_not_null()).alias("target"),
        (_flag("qb_dropback") & not_2pt).alias("dropback"),
        (_flag("qb_dropback") & not_2pt & (_flag("sack") | _flag("qb_hit"))).alias("hit_or_sack"),
        (pl.col("play_type").is_in(["run", "pass"]) & not_2pt).alias("scrimmage"),
    )


def derive_pbp(pbp: pl.DataFrame, season: int, positions: dict[str, str],
               ftn: pl.DataFrame | None = None, pfr_pass: pl.DataFrame | None = None,
               ) -> tuple[list[dict], list[dict], list[dict]]:
    """Player-week, offense team-week and defense team-week aggregates from play-by-play."""
    p = classify_plays(pbp.filter(pl.col("season_type") == "REG"))
    pos = pl.DataFrame({"pid": list(positions.keys()), "ppos": list(positions.values())},
                       schema={"pid": pl.String, "ppos": pl.String})
    p = (p.join(pos.rename({"pid": "rusher_player_id", "ppos": "rusher_pos"}), on="rusher_player_id", how="left")
          .join(pos.rename({"pid": "receiver_player_id", "ppos": "receiver_pos"}), on="receiver_player_id", how="left"))
    if ftn is not None and ftn.height:
        codes = ftn.select(pl.col("nflverse_game_id").alias("game_id"),
                           pl.col("nflverse_play_id").cast(pl.Float64).alias("play_id"), "read_thrown")
        p = p.join(codes, on=["game_id", "play_id"], how="left")
        charted_games = set(ftn["nflverse_game_id"].unique().to_list())
    else:
        p = p.with_columns(pl.lit(None, dtype=pl.String).alias("read_thrown"))
        charted_games = set()
    p = p.with_columns(pl.col("game_id").is_in(list(charted_games)).alias("ftn_game"))

    rz, i10, gl = pl.col("yardline_100") <= 20, pl.col("yardline_100") <= 10, pl.col("yardline_100") <= 5
    dr, tg, rp = pl.col("designed_rush"), pl.col("target"), pl.col("rush_play")
    code = pl.col("read_thrown")
    charted_target = tg & code.is_not_null()

    rushes = p.filter(rp).group_by(["week", "rusher_player_id", "posteam"]).agg(
        pl.len().alias("rush_attempts"),
        dr.sum().alias("designed_rushes"),
        pl.col("scramble").sum().alias("scrambles"),
        (dr & _flag("success")).sum().alias("designed_rush_success"),
        (dr & (pl.col("yards_gained") >= 10)).sum().alias("explosive_runs"),
        (dr & rz).sum().alias("rz_carries"),
        (dr & i10).sum().alias("inside10_carries"),
        (dr & gl).sum().alias("gl_carries"),
    ).rename({"rusher_player_id": "player_id", "posteam": "team"})
    targets = p.filter(tg).group_by(["week", "receiver_player_id", "posteam"]).agg(
        pl.len().alias("targets"),
        rz.sum().alias("rz_targets"),
        i10.sum().alias("inside10_targets"),
        pl.col("ftn_game").any().alias("ftn_game"),
        charted_target.sum().alias("ftn_charted_targets"),
        *[(code == c).sum().alias(f"ftn_read{c}_targets") for c in FTN_NUMERIC_CODES],
        (code == "DES").sum().alias("designed_targets"),
        (code == "CHK").sum().alias("checkdown_targets"),
        (code == "SD").sum().alias("scramble_drill_targets"),
    ).rename({"receiver_player_id": "player_id", "posteam": "team"})
    player = rushes.join(targets, on=["week", "player_id", "team"], how="full", coalesce=True)
    zero_cols = ["rush_attempts", "designed_rushes", "scrambles", "designed_rush_success", "explosive_runs",
                 "rz_carries", "inside10_carries", "gl_carries", "targets", "rz_targets", "inside10_targets"]
    ftn_cols = ["ftn_charted_targets", *[f"ftn_read{c}_targets" for c in FTN_NUMERIC_CODES],
                "designed_targets", "checkdown_targets", "scramble_drill_targets"]
    player = player.with_columns([pl.col(c).fill_null(0) for c in zero_cols])

    team_off = p.filter(pl.col("scrimmage")).group_by(["week", "posteam"]).agg(
        pl.col("defteam").first().alias("opponent"),
        rp.sum().alias("team_rush_attempts"),
        dr.sum().alias("team_designed_rushes"),
        pl.col("scramble").sum().alias("team_scrambles"),
        (dr & (pl.col("rusher_pos") == "RB")).sum().alias("team_rb_carries"),
        tg.sum().alias("team_targets"),
        (dr & rz).sum().alias("team_rz_carries"),
        (tg & rz).sum().alias("team_rz_targets"),
        (tg & (code == FTN_ASSUMED_PRIMARY_READ_CODE)).sum().alias("team_ftn_read1_targets"),
        charted_target.sum().alias("team_ftn_charted_targets"),
        pl.col("ftn_game").any().alias("ftn_game"),
        pl.col("dropback").sum().alias("dropbacks"),
        (pl.col("dropback") & _flag("sack")).sum().alias("sacks"),
        pl.col("hit_or_sack").sum().alias("hit_or_sack"),
        pl.col("pass_oe").drop_nulls().mean().alias("proe"),
        pl.col("pass_oe").drop_nulls().len().alias("proe_plays"),
        pl.col("pass").filter((pl.col("wp") >= 0.2) & (pl.col("wp") <= 0.8)
                              & (pl.col("half_seconds_remaining") > 120)).mean().alias("neutral_pass_rate"),
        ((pl.col("wp") >= 0.2) & (pl.col("wp") <= 0.8) & (pl.col("half_seconds_remaining") > 120)).sum()
        .alias("neutral_plays"),
        pl.len().alias("offense_plays"),
    ).rename({"posteam": "team"})

    pressures: dict[tuple, float] = {}
    if pfr_pass is not None and pfr_pass.height:
        for r in pfr_pass.filter(pl.col("game_type") == "REG").group_by(["week", "team"]).agg(
                pl.col("times_pressured").sum()).iter_rows(named=True):
            pressures[(r["week"], r["team"])] = r["times_pressured"]

    defense = p.filter(pl.col("scrimmage")).group_by(["week", "defteam"]).agg(
        pl.col("posteam").first().alias("opponent"),
        (_flag("pass") & pl.col("epa").is_not_null()).sum().alias("pass_plays"),
        pl.col("epa").filter(_flag("pass")).sum().alias("pass_epa_sum"),
        (dr & pl.col("epa").is_not_null()).sum().alias("rush_plays"),
        pl.col("epa").filter(dr).sum().alias("rush_epa_sum"),
        (dr & _flag("success")).sum().alias("rush_success"),
        (dr & (pl.col("yards_gained") >= 10)).sum().alias("explosive_runs"),
        tg.sum().alias("targets"),
        (tg & (pl.col("receiver_pos") == "RB")).sum().alias("rb_targets"),
        (tg & (pl.col("receiver_pos") == "WR")).sum().alias("wr_targets"),
        (tg & (pl.col("receiver_pos") == "TE")).sum().alias("te_targets"),
    )

    player_rows = []
    for r in player.iter_rows(named=True):
        charted = r.pop("ftn_game")
        if not charted:  # game not charted by FTN: unknown, not zero
            for c in ftn_cols:
                r[c] = None
        else:
            for c in ftn_cols:
                r[c] = r[c] or 0
        player_rows.append({**r, "season": season})
    team_rows = []
    for r in team_off.iter_rows(named=True):
        charted = r.pop("ftn_game")
        if not charted:
            r["team_ftn_read1_targets"] = r["team_ftn_charted_targets"] = None
        db = r["dropbacks"]
        r["sack_rate"] = r["sacks"] / db if db else None
        r["hit_or_sack_rate"] = r["hit_or_sack"] / db if db else None
        pr = pressures.get((r["week"], r["team"]))
        r["pfr_pressures"] = int(pr) if pr is not None else None
        r["pfr_pressure_rate"] = pr / db if pr is not None and db else None
        r["team_offense_snaps"] = None  # filled from PFR snap counts in analytics
        team_rows.append({**r, "season": season})
    defense_rows = [{**r, "season": season} for r in defense.iter_rows(named=True)]
    return player_rows, team_rows, defense_rows


def player_positions(rosters_weekly: pl.DataFrame | None, players: pl.DataFrame | None) -> dict[str, str]:
    """gsis -> position: latest weekly-roster position this season, else nflverse players table."""
    out: dict[str, str] = {}
    if players is not None:
        for g, ps in players.select("gsis_id", "position").iter_rows():
            if g and ps:
                out[g] = "RB" if ps == "FB" else ps
    if rosters_weekly is not None and rosters_weekly.height:
        latest = rosters_weekly.sort("week").group_by("gsis_id").agg(pl.col("position").last())
        for g, ps in latest.iter_rows():
            if g and ps:
                out[g] = "RB" if ps == "FB" else ps
    return out


def write_all(con, p: NflPayloads) -> dict:
    stats: dict[str, Any] = {"warnings": list(p.warnings), "season": p.season}
    con.execute("BEGIN TRANSACTION")
    try:
        for table, df in p.frames.items():
            season_col = RAW_DATASETS[table][1]
            stats[table] = replace_frame_partition(con, table, df, season_col, p.season)
            if "week" in df.columns and season_col:
                stats[f"{table}_max_week"] = df["week"].max()
        if p.players is not None:
            stats["nfl_players"] = upsert_rows(con, "nfl_players", curate_players(p.players), ["gsis_id"])
        if p.pbp is not None:
            positions = player_positions(p.frames.get("nfl_rosters_weekly"), p.players)
            player_rows, team_rows, defense_rows = derive_pbp(
                p.pbp, p.season, positions, ftn=p.ftn, pfr_pass=p.frames.get("nfl_pfr_pass"))
            stats["nfl_pbp_player_week"] = replace_where(con, "nfl_pbp_player_week", player_rows, "season = ?", [p.season])
            stats["nfl_team_week"] = replace_where(con, "nfl_team_week", team_rows, "season = ?", [p.season])
            stats["nfl_defense_week"] = replace_where(con, "nfl_defense_week", defense_rows, "season = ?", [p.season])
            stats["nfl_kick_attempts"] = replace_where(con, "nfl_kick_attempts", derive_kicks(p.pbp, p.season),
                                                       "season = ?", [p.season])
            stats["nfl_team_game_drives"] = replace_where(con, "nfl_team_game_drives",
                                                          derive_team_drives(p.pbp, p.season), "season = ?", [p.season])
            stats["pbp_max_week"] = p.pbp["week"].max()
            stats["ftn_games_charted"] = p.ftn["nflverse_game_id"].n_unique() if p.ftn is not None else 0
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return stats


def record_injury_retrievals(con, p: NflPayloads, sync_run_id: str | None = None) -> dict | None:
    """Append this sync's injury download to the write-once retrieval log (injury_retrievals), using
    the download times captured by fetch_all. Not part of the data transaction and it changes no
    existing table; a failure is returned to the caller as a warning, never raised."""
    df = p.frames.get("nfl_injuries")
    times = p.fetch_times.get("nfl_injuries")
    if df is None or times is None:
        return None
    obs = injury_freshness.nflverse_observations(df.to_dicts(), p.season, retrieved_at=times[1],
                                                 fetch_started_at=times[0], sync_run_id=sync_run_id)
    return injury_freshness.record_observations(con, obs)


def run_nfl_sync(con_factory, season: int) -> dict:
    payloads = fetch_all(season)
    with con_factory() as con:
        run = SyncRun(con, "nfl")
        run.start(stats={"season": season})      # a crash after write_all's COMMIT leaves this 'running' row (see injury_freshness)
        try:
            stats = write_all(con, payloads)
        except Exception as err:
            run.finish("error", f"{type(err).__name__}: {err}")
            raise
        try:
            stats["injury_retrievals"] = record_injury_retrievals(con, payloads, run.run_id)
        except Exception as err:   # audit log only; the synced data above is already committed
            msg = f"injury retrieval log failed: {type(err).__name__}: {err}"
            log.warning(msg)
            stats["warnings"].append(msg)
        run.finish("ok", "warnings: %d" % len(stats["warnings"]), stats)
    return stats
