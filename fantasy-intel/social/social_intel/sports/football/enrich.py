"""Enrich an NFL trend cluster with our own analytics, read-only from ``fantasy.duckdb``.

Every number returned is a ``SourcedStat``-shaped dict with the table it came from and the data cut
(``as_of``) it was computed on. Point-in-time rule: only weeks whose games had all kicked off before
``now`` are used, so replaying an old trend never sees later games.

The output also compares what the internet is saying (topic direction: breakout/buy vs bust/sell)
with what the usage data says (role up or down), which is the editorial edge: "the internet thinks
X, our data says Y".
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

POSITIVE_TOPICS = {"breakout", "buy_low"}
NEGATIVE_TOPICS = {"bust", "sell_high"}
LEAD_METRIC = {"RB": "backfield_rush_share", "WR": "target_share", "TE": "target_share", "QB": "fantasy_points_ppr"}
METRIC_LABEL = {
    "offense_pct": "snap share", "target_share": "target share", "backfield_rush_share": "share of RB carries",
    "air_yards_share": "air-yard share", "targets": "targets", "carries": "carries", "xfp": "expected PPR points",
    "fantasy_points_ppr": "PPR points", "rz_opps": "red-zone opportunities",
}


def _stat(label: str, value: Any, source: str, as_of: str) -> dict[str, Any]:
    if isinstance(value, float):
        value = round(value, 3)
    return {"label": label, "value": value, "source": source, "as_of": as_of}


def social_direction(topics: list[str], stance: dict[str, Any] | None = None) -> str:
    """Which way the crowd leans: counted posts when we have them (stance), else the topic mix."""
    pos, neg = (stance or {}).get("positive", 0), (stance or {}).get("negative", 0)
    if pos + neg >= 2:
        if pos >= 1.5 * max(neg, 0.5):
            return "positive"
        if neg >= 1.5 * max(pos, 0.5):
            return "negative"
        return "mixed"
    t = set(topics)
    if t & POSITIVE_TOPICS and not t & NEGATIVE_TOPICS:
        return "positive"
    if t & NEGATIVE_TOPICS and not t & POSITIVE_TOPICS:
        return "negative"
    return "mixed"


@dataclass
class DataCut:
    season: int
    through_week: int
    synced_at: str | None

    @property
    def label(self) -> str:
        s = f"{self.season} weeks 1-{self.through_week}"
        return s + (f" (synced {self.synced_at})" if self.synced_at else "")


class FootballEnricher:
    def __init__(self, db_path: Path, season: int, now: datetime | None = None):
        self.db_path = Path(db_path)
        self.season = season
        self.now = now or datetime.now(timezone.utc)
        self.con: duckdb.DuckDBPyConnection | None = None
        self.cut: DataCut | None = None
        if self.db_path.exists():
            try:
                self.con = duckdb.connect(str(self.db_path), read_only=True)
                self.cut = self._data_cut()
            except duckdb.Error:
                self.con = None

    def close(self) -> None:
        if self.con:
            self.con.close()

    # ------------------------------------------------------------------ point in time

    def _data_cut(self) -> DataCut:
        con = self.con
        assert con is not None
        max_usage = con.execute("SELECT max(week) FROM player_week_usage WHERE season = ?", [self.season]).fetchone()[0] or 0
        # last week whose final game kicked off before `now` (games list gameday + gametime ET)
        completed = con.execute(
            """SELECT max(week) FROM (
                 SELECT week, max(CAST(gameday AS DATE)) AS last_day FROM nfl_schedules
                 WHERE season = ? AND game_type = 'REG' GROUP BY week)
               WHERE last_day < CAST(? AS DATE)""",
            [self.season, self.now.astimezone(timezone.utc).replace(tzinfo=None)]).fetchone()[0] or 0
        synced = con.execute("SELECT max(finished_at) FROM sync_runs WHERE source = 'nfl' AND status = 'ok'").fetchone()[0]
        return DataCut(self.season, int(min(max_usage, completed)), synced.strftime("%Y-%m-%d %H:%MZ") if synced else None)

    def upcoming_week(self) -> int:
        return (self.cut.through_week if self.cut else 0) + 1

    # ------------------------------------------------------------------ per player

    def player(self, gsis_id: str) -> dict[str, Any] | None:
        if not self.con or not self.cut:
            return None
        con, cut = self.con, self.cut
        src_usage = f"fantasy.duckdb:player_week_usage season={cut.season}"
        rows = con.execute(
            """SELECT week, team, opponent, position, offense_pct, target_share, backfield_rush_share,
                      air_yards_share, targets, carries, coalesce(rz_targets,0)+coalesce(rz_carries,0) AS rz_opps,
                      xfp, fantasy_points_ppr
               FROM player_week_usage WHERE season = ? AND gsis_id = ? AND week <= ? ORDER BY week""",
            [cut.season, gsis_id, cut.through_week]).fetchall()
        ident = con.execute(
            """SELECT arg_max(full_name, week), arg_max(team, week), arg_max(position, week),
                      arg_max(status, week), arg_max(espn_id, week)
               FROM nfl_rosters_weekly WHERE season = ? AND gsis_id = ? AND week <= ?""",
            [cut.season, gsis_id, max(cut.through_week, 1) + 1]).fetchone()
        if not ident or not ident[0]:
            return None
        name, team, pos, roster_status, espn_id = ident
        out: dict[str, Any] = {"gsis_id": gsis_id, "name": name, "team": team, "position": pos,
                               "roster_status": roster_status, "stats": [], "weekly": [], "data_cut": cut.label}
        cols = ["week", "team", "opponent", "position", "offense_pct", "target_share", "backfield_rush_share",
                "air_yards_share", "targets", "carries", "rz_opps", "xfp", "fantasy_points_ppr"]
        weekly = [dict(zip(cols, r)) for r in rows]
        out["weekly"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in w.items()} for w in weekly]
        stats = out["stats"]
        if weekly:
            recent, base = weekly[-2:], weekly[:-2]
            lead = LEAD_METRIC.get(pos or "", "offense_pct")
            trend = {}
            for m in ("offense_pct", "target_share", "backfield_rush_share", "targets", "carries", "xfp",
                      "fantasy_points_ppr", "rz_opps"):
                rv = [w[m] for w in recent if w[m] is not None]
                bv = [w[m] for w in base if w[m] is not None]
                if not rv:
                    continue
                r_mean = statistics.fmean(rv)
                weeks_r = f"weeks {recent[0]['week']}-{recent[-1]['week']}" if len(recent) > 1 else f"week {recent[0]['week']}"
                stats.append(_stat(f"{METRIC_LABEL[m]}, {weeks_r} avg", r_mean, src_usage + f" {weeks_r}", cut.label))
                if bv:
                    b_mean = statistics.fmean(bv)
                    weeks_b = f"weeks {base[0]['week']}-{base[-1]['week']}"
                    stats.append(_stat(f"{METRIC_LABEL[m]}, {weeks_b} avg", b_mean, src_usage + f" {weeks_b}", cut.label))
                    trend[m] = {"recent": round(r_mean, 3), "baseline": round(b_mean, 3), "delta": round(r_mean - b_mean, 3),
                                "recent_weeks": [w["week"] for w in recent], "baseline_weeks": [w["week"] for w in base]}
            out["trend"] = trend
            out["lead_metric"] = lead
            ld = trend.get(lead) or trend.get("offense_pct")
            if ld:
                scale = 1.0 if lead in ("fantasy_points_ppr", "xfp") else 0.0
                thresh = 3.0 if scale else 0.05
                out["data_direction"] = "up" if ld["delta"] > thresh else "down" if ld["delta"] < -thresh else "flat"
        score = con.execute(
            """SELECT as_of_week, trend_label, role_trend, market_signal, value_gap_label, fp_per_game, xfp_per_game,
                      fp_minus_xfp, opportunity_score_100, value_score_100, reasons
               FROM player_scores WHERE season = ? AND gsis_id = ? AND as_of_week <= ?""",
            [cut.season, gsis_id, cut.through_week]).fetchone()
        if score:
            src = f"fantasy.duckdb:player_scores season={cut.season} as_of_week={score[0]}"
            out["scores"] = {"trend_label": score[1], "role_trend": score[2], "market_signal": score[3],
                             "value_gap_label": score[4], "reasons": json.loads(score[10]) if score[10] else []}
            if score[5] is not None:
                stats.append(_stat("PPR points per game", score[5], src, cut.label))
            if score[6] is not None:
                stats.append(_stat("expected PPR points per game (xFP)", score[6], src, cut.label))
            if score[7] is not None:
                stats.append(_stat("PPR points vs expected, per game", score[7], src, cut.label))
            if score[8] is not None:
                stats.append(_stat("opportunity rating (0-100)", score[8], src, cut.label))
        inj = con.execute(
            """SELECT week, report_status, report_primary_injury, practice_status FROM nfl_injuries
               WHERE season = ? AND gsis_id = ? AND week <= ? ORDER BY week DESC LIMIT 1""",
            [cut.season, gsis_id, cut.through_week + 1]).fetchone()
        if inj:
            out["injury"] = {"week": inj[0], "report_status": inj[1], "injury": inj[2], "practice_status": inj[3],
                             "source": f"fantasy.duckdb:nfl_injuries season={cut.season} week={inj[0]} (official report)"}
        missed = con.execute(
            """SELECT list(DISTINCT week ORDER BY week) FROM nfl_injuries WHERE season = ? AND gsis_id = ? AND week <= ?
               AND lower(report_status) IN ('out', 'doubtful')""", [cut.season, gsis_id, cut.through_week]).fetchone()
        out["out_weeks"] = list(missed[0] or []) if missed else []
        if espn_id:
            fp = con.execute(
                """SELECT percent_owned, percent_started, percent_change, injury_status, detail_synced_at
                   FROM fantasy_players WHERE espn_player_id = ?""", [int(espn_id)]).fetchone()
            if fp:
                src = "fantasy.duckdb:fantasy_players (ESPN)"
                as_of = fp[4].strftime("%Y-%m-%d") if fp[4] else cut.label
                if fp[0] is not None:
                    stats.append(_stat("ESPN rostered %", fp[0], src, as_of))
                if fp[2] is not None:
                    stats.append(_stat("ESPN rostered % change (7 days)", fp[2], src, as_of))
                out["espn_injury_status"] = fp[3]
            proj = con.execute(
                """SELECT week, projected_points, interval_low, interval_high, model, coverage_status, opponent
                   FROM independent_player_projections WHERE season = ? AND espn_player_id = ? AND week = ?""",
                [cut.season, int(espn_id), cut.through_week + 1]).fetchone()
            if proj and proj[1] is not None:
                src = f"fantasy.duckdb:independent_player_projections season={cut.season} week={proj[0]} model={proj[4]}"
                stats.append(_stat(f"our week {proj[0]} projection (PPR)", proj[1], src, cut.label))
                if proj[2] is not None and proj[3] is not None:
                    stats.append(_stat(f"week {proj[0]} 80% range", f"{proj[2]:.1f}-{proj[3]:.1f}", src, cut.label))
                out["projection"] = {"week": proj[0], "points": proj[1], "low": proj[2], "high": proj[3],
                                     "coverage": proj[5], "opponent": proj[6]}
            ros = con.execute(
                """SELECT as_of_week, current_week_projection, ros_projected_points FROM player_ros_projections
                   WHERE season = ? AND espn_player_id = ? AND as_of_week <= ? ORDER BY as_of_week DESC LIMIT 1""",
                [cut.season, int(espn_id), cut.through_week + 1]).fetchone()
            if ros and ros[2] is not None:
                src = f"fantasy.duckdb:player_ros_projections season={cut.season} as_of_week={ros[0]} (ESPN)"
                stats.append(_stat("ESPN rest-of-season projection (points)", ros[2], src, cut.label))
        if team:
            tc = con.execute(
                """SELECT next_week, next_opponent, next_implied_total FROM team_context
                   WHERE season = ? AND team = ? AND through_week <= ?""",
                [cut.season, team, cut.through_week]).fetchone()
            if tc and tc[1]:
                out["next_game"] = {"week": tc[0], "opponent": tc[1], "implied_total": tc[2]}
                dc = con.execute("SELECT ranks, through_week FROM defense_context WHERE season = ? AND defteam = ?",
                                 [cut.season, tc[1]]).fetchone()
                if dc and dc[0]:
                    ranks = json.loads(dc[0])
                    out["opponent_defense_ranks"] = ranks
                    key = "rush_epa_allowed" if pos == "RB" else "pass_epa_allowed"
                    if key in ranks:
                        stats.append(_stat(f"week {tc[0]} opponent {tc[1]} rank in {key.replace('_', ' ')} (1 = most allowed)",
                                           ranks[key], f"fantasy.duckdb:defense_context season={cut.season} through_week={dc[1]}",
                                           cut.label))
                if tc[2] is not None:
                    stats.append(_stat(f"{team} implied points, week {tc[0]}", tc[2], "fantasy.duckdb:team_context (Vegas lines)", cut.label))
        return out

    def teammates(self, team: str, position: str, exclude: str) -> list[dict[str, Any]]:
        """Same-position teammates and their recent share: who absorbs the work if he sits."""
        if not self.con or not self.cut:
            return []
        metric = LEAD_METRIC.get(position, "offense_pct")
        if metric == "fantasy_points_ppr":
            metric = "offense_pct"
        rows = self.con.execute(
            f"""SELECT u.gsis_id, any_value(r.full_name), avg(u.{metric}), avg(u.offense_pct)
                FROM player_week_usage u
                LEFT JOIN (SELECT gsis_id, arg_max(full_name, week) AS full_name FROM nfl_rosters_weekly
                           WHERE season = ? GROUP BY 1) r USING (gsis_id)
                WHERE u.season = ? AND u.team = ? AND u.position = ? AND u.gsis_id <> ? AND u.week > ? - 2
                  AND u.week <= ?
                GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 3""",
            [self.cut.season, self.cut.season, team, position, exclude, self.cut.through_week,
             self.cut.through_week]).fetchall()
        return [{"gsis_id": r[0], "name": r[1], "metric": METRIC_LABEL.get(metric, metric),
                 "recent": round(r[2], 3) if r[2] is not None else None,
                 "snap_share": round(r[3], 3) if r[3] is not None else None,
                 "source": f"fantasy.duckdb:player_week_usage season={self.cut.season} last 2 weeks"} for r in rows]

    # ------------------------------------------------------------------ cluster level

    def enrich(self, player_ids: list[str], topics: list[str], stance: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.con or not self.cut:
            return {"analytic_coverage": 0.0, "analytic_note": "analytics database unavailable", "players": []}
        players = [p for pid in player_ids[:3] if (p := self.player(pid))]
        social_dir = social_direction(topics, stance)
        verdicts = []
        for p in players:
            d = p.get("data_direction")
            if not d:
                continue
            agrees = (social_dir == "positive" and d == "up") or (social_dir == "negative" and d == "down")
            disagrees = (social_dir == "positive" and d == "down") or (social_dir == "negative" and d == "up")
            verdicts.append({"player": p["name"], "social": social_dir, "data": d,
                             "relation": "supports" if agrees else "contradicts" if disagrees else "neutral"})
            if "injury" in topics and p.get("team") and p.get("position"):
                p["teammates"] = self.teammates(p["team"], p["position"], p["gsis_id"])
        n_stats = sum(len(p["stats"]) for p in players)
        coverage = 0.0
        if players:
            coverage = min(1.0, 0.35 + 0.05 * n_stats)
            if any(v["relation"] == "contradicts" for v in verdicts):
                coverage = min(1.0, coverage + 0.15)  # contrarian, data-backed: the best kind of video
        note = (f"{len(players)} player(s), {n_stats} sourced stats through {self.cut.label}"
                + (f"; data {verdicts[0]['relation']} the take on {verdicts[0]['player']}" if verdicts else ""))
        return {"analytic_coverage": round(coverage, 2), "analytic_note": note, "players": players,
                "social_direction": social_dir, "verdicts": verdicts, "data_cut": self.cut.label,
                "upcoming_week": self.upcoming_week()}
