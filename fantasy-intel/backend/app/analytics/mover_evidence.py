"""Data recorded alongside a projection move: what changed between two stored forecasts, per mover.

Read-only, SQL + stdlib only (the hosted API has no polars/numpy), and nothing here writes. For each
mover of the projection-movers page it lists the recorded facts that changed over the same period as
the forecast did, each with before/after values, the weeks it covers and the table it came from. It
never says why a forecast moved: the wording is "recorded alongside", never a cause, and a mover with
no recorded change simply has no headline and a `missing` list of what was checked.

Information window = [window_from, window_to) = [earlier snapshot taken_at, latest snapshot taken_at).
  * NFL game data (usage, injury report, weekly roster) is read only for games with a final score that
    kicked off inside the window at least GAME_LENGTH before window_to, so the game was over when the
    latest forecast was made ("after"), and for the player's last games that kicked off before it
    ("before": up to PRIOR_GAMES games). A row whose team-week kickoff is unknown, at or after
    window_to, or in a week of the compared window is never read.
  * Depth charts are timestamped snapshots: the latest at or before each snapshot time is compared.
  * ESPN's injury designation is read only when it was refreshed inside the window. ESPN keeps no history, so it
    is the status at that check, not a change: informational, never the headline (nor is an injury-report
    designation with no earlier report to compare with).
  * Ours only: model components and per-week coverage changes come from the mover itself, the
    opponent of each target week from the two runs' stored rows.

Signals (thresholds below, fixed): usage (snap share 5 pts, target share 3 pts, carries/game 2,
backfield rush share 8 pts, route participation 8 pts, xFP/game 2), availability (injury report
designation / practice, weekly roster status, ESPN designation), role (team, depth chart rank),
schedule (ours: opponent differs between the two runs; a bye inside the window), model (ours).
The headline is the strongest signal: availability, then role, then usage (largest change relative to
its threshold), then schedule, then model. The number of queries is constant, whatever the mover count.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple

from ..db.connection import fetch_dicts, table_columns, table_exists
from . import production_forecasts as pf
from .player_availability import RULE_CODES
from .projection_backtest import _kickoff, _utc

UTC = timezone.utc
INDEPENDENT = "independent"      # projection_source.SOURCES: the opt-in source with model components
NOTE = "Observed over the same period as the change. Not shown to be the cause."
PRIOR_GAMES = 4                  # the "before" sample: the player's last games before the earlier snapshot
HIGH_CONFIDENCE = "high"         # the only player_id_map confidence the evidence reads
COMPONENT_MIN = 0.05             # |component| shown (projection movers' "flat" threshold)
SYNC_TOLERANCE = timedelta(seconds=1)   # snapshot times may be stored truncated to whole seconds
GAME_LENGTH = timedelta(hours=3, minutes=30)   # a window game counts once it kicked off this long before window_to

# key, label, player_week_usage column, positions, threshold, unit, source
USAGE = (
    ("snap_share", "Snap share", "offense_pct", ("QB", "RB", "WR", "TE"), 0.05, "share", "nflverse snap counts"),
    ("target_share", "Target share", "target_share", ("RB", "WR", "TE"), 0.03, "share", "nflverse play-by-play"),
    ("carries", "Carries per game", "carries", ("RB",), 2.0, "count", "nflverse player stats"),
    ("backfield_share", "Backfield rush share", "backfield_rush_share", ("RB",), 0.08, "share", "nflverse play-by-play"),
    ("route_participation", "Route participation", "route_participation", ("RB", "WR", "TE"), 0.08, "share",
     "external route data"),
    ("xfp", "Expected points (xFP)", "xfp", ("QB", "RB", "WR", "TE"), 2.0, "points", "nflverse expected fantasy points"),
)
MISSING_NAME = {"snap_share": "snap", "target_share": "target", "carries": "carry", "backfield_share": "backfield",
                "route_participation": "route", "xfp": "expected-points (xFP)"}

# Headline order: lower group first, then the larger strength within it.
G_AVAILABILITY, G_ROLE, G_USAGE, G_SCHEDULE, G_MODEL, G_INFO = range(6)

ROSTER_LABEL = {"ACT": "Active", "INA": "Inactive", "RES": "Reserve/injured", "CUT": "Released", "DEV": "Practice squad",
                "RET": "Retired", "EXE": "Exempt list", "TRT": "Reserve/other", "TRD": "Traded"}
ROSTER_STRENGTH = {"RES": 5.0, "CUT": 4.5, "INA": 4.0, "EXE": 4.0, "RET": 4.0}
DESIGNATION_STRENGTH = {"Out": 5.0, "Doubtful": 4.0, "Questionable": 3.0}
ESPN_LABEL = {"OUT": "Out", "DOUBTFUL": "Doubtful", "QUESTIONABLE": "Questionable", "INJURY_RESERVE": "Injured reserve",
              "DAY_TO_DAY": "Day-to-day", "SUSPENSION": "Suspended"}
ESPN_STRENGTH = {"INJURY_RESERVE": 5.0, "OUT": 4.5, "SUSPENSION": 4.5, "DOUBTFUL": 3.5, "QUESTIONABLE": 2.5, "DAY_TO_DAY": 2.0}
PRACTICE = (("did not participate", "did not practice"), ("limited", "limited practice"), ("full", "full practice"))
COVERAGE_TEXT = {"projected": "projected", "withheld_unavailable": "withheld (unavailable)", "bye": "bye week",
                 "abstain_no_history": "no estimate (no history)"}


class Game(NamedTuple):
    week: int
    team: str
    opponent: str
    kickoff: datetime | None
    final: bool


# ----------------------------------------------------------------------------- small helpers


def _aware(value) -> datetime | None:
    return _utc(value)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _weeks(ws: Iterable[int]) -> str:
    ws = sorted(set(ws))
    if len(ws) > 1 and ws == list(range(ws[0], ws[-1] + 1)):
        return f"weeks {ws[0]}-{ws[-1]}"
    return ("week " if len(ws) == 1 else "weeks ") + ", ".join(map(str, ws))


def _day(dt: datetime) -> str:
    return f"{dt.strftime('%b')} {dt.day}"


def _stamp(dt: datetime) -> str:
    return f"{_day(dt)} {dt:%H:%M} UTC"


def _in_list(n: int) -> str:
    return ", ".join("?" * n)


def _fmt(unit: str, v: float) -> str:
    if unit == "share":
        return f"{round(v * 100):.0f}%"
    return f"{v:.1f}"


def _signed(v: float) -> str:
    return f"{v:+.1f}"


def _practice(text: str | None) -> str | None:
    t = (text or "").lower()
    for needle, label in PRACTICE:
        if needle in t:
            return label
    return None


def _designation(text) -> str | None:
    t = str(text).strip().title() if text else ""
    return t or None


def _clean(sig: dict) -> dict:
    return {k: v for k, v in sig.items() if not k.startswith("_")}


def _signal(group: int, strength: float, headline: str, **fields) -> dict:
    fields.setdefault("weeks_before", [])
    fields.setdefault("weeks_after", [])
    return {"_group": group, "_strength": strength, "_headline": headline, **fields}


# ----------------------------------------------------------------------------- batched reads


def _games(con, season: int) -> dict[tuple[int, str], Game]:
    """(week, team) -> Game for the REG schedule. `final` = both scores present. Unknown kickoff = None."""
    if not table_exists(con, "nfl_schedules"):
        return {}
    cols = set(table_columns(con, "nfl_schedules"))
    if not {"week", "gameday", "gametime", "home_team", "away_team"} <= cols:
        return {}
    scores = "home_score, away_score" if {"home_score", "away_score"} <= cols else "NULL AS home_score, NULL AS away_score"
    kind = " AND game_type = 'REG'" if "game_type" in cols else ""
    out: dict[tuple[int, str], Game] = {}
    for g in fetch_dicts(con, f"SELECT week, gameday, gametime, home_team, away_team, {scores} FROM nfl_schedules "
                              f"WHERE season = ?{kind}", [season]):
        ko = _kickoff(g)
        final = g["home_score"] is not None and g["away_score"] is not None
        week = int(g["week"])
        out[(week, g["home_team"])] = Game(week, g["home_team"], g["away_team"], ko, final)
        out[(week, g["away_team"])] = Game(week, g["away_team"], g["home_team"], ko, final)
    return out


def _gsis_ids(con, espn_ids: list[int]) -> dict[int, str]:
    """espn_player_id -> gsis_id for high-confidence links only (the rule participation and the forecasts use)."""
    if not espn_ids or not table_exists(con, "player_id_map"):
        return {}
    rows = fetch_dicts(con, f"SELECT espn_player_id, gsis_id FROM player_id_map WHERE match_confidence = ? "
                            f"AND gsis_id IS NOT NULL AND espn_player_id IN ({_in_list(len(espn_ids))})",
                       [HIGH_CONFIDENCE, *espn_ids])
    return {int(r["espn_player_id"]): r["gsis_id"] for r in rows}


def _usage_rows(con, season: int, gsis: list[str], max_week: int) -> dict[str, list[dict]]:
    if not gsis or not table_exists(con, "player_week_usage"):
        return {}
    have = set(table_columns(con, "player_week_usage"))
    cols = ", ".join(c if c in have else f"NULL AS {c}" for c in
                     ("team", "offense_pct", "target_share", "carries", "backfield_rush_share", "route_participation", "xfp"))
    out: dict[str, list[dict]] = defaultdict(list)
    for r in fetch_dicts(con, f"SELECT gsis_id, week, {cols} FROM player_week_usage WHERE season = ? AND week <= ? "
                              f"AND gsis_id IN ({_in_list(len(gsis))})", [season, max_week, *gsis]):
        out[r["gsis_id"]].append(r)
    return out


def _injury_rows(con, season: int, gsis: list[str], max_week: int, until: datetime):
    """(rows by gsis_id, {(team, week)} with any report row). `date_modified`, when the table has it, must precede `until`."""
    if not table_exists(con, "nfl_injuries"):
        return {}, set()
    have = set(table_columns(con, "nfl_injuries"))
    needed = {"week", "team", "gsis_id", "report_status", "practice_status"}
    if not needed <= have:
        return {}, set()
    where = "season = ? AND week <= ?"
    params: list = [season, max_week]
    if "game_type" in have:
        where += " AND (game_type = 'REG' OR game_type IS NULL)"
    late, cutoff = "FALSE", []
    if "date_modified" in have:       # a row the provider modified at/after the window end was not known inside it
        late, cutoff = "coalesce(date_modified >= ?, FALSE)", [until.astimezone(UTC).replace(tzinfo=None)]
    reported = {(r["team"], int(r["week"])) for r in fetch_dicts(
        con, f"SELECT team, week FROM nfl_injuries WHERE {where} GROUP BY team, week HAVING NOT bool_and({late})",
        [*params, *cutoff])}
    rows: dict[str, list[dict]] = defaultdict(list)
    if gsis:
        for r in fetch_dicts(con, f"SELECT gsis_id, week, team, report_status, practice_status, {late} AS late "
                                  f"FROM nfl_injuries WHERE {where} AND gsis_id IN ({_in_list(len(gsis))})",
                             [*cutoff, *params, *gsis]):
            rows[r["gsis_id"]].append(r)
    return rows, reported


def _roster_rows(con, season: int, gsis: list[str], max_week: int) -> dict[str, list[dict]]:
    if not gsis or not table_exists(con, "nfl_rosters_weekly"):
        return {}
    have = set(table_columns(con, "nfl_rosters_weekly"))
    if not {"week", "team", "gsis_id", "status"} <= have:
        return {}
    abbr = "status_description_abbr" if "status_description_abbr" in have else "NULL"
    kind = " AND (game_type = 'REG' OR game_type IS NULL)" if "game_type" in have else ""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in fetch_dicts(con, f"SELECT gsis_id, week, team, status, {abbr} AS abbr FROM nfl_rosters_weekly "
                              f"WHERE season = ? AND week <= ?{kind} AND gsis_id IN ({_in_list(len(gsis))})",
                         [season, max_week, *gsis]):
        out[r["gsis_id"]].append(r)
    return out


def _depth_rows(con, season: int, gsis: list[str], start: datetime, end: datetime) -> dict[str, dict[str, dict]]:
    """gsis_id -> {"before": {...}, "after": {...}}: for each position the latest depth-chart row with dt at or before
    each snapshot time (ISO-8601 'Z' text compares correctly as a string; other formats never match)."""
    if not gsis or not table_exists(con, "nfl_depth_charts"):
        return {}
    have = set(table_columns(con, "nfl_depth_charts"))
    if not {"dt", "team", "gsis_id", "pos_abb", "pos_rank", "season"} <= have:
        return {}
    cut = lambda t: t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    ids = _in_list(len(gsis))
    sql = f"""
        SELECT side, gsis_id, pos_abb, team, pos_rank, dt FROM (
            SELECT 'before' AS side, gsis_id, pos_abb, team, pos_rank, dt, row_number() OVER (
                PARTITION BY gsis_id, pos_abb ORDER BY dt DESC, pos_rank) AS rn
            FROM nfl_depth_charts WHERE season = ? AND gsis_id IN ({ids}) AND dt <= ?
              AND dt GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'
            UNION ALL
            SELECT 'after', gsis_id, pos_abb, team, pos_rank, dt, row_number() OVER (
                PARTITION BY gsis_id, pos_abb ORDER BY dt DESC, pos_rank)
            FROM nfl_depth_charts WHERE season = ? AND gsis_id IN ({ids}) AND dt <= ?
              AND dt GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'
        ) WHERE rn = 1"""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in fetch_dicts(con, sql, [season, *gsis, cut(start), season, *gsis, cut(end)]):
        out[r["gsis_id"]][f"{r['side']}:{r['pos_abb']}"] = r
    return out


def _espn_status(con, espn_ids: list[int]) -> dict[int, dict]:
    if not espn_ids or not table_exists(con, "fantasy_players"):
        return {}
    have = set(table_columns(con, "fantasy_players"))
    if not {"injury_status", "detail_synced_at"} <= have:
        return {}
    return {int(r["espn_player_id"]): r for r in fetch_dicts(
        con, f"SELECT espn_player_id, injury_status, detail_synced_at FROM fantasy_players "
             f"WHERE espn_player_id IN ({_in_list(len(espn_ids))})", espn_ids)}


def _run_map(run_ids) -> dict[int, str]:
    """{week: run_id} from a dict (int or str week keys) or a list of {"week", "run_id"}."""
    out: dict[int, str] = {}
    if isinstance(run_ids, dict):
        pairs = run_ids.items()
    elif isinstance(run_ids, (list, tuple)):
        pairs = [(r.get("week"), r.get("run_id")) for r in run_ids if isinstance(r, dict)]
    else:
        return out
    for w, rid in pairs:
        try:
            if rid:
                out[int(w)] = str(rid)
        except (TypeError, ValueError):
            continue
    return out


def _opponents(con, espn_ids: list[int], before: dict[int, str], after: dict[int, str]) -> dict[tuple[int, int], tuple]:
    """(espn_player_id, week) -> (opponent in the earlier run, opponent in the latest run)."""
    runs = sorted(set(before.values()) | set(after.values()))
    if not runs or not espn_ids or pf.rows_source(con) != pf.ROWS:      # the run-keyed store only (not the v7 layout)
        return {}
    got: dict[tuple[str, int], str | None] = {}
    for r in fetch_dicts(con, f"SELECT run_id, espn_player_id, opponent FROM {pf.ROWS} "
                              f"WHERE run_id IN ({_in_list(len(runs))}) AND espn_player_id IN ({_in_list(len(espn_ids))})",
                         [*runs, *espn_ids]):
        got[(r["run_id"], int(r["espn_player_id"]))] = r["opponent"]
    out = {}
    for pid in espn_ids:
        for week in set(before) & set(after):
            b, a = got.get((before[week], pid)), got.get((after[week], pid))
            if b and a:
                out[(pid, week)] = (b, a)
    return out


# ----------------------------------------------------------------------------- per-player signals


def _usage_changes(position: str, prior: list[dict], now: list[dict], now_label: str, missing: list[str]) -> list[dict]:
    """Prior = the player's last games before the window, now = his games inside it (both gated by the caller)."""
    out = []
    for key, label, col, positions, threshold, unit, source in USAGE:
        if position not in positions:
            continue
        before = [(r["week"], _number(r[col])) for r in prior if _number(r[col]) is not None]
        after = [(r["week"], _number(r[col])) for r in now if _number(r[col]) is not None]
        if not before or not after:
            if before and not after:
                missing.append(f"No {MISSING_NAME[key]} data for {now_label} yet")
            elif after and not before:
                missing.append(f"No earlier {MISSING_NAME[key]} data to compare with")
            continue
        b = sum(v for _, v in before) / len(before)
        a = sum(v for _, v in after) / len(after)
        change = a - b
        if round(abs(change), 9) < threshold:
            continue
        wb, wa = [w for w, _ in before], [w for w, _ in after]
        direction = "up" if change > 0 else "down"
        verb = "averaged" if len(wa) > 1 else "was"
        out.append(_signal(
            G_USAGE, abs(change) / threshold,
            f"{label} {'averaged ' if len(wa) > 1 else ''}{_fmt(unit, a)} in {_weeks(wa)}, {direction} from {_fmt(unit, b)}",
            kind="usage", key=key, label=label, before=round(b, 4), after=round(a, 4), unit=unit,
            weeks_before=wb, weeks_after=wa, source=source,
            detail=(f"{label} {verb} {_fmt(unit, a)} in {_weeks(wa)}, against an average of {_fmt(unit, b)} "
                    f"over {_weeks(wb)} (recorded, per game played).")))
    return out


def _roster_text(row: dict) -> str:
    status = (row.get("status") or "").upper()
    if status == "RES" and row.get("abbr") in RULE_CODES:
        return RULE_CODES[row["abbr"]][1]
    return ROSTER_LABEL.get(status, status or "Unknown")


class _Window:
    """Everything an evidence run needs about the window, shared by all movers."""

    def __init__(self, games, start, end, window_weeks):
        self.games, self.start, self.end, self.weeks = games, start, end, set(window_weeks)

    def readable(self, week, team) -> Game | None:
        """The game a (week, team) row belongs to, only when it may be read at all (known kickoff before the end)."""
        g = self.games.get((int(week), team))
        if g is None or g.kickoff is None or g.kickoff >= self.end or int(week) in self.weeks:
            return None
        return g

    def inside(self, week, team) -> Game | None:
        g = self.readable(week, team)
        return g if self.finished(g) else None

    def finished(self, g: Game | None) -> bool:
        """A window game that was over (final, and kicked off GAME_LENGTH before the latest snapshot)."""
        return (g is not None and g.final and g.kickoff is not None and g.kickoff >= self.start
                and g.kickoff + GAME_LENGTH <= self.end)

    def before(self, week, team) -> Game | None:
        g = self.readable(week, team)
        return g if g is not None and g.kickoff < self.start else None


def _explain_one(m: dict, ctx: dict) -> dict:
    win: _Window = ctx["win"]
    pid, position, team = int(m["espn_player_id"]), m.get("position"), m.get("nfl_team")
    gsis = ctx["gsis"].get(pid)
    signals: list[dict] = []
    missing: list[str] = []
    games: list[dict] = []

    def add(sig):
        signals.append(sig)

    usage = ctx["usage"].get(gsis, []) if gsis else []
    roster = ctx["roster"].get(gsis, []) if gsis else []
    injuries = {int(r["week"]): r for r in ctx["injuries"].get(gsis, [])} if gsis else {}
    if not gsis:
        missing.append("No high-confidence NFL ID for this player, so no NFL game, injury or depth-chart data was read")
    elif not win.games:
        missing.append("No NFL schedule is stored, so no game could be placed in the window")

    # ---- which of his games fall inside the window, and which came before it
    now_rows = sorted((r for r in usage if win.inside(r["week"], r["team"])), key=lambda r: r["week"])
    prior_rows = sorted((r for r in usage if win.before(r["week"], r["team"])),
                        key=lambda r: win.games[(int(r["week"]), r["team"])].kickoff)[-PRIOR_GAMES:]
    in_games: dict[int, str] = {}                       # week -> team of the window games that count for him
    for r in now_rows:
        in_games[int(r["week"])] = r["team"]
    for r in roster:
        if win.inside(r["week"], r["team"]):
            in_games.setdefault(int(r["week"]), r["team"])
    started_unfinished = []
    if team:
        for (week, t), g in win.games.items():
            if t != team or week in win.weeks or g.kickoff is None or not (win.start <= g.kickoff < win.end):
                continue
            if win.finished(g):
                in_games.setdefault(week, team)
            else:
                started_unfinished.append(week)
    for week in sorted(in_games):
        g = win.games[(week, in_games[week])]
        games.append({"week": week, "team": g.team, "kickoff": _iso(g.kickoff)})

    # ---- usage
    if gsis:
        if not in_games:
            if started_unfinished:
                missing.append(f"The {_weeks(started_unfinished)} game kicked off between the snapshots but was not over by the latest snapshot")
            else:
                missing.append("No completed game between the two snapshots")
        else:
            absent = [w for w in in_games if w not in {int(r["week"]) for r in now_rows}]
            if absent and not now_rows:
                missing.append(f"No usage recorded for {_weeks(absent)} (not in the stored player data)")
            elif now_rows:
                if not prior_rows:
                    missing.append("No earlier games this season to compare usage with")
                else:
                    signals += _usage_changes(position, prior_rows, now_rows, _weeks(r["week"] for r in now_rows), missing)

    # ---- availability: injury report, roster status
    before_anchor = None        # (week, team) of his latest game or roster row before the window
    cands = [(win.games[(int(r["week"]), r["team"])].kickoff, int(r["week"]), r["team"])
             for r in list(roster) + list(prior_rows) if win.before(r["week"], r["team"])]
    if cands:
        _, bw, bt = max(cands)
        before_anchor = (bw, bt)
    if gsis and in_games:
        aw = max(in_games)
        at = in_games[aw]
        reported = ctx["reported"]

        def state(week, tm):
            row = injuries.get(week)
            if row is not None and row["team"] == tm:
                return ("unknown", None) if row["late"] else ("row", row)
            return ("none", None) if (tm, week) in reported else ("unknown", None)

        a_kind, a_row = state(aw, at)
        b_kind, b_row = state(*before_anchor) if before_anchor else ("unknown", None)
        if a_kind == "unknown":
            missing.append(f"No injury report recorded for {at} in week {aw}")
        else:
            a_des = _designation(a_row["report_status"]) if a_row else None
            b_des = _designation(b_row["report_status"]) if b_row else None
            a_pr = _practice(a_row["practice_status"]) if a_row else None
            b_pr = _practice(b_row["practice_status"]) if b_row else None
            bw = before_anchor[0] if before_anchor else None
            when_b = f"week {bw}" if bw else "an earlier week"
            if a_des != b_des:
                known_b = b_kind != "unknown"
                before_txt = (b_des or "not listed") if known_b else "no earlier report stored"
                strength = DESIGNATION_STRENGTH.get(a_des, 2.5 if a_des is None else 3.0)
                if a_des is None:
                    head = f"Not listed on the week {aw} injury report, {b_des} in {when_b}"
                else:
                    head = f"Listed {a_des} on the week {aw} injury report" + (
                        f" ({'not listed' if b_des is None else b_des} in {when_b})" if known_b else "")
                add(_signal(G_AVAILABILITY if known_b else G_INFO, strength, head, kind="availability",
                            key="injury_designation",
                            label="Injury report designation", before=b_des if known_b else None,
                            after=a_des or "Not listed", unit="text", weeks_before=[bw] if bw and known_b else [],
                            weeks_after=[aw], source="nflverse injury reports",
                            detail=(f"The week {aw} injury report lists {a_des or 'no designation'}; "
                                    f"the {when_b} report: {before_txt}. Recorded designation only.")))
            elif a_pr != b_pr and "did not practice" in (a_pr, b_pr) and b_kind != "unknown":
                add(_signal(G_AVAILABILITY, 1.0, f"Practice status {a_pr or 'not listed'} in week {aw}, "
                            f"{b_pr or 'not listed'} in {when_b}", kind="availability", key="practice_status",
                            label="Practice status", before=b_pr, after=a_pr, unit="text",
                            weeks_before=[bw] if bw else [], weeks_after=[aw], source="nflverse injury reports",
                            detail=f"Practice participation was {a_pr or 'not listed'} in week {aw} and "
                                   f"{b_pr or 'not listed'} in {when_b}."))
        # roster status
        r_after = max((r for r in roster if win.inside(r["week"], r["team"])), key=lambda r: int(r["week"]), default=None)
        r_before = None
        if before_anchor:
            r_before = next((r for r in roster if (int(r["week"]), r["team"]) == before_anchor), None)
        if r_after and r_before:
            ta, tb = _roster_text(r_after), _roster_text(r_before)
            if ta != tb:
                wa, wb = int(r_after["week"]), int(r_before["week"])
                code = (r_after.get("status") or "").upper()
                add(_signal(G_AVAILABILITY, ROSTER_STRENGTH.get(code, 3.0),
                            f"Roster status {ta} in week {wa}, {tb} in week {wb}", kind="availability",
                            key="roster_status", label="Roster status", before=tb, after=ta, unit="text",
                            weeks_before=[wb], weeks_after=[wa], source="nflverse weekly rosters",
                            detail=f"The weekly roster lists {ta} for week {wa} and {tb} for week {wb}."))
        elif not r_after:
            missing.append(f"No weekly roster row for week {aw}")
    espn = ctx["espn"].get(pid)
    if espn:
        synced = _aware(espn["detail_synced_at"])
        status = str(espn["injury_status"] or "").upper()
        if synced is not None and win.start <= synced <= win.end + SYNC_TOLERANCE:
            label = ESPN_LABEL.get(status)
            if label:
                add(_signal(G_INFO, ESPN_STRENGTH.get(status, 2.0), f"ESPN injury status at the latest check: {label}",
                            kind="availability", key="espn_injury_status", label="ESPN injury status", before=None,
                            after=label, unit="text", source="ESPN player status",
                            detail=f"ESPN listed this player as {label} when last checked on {_stamp(synced)}; "
                                   "no earlier ESPN status is stored, so the previous value is not shown."))
        elif synced is not None and synced > win.end + SYNC_TOLERANCE:
            missing.append("ESPN injury status was refreshed after the latest snapshot, so it was not read")
    if gsis and in_games and not any(s["kind"] == "availability" and s["_group"] != G_INFO for s in signals):
        missing.append("No injury-report or roster-status change recorded between the snapshots")

    # ---- role: team and depth chart
    depth = ctx["depth"].get(gsis, {}) if gsis else {}
    d_before, d_after = depth.get(f"before:{position}"), depth.get(f"after:{position}")
    team_pairs = []
    if d_before and d_after:
        team_pairs.append((d_before["team"], d_after["team"], f"the {_stamp(_depth_time(d_before))} and "
                           f"{_stamp(_depth_time(d_after))} depth charts", "nflverse depth charts"))
    if before_anchor and in_games:
        r_b = next((r for r in roster if (int(r["week"]), r["team"]) == before_anchor), None)
        r_a = max((r for r in roster if win.inside(r["week"], r["team"])), key=lambda r: int(r["week"]), default=None)
        if r_a and r_b:
            team_pairs.append((r_b["team"], r_a["team"], f"the week {r_b['week']} and week {r_a['week']} rosters",
                               "nflverse weekly rosters"))
    if prior_rows and now_rows:
        team_pairs.append((prior_rows[-1]["team"], now_rows[-1]["team"],
                           f"games in week {prior_rows[-1]['week']} and week {now_rows[-1]['week']}", "nflverse player stats"))
    for tb, ta, where, source in team_pairs:
        if tb and ta and tb != ta:
            add(_signal(G_ROLE, 5.0, f"Listed with {ta}, previously {tb}", kind="role", key="team", label="Team",
                        before=tb, after=ta, unit="text", source=source,
                        detail=f"Recorded with {tb} and then with {ta} across {where}."))
            break
    if d_before and d_after and d_before["pos_rank"] is not None and d_after["pos_rank"] is not None:
        rb, ra = int(d_before["pos_rank"]), int(d_after["pos_rank"])
        if rb != ra:
            tb, ta = _depth_time(d_before), _depth_time(d_after)
            add(_signal(G_ROLE, 3.0 + abs(ra - rb) / 10, f"Depth chart {position}{ra} on {_day(ta)}, "
                        f"{'up' if ra < rb else 'down'} from {position}{rb} on {_day(tb)}", kind="role", key="depth_rank",
                        label="Depth chart rank", before=rb, after=ra, unit="count", source="nflverse depth charts",
                        detail=f"Listed {position}{rb} in the {_stamp(tb)} depth chart and {position}{ra} in the "
                               f"{_stamp(ta)} one (1 is the top of the chart)."))
    elif gsis and not (d_before and d_after):
        missing.append(f"Depth chart rank not compared: no {position} entry in both snapshots")

    # ---- schedule (a bye is shown for both sources; the opponent comparison is ours only)
    byes = sorted(w["week"] for w in m.get("weeks") or [] if w.get("bye"))
    # A bye in only one forecast (a trade between the two): ours already reports it as a coverage-status change.
    one_sided = {side: sorted(w["week"] for w in m.get("weeks") or []
                              if w.get(f"bye_{side}") and not w.get(f"bye_{'after' if side == 'before' else 'before'}"))
                 for side in ("before", "after")}
    if ctx["ours"]:
        changed = [(w["week"], *ctx["opponents"][(pid, w["week"])]) for w in m.get("weeks") or []
                   if (pid, w["week"]) in ctx["opponents"] and ctx["opponents"][(pid, w["week"])][0] != ctx["opponents"][(pid, w["week"])][1]]
        for week, ob, oa in changed:
            add(_signal(G_SCHEDULE, 2.0, f"Week {week} opponent {oa} in the latest run, {ob} in the earlier run",
                        kind="schedule", key="opponent", label="Opponent", before=ob, after=oa, unit="text",
                        weeks_before=[week], weeks_after=[week], source="stored forecast runs",
                        detail=f"The stored forecast for week {week} lists {oa} as the opponent in the latest run "
                               f"and {ob} in the earlier run."))
        if not ctx["opponents"] and ctx["run_ids"]:
            missing.append("Opponents could not be compared between the two forecast runs")
    if byes:
        add(_signal(G_INFO, 0.0, f"{_weeks(byes).capitalize()} {'is a bye' if len(byes) == 1 else 'are byes'}", kind="schedule",
                    key="bye_week", label="Bye week", before=None, after=byes[0], unit="count", weeks_after=byes,
                    source="NFL schedule",
                    detail=f"{_weeks(byes).capitalize()} {'is a bye' if len(byes) == 1 else 'are byes'}, counted as "
                           "0 points in both forecasts."))
    if not ctx["ours"]:
        for side, which, other in (("before", "earlier", "latest"), ("after", "latest", "earlier")):
            ws = one_sided[side]
            if ws:
                head = f"{_weeks(ws).capitalize()}: a bye in the {which} snapshot only"
                add(_signal(G_SCHEDULE, sum(abs(_number(w.get("delta")) or 0.0) for w in m["weeks"] if w["week"] in ws),
                            head, kind="schedule", key=f"bye_{side}_only", label="Bye week", before=None, after=None,
                            unit="text", weeks_before=ws if side == "before" else [], weeks_after=ws if side == "after" else [],
                            source="NFL schedule",
                            detail=f"{_weeks(ws).capitalize()} counts 0 points as a bye in the {which} snapshot, while "
                                   f"the {other} snapshot has a projection for the player that week."))

    # ---- model (ours only)
    if ctx["ours"]:
        comps = m.get("components") or {}
        for key, label, head, text in (
                ("availability", "Availability (chance of appearing)",
                 "Availability (chance of appearing) moved the total by {v}", "Availability (the model's chance of appearing) moved the total by {v}."),
                ("per_game", "Per-game outlook", "Per-game outlook moved the total by {v}",
                 "Per-game outlook moved the total by {v}.")):
            v = _number(comps.get(key))
            if v is not None and abs(v) >= COMPONENT_MIN:
                add(_signal(G_MODEL, abs(v), head.format(v=_signed(v)), kind="model", key=f"{key}_component", label=label,
                            before=None, after=round(v, 2), unit="points", source="Fantasy Intel projection components",
                            detail=text.format(v=_signed(v))))
        shifts: dict[tuple, list] = defaultdict(list)
        for w in m.get("weeks") or []:
            sb, sa = w.get("status_before"), w.get("status_after")
            if sb and sa and sb != sa:
                shifts[(sb, sa)].append(w)
        for (sb, sa), ws in sorted(shifts.items()):
            weeks = sorted(w["week"] for w in ws)
            tb, ta = COVERAGE_TEXT.get(sb, sb.replace("_", " ")), COVERAGE_TEXT.get(sa, sa.replace("_", " "))
            add(_signal(G_MODEL, sum(abs(_number(w.get("delta")) or 0.0) for w in ws),
                        f"{_weeks(weeks).capitalize()}: {ta} in the latest run, {tb} in the earlier run",
                        kind="model", key="coverage_status", label="Model coverage", before=tb, after=ta, unit="text",
                        weeks_before=weeks, weeks_after=weeks, source="Fantasy Intel projection runs",
                        detail=f"For {_weeks(weeks)} the earlier run was {tb} and the latest run is {ta}."))

    signals.sort(key=lambda s: (s["_group"], -s["_strength"], s["key"]))
    return {
        # A bye inside the window is in both snapshots, so it did not change alongside the move: never the headline.
        "headline": next((s["_headline"] for s in signals if s["_group"] != G_INFO), None),
        "signals": [_clean(s) for s in signals],
        "missing": list(dict.fromkeys(missing)),
        "information_window": {"from": _iso(win.start), "to": _iso(win.end), "games": games},
        "note": NOTE,
    }


def _depth_time(row: dict) -> datetime:
    return _aware(row["dt"])


# ----------------------------------------------------------------------------- public API


def explain(con, season: int, movers: list[dict], *, window_from, window_to, source: str | None,
            window_weeks: Iterable[int] = (), baseline_run_ids=None, current_run_ids=None) -> dict[int, dict]:
    """Evidence per mover: {espn_player_id: Evidence}. `window_from`/`window_to` are the two snapshots' taken_at
    (aware datetimes or ISO strings). `window_weeks`: the compared target weeks (never read). The run id maps
    (ours; {week: run_id}) enable the opponent comparison."""
    start, end = _aware(window_from), _aware(window_to)
    if start is None or end is None or end < start:
        raise ValueError("window_from and window_to must be ISO timestamps with window_from <= window_to")
    movers = [m for m in movers if m.get("espn_player_id") is not None]
    if not movers:
        return {}
    ours = source == INDEPENDENT
    espn_ids = sorted({int(m["espn_player_id"]) for m in movers})
    games = _games(con, season)
    win = _Window(games, start, end, [int(w) for w in window_weeks])
    kickoffs = [g.week for g in games.values() if g.kickoff is not None and g.kickoff < end]
    max_week = max(kickoffs) if kickoffs else 0
    gsis_by_espn = _gsis_ids(con, espn_ids)
    gsis = sorted(set(gsis_by_espn.values()))
    injuries, reported = _injury_rows(con, season, gsis, max_week, end) if max_week else ({}, set())
    b_runs, a_runs = _run_map(baseline_run_ids), _run_map(current_run_ids)
    ctx = {
        "win": win, "ours": ours, "gsis": gsis_by_espn,
        "usage": _usage_rows(con, season, gsis, max_week) if max_week else {},
        "roster": _roster_rows(con, season, gsis, max_week) if max_week else {},
        "injuries": injuries, "reported": reported,
        "depth": _depth_rows(con, season, gsis, start, end),
        "espn": _espn_status(con, espn_ids),
        "run_ids": bool(b_runs and a_runs),
        "opponents": _opponents(con, espn_ids, b_runs, a_runs) if ours else {},
    }
    return {int(m["espn_player_id"]): _explain_one(m, ctx) for m in movers}


def attach(con, season: int, result: dict) -> dict:
    """Set `evidence` on every mover in result["risers"] and result["fallers"] (`players` are left untouched).
    An unavailable result is returned unchanged."""
    if not result.get("available"):
        return result
    shown = list(result.get("risers") or []) + list(result.get("fallers") or [])
    if not shown:
        return result
    base, cur = result.get("baseline") or {}, result.get("current") or {}
    start, end = _aware(base.get("taken_at")), _aware(cur.get("taken_at"))
    if start is None or end is None or end < start:     # movement guarantees order; never fail the page over it
        return result
    evidence = explain(con, season, shown, window_from=base.get("taken_at"), window_to=cur.get("taken_at"),
                       source=result.get("source"), window_weeks=(result.get("window") or {}).get("weeks") or (),
                       baseline_run_ids=base.get("run_ids"), current_run_ids=cur.get("run_ids"))
    # The engine builds `players` from the same dicts as the two lists: copy those so `players` keeps evidence = None.
    shared = {id(m) for m in result.get("players") or []}
    for key in ("risers", "fallers"):
        rows = result.get(key) or []
        for i, m in enumerate(rows):
            if m.get("espn_player_id") is None:
                continue
            if id(m) in shared:
                m = rows[i] = dict(m)
            m["evidence"] = evidence[int(m["espn_player_id"])]
    return result
