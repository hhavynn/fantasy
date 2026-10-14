"""Validation layer for the NFL analytics pipeline.

errors   -> genuine logical/data corruption; the analytics run is rolled back.
warnings -> statistically strange but possibly legitimate; logged and shown on /data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from ..db.connection import fetch_dicts, table_exists

EPS = 1e-6


@dataclass
class Issue:
    severity: str          # error | warning
    check_name: str
    detail: str
    count: int = 0
    examples: list = field(default_factory=list)

    def as_row(self) -> dict:
        return asdict(self)


class ValidationError(RuntimeError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.check_name}: {i.detail} ({i.count})" for i in issues if i.severity == "error"))


def _check(rows, pred, severity, name, detail, key=lambda r: r) -> list[Issue]:
    bad = [r for r in rows if pred(r)]
    if not bad:
        return []
    return [Issue(severity, name, detail, len(bad), [key(r) for r in bad[:5]])]


def _out_of_unit(v) -> bool:
    return v is not None and (v < -EPS or v > 1 + EPS)


def _dupes(rows, keys, name) -> list[Issue]:
    c = Counter(tuple(r[k] for k in keys) for r in rows)
    d = [k for k, n in c.items() if n > 1]
    return [Issue("error", name, f"duplicate {keys}", len(d), d[:5])] if d else []


def check_usage(rows: list[dict]) -> list[Issue]:
    k = lambda r: (r["week"], r["gsis_id"])  # noqa: E731
    out = _dupes(rows, ("season", "week", "gsis_id"), "usage_duplicate_keys")
    for col in ("offense_pct", "target_share", "team_rush_share", "backfield_rush_share", "first_read_target_share"):
        out += _check(rows, lambda r, c=col: _out_of_unit(r.get(c)), "error", f"{col}_range", f"{col} outside 0..1", k)
    out += _check(rows, lambda r: r.get("receptions") is not None and r.get("targets") is not None
                  and r["receptions"] > r["targets"], "error", "receptions_gt_targets", "receptions > targets", k)
    out += _check(rows, lambda r: (r.get("broken_tackles") or 0) < 0, "error", "negative_broken_tackles",
                  "broken tackles < 0", k)
    out += _check(rows, lambda r: (r.get("xfp") or 0) < -0.5, "warning", "negative_xfp", "xFP below -0.5", k)
    out += _check(rows, lambda r: r.get("air_yards_share") is not None and not (-0.5 <= r["air_yards_share"] <= 1.0),
                  "warning", "air_yards_share_unusual", "air-yard share outside -0.5..1.0", k)
    out += _check(rows, lambda r: r.get("yac_per_att") is not None and (r.get("carries") or 0) >= 5
                  and (r["yac_per_att"] > 8 or r["yac_per_att"] < -2), "warning", "yac_per_att_extreme",
                  "YAC/att outside -2..8 on 5+ carries", k)
    return out


def check_pbp_player(rows: list[dict]) -> list[Issue]:
    k = lambda r: (r["week"], r["player_id"])  # noqa: E731
    out = _dupes(rows, ("season", "week", "player_id"), "pbp_player_duplicate_keys")
    out += _check(rows, lambda r: r["gl_carries"] > r["rz_carries"], "error", "gl_gt_rz_carries",
                  "goal-line carries > red-zone carries", k)
    out += _check(rows, lambda r: r["rz_carries"] > r["designed_rushes"], "error", "rz_gt_carries",
                  "red-zone carries > designed rushes", k)
    out += _check(rows, lambda r: r["rz_targets"] > r["targets"], "error", "rz_gt_targets",
                  "red-zone targets > targets", k)
    out += _check(rows, lambda r: r["designed_rushes"] > r["rush_attempts"], "error", "designed_gt_rushes",
                  "designed rushes > rush attempts", k)
    out += _check(rows, lambda r: r.get("first_read_targets") is not None
                  and r["first_read_targets"] > r["targets"], "error", "first_read_gt_targets",
                  "first-read targets > targets", k)
    return out


def check_team_week(rows: list[dict]) -> list[Issue]:
    k = lambda r: (r["week"], r["team"])  # noqa: E731
    out = _dupes(rows, ("season", "week", "team"), "team_week_duplicate_keys")
    out += _check(rows, lambda r: r["hit_or_sack"] < r["sacks"] or r["hit_or_sack"] > r["dropbacks"], "error",
                  "hit_or_sack_bounds", "hit-or-sack outside [sacks, dropbacks] (double counting?)", k)
    out += _check(rows, lambda r: _out_of_unit(r.get("sack_rate")) or _out_of_unit(r.get("hit_or_sack_rate")),
                  "error", "pressure_rate_range", "sack or hit-or-sack rate outside 0..1", k)
    out += _check(rows, lambda r: r["team_rb_carries"] > r["team_designed_rushes"], "error",
                  "rb_carries_gt_designed", "RB carries > team designed rushes", k)
    out += _check(rows, lambda r: r.get("proe") is not None and (r.get("proe_plays") or 0) >= 30
                  and abs(r["proe"]) > 25, "warning", "extreme_proe", "|PROE| > 25 pts", k)
    out += _check(rows, lambda r: not (35 <= (r.get("offense_plays") or 0) <= 100), "warning",
                  "unusual_pbp_row_count", "offensive run/pass plays outside 35..100 for a game", k)
    return out


def check_defense_week(rows: list[dict]) -> list[Issue]:
    out = _dupes(rows, ("season", "week", "defteam"), "defense_week_duplicate_keys")
    out += _check(rows, lambda r: r["rb_targets"] + r["wr_targets"] + r["te_targets"] > r["targets"], "error",
                  "position_targets_gt_total", "RB+WR+TE targets allowed > total targets",
                  lambda r: (r["week"], r["defteam"]))
    return out


def check_lines(rows: list[dict]) -> list[Issue]:
    by_game: dict[str, list[dict]] = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(r)
    bad = []
    for gid, rs in by_game.items():
        imps = [r["implied_total"] for r in rs]
        if any(i is None for i in imps):
            continue
        total = rs[0]["total_line"]
        home = next((r for r in rs if r["is_home"]), None)
        if (abs(sum(imps) - total) > EPS or min(imps) < 0 or len(rs) != 2
                or (home and rs[0]["spread_line"] > 0 and home["implied_total"] < max(imps) - EPS)):
            bad.append(gid)
    return [Issue("error", "implied_total_arithmetic", "implied totals inconsistent with total/spread",
                  len(bad), bad[:5])] if bad else []


def check_target_share_sums(rows: list[dict]) -> list[Issue]:
    """rows: (team, week, share_sum). nflverse shares are targets/team_targets -> sum ~ 1."""
    return _check(rows, lambda r: r["share_sum"] is not None and abs(r["share_sum"] - 1.0) > 0.02, "warning",
                  "target_share_sum", "team target shares do not sum to ~1.0", lambda r: (r["team"], r["week"]))


FIRST_READ_MIN_RATE = 0.35   # first reads are normally ~50-60% of charted targets


def check_first_read_rate(first_read: int, charted: int, code: str) -> list[Issue]:
    """Sanity check on FTN code semantics: the first-read code should cover a large share of targets."""
    if not charted:
        return []
    rate = first_read / charted
    if rate < FIRST_READ_MIN_RATE:
        return [Issue("warning", "ftn_first_read_rate", f"only {rate:.0%} of charted targets use first-read code "
                      f"{code!r}; FTN code semantics may have changed", charted)]
    return []


def run_validations(con, season: int, extra: list[Issue] | None = None) -> list[Issue]:
    issues = list(extra or [])
    issues += check_usage(fetch_dicts(con, "SELECT * FROM player_week_usage WHERE season = ?", [season]))
    issues += check_pbp_player(fetch_dicts(con, "SELECT * FROM nfl_pbp_player_week WHERE season = ?", [season]))
    issues += check_team_week(fetch_dicts(con, "SELECT * FROM nfl_team_week WHERE season = ?", [season]))
    issues += check_defense_week(fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ?", [season]))
    issues += check_lines(fetch_dicts(con, "SELECT * FROM team_game_lines WHERE season = ?", [season]))
    if table_exists(con, "nfl_weekly_stats"):
        issues += check_target_share_sums(fetch_dicts(con, """
            SELECT team, week, sum(target_share) AS share_sum FROM nfl_weekly_stats
            WHERE season = ? AND season_type = 'REG' GROUP BY 1, 2""", [season]))
    from ..nfl.sync import FIRST_READ_CODE
    fr, charted = con.execute("""SELECT coalesce(sum(first_read_targets), 0), coalesce(sum(ftn_charted_targets), 0)
                                 FROM nfl_pbp_player_week WHERE season = ?""", [season]).fetchone()
    issues += check_first_read_rate(int(fr), int(charted), FIRST_READ_CODE)
    from .availability import duplicate_injury_reports
    dup, has_dm = duplicate_injury_reports(con, season)
    if dup and not has_dm:
        issues.append(Issue("warning", "injury_duplicates_without_timestamp",
                            "multiple same-week injury rows and no date_modified; tie broken by severity", dup))
    unmatched = con.execute("""SELECT count(*) FROM player_id_map m JOIN fantasy_players fp USING (espn_player_id)
                               WHERE m.match_method = 'unmatched'
                                 AND (fp.availability_status = 'ONTEAM' OR fp.percent_owned >= 1)""").fetchone()[0]
    if unmatched:
        issues.append(Issue("warning", "espn_gsis_unmatched", "rostered/owned ESPN players without an NFL id",
                            unmatched))
    no_ngs = con.execute("""SELECT count(DISTINCT gsis_id) FROM player_week_usage WHERE season = ?
                            AND position IN ('RB','WR','TE') AND opportunities >= 10
                            AND ngs_rush_yoe_per_att IS NULL AND ngs_avg_separation IS NULL""", [season]).fetchone()[0]
    if no_ngs:
        issues.append(Issue("warning", "ngs_missing_rows",
                            "players with 10+ opportunities in a game but no NGS row (unqualified; left NULL)", no_ngs))
    return issues
