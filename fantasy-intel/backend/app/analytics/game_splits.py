"""Game splits: one WR / RB / TE's games with a teammate on the field against games without him (issue #210).

Read-only, deterministic, descriptive. Every input comes from the global nflverse tables the NFL sync already
stores; nothing is fetched, written or estimated, and no league roster, lineup or scoring feeds a number.
Definitions (docs/game_splits.md):

  subject game    a REG game in the window that the player played (an offensive snap, target or carry) with both
                  nflverse xFP and PPR, the research lab's qualifying game. Byes, games he missed and games without
                  xFP are listed in the week drilldown but are not samples.
  eligible        the game is final (a score in nfl_schedules) and, with `as_of`, its game day is before that date.
                  Nothing after the cutoff is read for a week before it: roster statuses are per week, and the
                  teammate's usual snap share (the partial-game reference) uses eligible games only.
  teammates       the pair counts on a team T only when the teammate was on T in an eligible week of the window
                  (a roster row of any status, or an appearance). Subject games for another team are not samples.
  with            the teammate appeared for T in that game (a box-score row or offensive snaps) with a snap record
                  that is not a partial game: below PARTIAL_SHARE_RATIO of his median snap share for T over the
                  eligible window, once that median rests on PARTIAL_REFERENCE_MIN_GAMES games.
  without         confirmed not on the field for T: game-day inactive (INA), a reserve list, the practice squad,
                  suspended or another non-active roster status on T; on another team's roster or playing for
                  another team that week; or a snap record for T showing no offensive snap.
  excluded        everything else is unknown and never "without": listed on two teams in one week, active with no
                  box-score or snap record, appeared without a snap record (a partial game cannot be ruled out), a
                  partial game, no roster record although the team's roster is stored, or no roster data for the
                  team-week. Each is counted by reason.

Per-group metrics reuse the research lab's window summary (sums over the group's games). Differences (without
minus with) appear only when both groups have MIN_GROUP_GAMES games; otherwise the split abstains. No significance
test, probability, confidence number, forecast or causal reading is produced: the groups differ in opponents,
quarterbacks, game scripts and other teammates as well.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import median
from types import MappingProxyType

from ..db.connection import fetch_dicts, table_columns, table_exists
from .player_scores import played
from .research import GSIS_RE, espn_mapping, is_qualifying, load_usage, summarize_window

VERSION = "game-splits.v1"
SUBJECT_POSITIONS = ("WR", "RB", "TE")
TEAMMATE_POSITIONS = ("QB", "RB", "WR", "TE")
FIRST_WEEK, LAST_WEEK = 1, 18            # regular season
MAX_SEASONS = 3                          # a window spans at most three seasons
MIN_GROUP_GAMES = 3                      # fewer games in either group: differences are withheld
SMALL_GROUP_GAMES = 6                    # under this, one game moves a group's mean noticeably (a caution, no number)
PARTIAL_SHARE_RATIO = 0.5                # a game below half his usual snap share is a partial game
PARTIAL_REFERENCE_MIN_GAMES = 3          # games with a snap record needed before the partial check applies
MAX_TEAMMATE_OPTIONS = 60
MAX_PLAYER_OPTIONS = 1500
REQUIRED_TABLES = ("player_week_usage", "nfl_rosters_weekly", "nfl_schedules")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Non-active roster statuses on the subject's team: confirmed not on the field.
ABSENT_STATUS = MappingProxyType({"INA": ("inactive", "Game-day inactive"),
                 "RES": ("reserve", "Reserve list (injured, PUP or NFI)"),
                 "DEV": ("practice_squad", "Practice squad"),
                 "SUS": ("suspended", "Suspended")})

REASONS = MappingProxyType({
    # without
    "inactive": "Game-day inactive",
    "reserve": "Reserve list (injured, PUP or NFI)",
    "practice_squad": "Practice squad",
    "suspended": "Suspended",
    "off_active_roster": "Listed off the active roster",
    "other_team": "On another team that week",
    "no_offensive_snaps": "Snap record shows no offensive snap",
    # excluded: status unknown
    "ambiguous_roster": "Listed on two teams that week",
    "active_no_record": "Active, but no box-score or snap record",
    "snap_record_missing": "Appeared, but no snap record (a partial game cannot be ruled out)",
    "partial_game": "Partial game (well below his usual snap share)",
    "no_roster_record": "No roster record for him that week",
    "roster_data_missing": "No roster data for the team that week",
    # not a sample (subject side)
    "bye": "Bye",
    "no_schedule": "No game in the stored schedule",
    "not_final": "Game not final",
    "after_cutoff": "On or after the cutoff date",
    "subject_did_not_play": "Did not play",
    "subject_metrics_missing": "Played; xFP or PPR not published",
    "not_teammates": "Not teammates on this team in the window",
    "no_record": "No record",
})

METRICS = MappingProxyType({
    "WR": (("snap_share", "Snap share", "pct"), ("routes_pg", "Routes/G", "num"), ("targets_pg", "Targets/G", "num"),
           ("target_share", "Target share", "pct"), ("air_yards_pg", "Air yards/G", "num"),
           ("air_yards_share", "Air-yard share", "pct"), ("adot", "aDOT", "num"), ("xfp_pg", "xFP/G", "num"),
           ("ppr_pg", "PPR/G", "num"), ("fpoe_pg", "FPOE/G", "signed")),
    "TE": (("snap_share", "Snap share", "pct"), ("routes_pg", "Routes/G", "num"), ("targets_pg", "Targets/G", "num"),
           ("target_share", "Target share", "pct"), ("air_yards_pg", "Air yards/G", "num"),
           ("rz_targets_pg", "Red-zone targets/G", "num"), ("xfp_pg", "xFP/G", "num"), ("ppr_pg", "PPR/G", "num"),
           ("fpoe_pg", "FPOE/G", "signed")),
    "RB": (("snap_share", "Snap share", "pct"), ("routes_pg", "Routes/G", "num"),
           ("designed_rushes_pg", "Designed runs/G", "num"), ("backfield_rush_share", "Backfield share", "pct"),
           ("targets_pg", "Targets/G", "num"), ("target_share", "Target share", "pct"),
           ("opportunities_pg", "Carries + targets/G", "num"), ("rz_carries_pg", "Red-zone carries/G", "num"),
           ("xfp_pg", "xFP/G", "num"), ("ppr_pg", "PPR/G", "num"), ("fpoe_pg", "FPOE/G", "signed")),
})

CONFOUNDING = (
    "This is a raw split of past games, not a causal estimate: the groups also differ in opponents, game scripts, "
    "quarterbacks, other injuries and the player's own health.",
    "It is not a forecast. A teammate's return or absence next week need not repeat these numbers.",
    "Groups are small. A single long touchdown or one blowout can move a group's mean by several points.",
)


class SplitsError(ValueError):
    """Invalid game-splits request (HTTP 422)."""


class SplitsNotFound(LookupError):
    """Unknown player or teammate (HTTP 404)."""


@dataclass(frozen=True)
class SplitsQuery:
    player: str
    teammate: str | None
    season_from: int
    week_from: int
    season_to: int
    week_to: int
    as_of: str | None

    @property
    def seasons(self) -> list[int]:
        return list(range(self.season_from, self.season_to + 1))

    def contains(self, season: int, week: int) -> bool:
        return (self.season_from, self.week_from) <= (season, week) <= (self.season_to, self.week_to)

    def cache_key(self) -> str:
        """Every input that changes the answer, in a fixed order (the HTTP response cache keys on the same query)."""
        return (f"{VERSION}|player={self.player}|teammate={self.teammate or '-'}|from={self.season_from}w{self.week_from}"
                f"|to={self.season_to}w{self.week_to}|as_of={self.as_of or '-'}")

    def as_dict(self) -> dict:
        return {"player": self.player, "teammate": self.teammate, "season_from": self.season_from,
                "week_from": self.week_from, "season_to": self.season_to, "week_to": self.week_to,
                "as_of": self.as_of}


# ------------------------------------------------------------------ validation --

def latest_season(con) -> int | None:
    """The newest season with stored NFL usage (global data only; no league is consulted)."""
    if not table_exists(con, "player_week_usage"):
        return None
    row = con.execute("SELECT max(season) FROM player_week_usage").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def validate(player: str, teammate: str | None, season_from: int | None, week_from: int | None,
             season_to: int | None, week_to: int | None, as_of: str | None, default_season: int | None) -> SplitsQuery:
    if not GSIS_RE.match(player or ""):
        raise SplitsError("player is not a valid nflverse player id")
    if teammate is not None and not GSIS_RE.match(teammate):
        raise SplitsError("teammate is not a valid nflverse player id")
    if teammate is not None and teammate == player:
        raise SplitsError("teammate must be a different player")
    if season_from is None and season_to is None:
        if default_season is None:
            raise SplitsError("no NFL usage is stored yet, so a season must be given")
        season_from = season_to = default_season
    season_from = season_to if season_from is None else season_from
    season_to = season_from if season_to is None else season_to
    week_from = FIRST_WEEK if week_from is None else week_from
    week_to = LAST_WEEK if week_to is None else week_to
    for name, w in (("week_from", week_from), ("week_to", week_to)):
        if not FIRST_WEEK <= w <= LAST_WEEK:
            raise SplitsError(f"{name} must be a regular-season week ({FIRST_WEEK}-{LAST_WEEK})")
    if (season_from, week_from) > (season_to, week_to):
        raise SplitsError("the window starts after it ends")
    if season_to - season_from + 1 > MAX_SEASONS:
        raise SplitsError(f"a window covers at most {MAX_SEASONS} seasons")
    if as_of is not None:
        if not DATE_RE.match(as_of):
            raise SplitsError("as_of must be a date written YYYY-MM-DD")
        try:
            date.fromisoformat(as_of)
        except ValueError:
            raise SplitsError("as_of is not a valid date") from None
    return SplitsQuery(player, teammate, season_from, week_from, season_to, week_to, as_of)


# ---------------------------------------------------------------------- loading --

def _in_list(values) -> str:
    return ", ".join(str(int(v)) for v in values)


def _roster_rows(con, seasons: list[int], gsis_ids: list[str]) -> list[dict]:
    gt = "AND (game_type = 'REG' OR game_type IS NULL)" if "game_type" in table_columns(con, "nfl_rosters_weekly") else ""
    return fetch_dicts(con, f"""
        SELECT DISTINCT season::INT AS season, week::INT AS week, gsis_id, team, status
        FROM nfl_rosters_weekly
        WHERE season IN ({_in_list(seasons)}) AND gsis_id IN (SELECT unnest(?::VARCHAR[])) {gt}
        ORDER BY season, week, gsis_id, team, status""", [sorted(gsis_ids)])


def _roster_coverage(con, seasons: list[int], teams: list[str]) -> set[tuple[int, int, str]]:
    if not teams:
        return set()
    gt = "AND (game_type = 'REG' OR game_type IS NULL)" if "game_type" in table_columns(con, "nfl_rosters_weekly") else ""
    return {(r["season"], r["week"], r["team"]) for r in fetch_dicts(con, f"""
        SELECT DISTINCT season::INT AS season, week::INT AS week, team FROM nfl_rosters_weekly
        WHERE season IN ({_in_list(seasons)}) AND team IN (SELECT unnest(?::VARCHAR[])) {gt}""", [sorted(teams)])}


def _usage_rows(con, seasons: list[int], gsis_id: str) -> list[dict]:
    """Every stored usage row of one player (any position), with how the row was sourced."""
    return fetch_dicts(con, f"""
        SELECT season, week, gsis_id, team, opponent, position, offense_snaps, offense_pct, targets, carries,
               opportunities, routes, xfp, fantasy_points_ppr, stats_source
        FROM player_week_usage WHERE season IN ({_in_list(seasons)}) AND gsis_id = ?
        ORDER BY season, week, team""", [gsis_id])


def _schedule(con, seasons: list[int]) -> dict[tuple[int, int, str], dict]:
    """(season, week, team) -> that team's REG game with its result when final."""
    out = {}
    for r in fetch_dicts(con, f"""
            SELECT season, week, game_id, gameday, home_team, away_team, home_score, away_score FROM nfl_schedules
            WHERE game_type = 'REG' AND season IN ({_in_list(seasons)}) ORDER BY season, week, game_id"""):
        final = r["home_score"] is not None and r["away_score"] is not None
        for team, opp, pf, pa, home in ((r["home_team"], r["away_team"], r["home_score"], r["away_score"], True),
                                        (r["away_team"], r["home_team"], r["away_score"], r["home_score"], False)):
            out[(int(r["season"]), int(r["week"]), team)] = {
                "game_id": r["game_id"], "gameday": None if r["gameday"] is None else str(r["gameday"])[:10],
                "opponent": opp, "home": home, "final": final, "points_for": pf if final else None,
                "points_against": pa if final else None}
    return out


def _primary_qbs(con, seasons: list[int], teams: list[str]) -> dict[tuple[int, int, str], str]:
    """(season, week, team) -> the team's QB with the most offensive snaps that game (ties: lowest id)."""
    if not teams:
        return {}
    rows = fetch_dicts(con, f"""
        SELECT season, week, team, gsis_id, offense_snaps FROM player_week_usage
        WHERE season IN ({_in_list(seasons)}) AND position = 'QB' AND offense_snaps > 0
          AND team IN (SELECT unnest(?::VARCHAR[]))
        ORDER BY season, week, team, offense_snaps DESC, gsis_id""", [sorted(teams)])
    out: dict[tuple[int, int, str], str] = {}
    for r in rows:
        out.setdefault((r["season"], r["week"], r["team"]), r["gsis_id"])
    return out


def _team_env(con, seasons: list[int], teams: list[str]) -> dict[tuple[int, int, str], dict]:
    """Optional environment per team-game: neutral pass rate and plays (play-by-play), implied total (lines)."""
    out: dict[tuple[int, int, str], dict] = defaultdict(dict)
    if not teams:
        return out
    if table_exists(con, "nfl_team_week"):
        for r in fetch_dicts(con, f"""
                SELECT season, week, team, neutral_pass_rate, offense_plays FROM nfl_team_week
                WHERE season IN ({_in_list(seasons)}) AND team IN (SELECT unnest(?::VARCHAR[]))""", [sorted(teams)]):
            out[(r["season"], r["week"], r["team"])].update(neutral_pass_rate=r["neutral_pass_rate"],
                                                            offense_plays=r["offense_plays"])
    if table_exists(con, "team_game_lines"):
        for r in fetch_dicts(con, f"""
                SELECT season, week, team, implied_total FROM team_game_lines
                WHERE season IN ({_in_list(seasons)}) AND team IN (SELECT unnest(?::VARCHAR[]))""", [sorted(teams)]):
            out[(r["season"], r["week"], r["team"])]["implied_total"] = r["implied_total"]
    return out


def _players(con, gsis_ids) -> dict[str, dict]:
    ids = sorted({g for g in gsis_ids if g})
    if not ids or not table_exists(con, "nfl_players"):
        return {}
    return {r["gsis_id"]: r for r in fetch_dicts(con, """
        SELECT gsis_id, display_name, position FROM nfl_players WHERE gsis_id IN (SELECT unnest(?::VARCHAR[]))""",
                                                  [ids])}


# ------------------------------------------------------------------- classify --

def appeared(r: dict | None) -> bool:
    """A box-score row (a quarterback's passes count) or an offensive snap, target or carry."""
    return r is not None and (played(r) or r.get("stats_source") in ("stats+snaps", "stats_only"))


def teammate_status(team: str, usage: list[dict], roster: list[dict], roster_known: bool,
                    reference_share: float | None, reference_games: int) -> tuple[str, str, dict]:
    """(group, reason, evidence) for one subject game on `team`. Group is "with", "without" or "excluded"; a status
    that is not known is "excluded", never "without"."""
    evidence = {"roster": [{"team": r["team"], "status": r["status"]} for r in roster],
                "usage_team": None, "offense_pct": None}
    here = next((u for u in usage if u["team"] == team), None)
    elsewhere = [u for u in usage if u["team"] != team and appeared(u)]
    if here is not None:
        evidence.update(usage_team=here["team"], offense_pct=here.get("offense_pct"))
    if appeared(here):
        share = here.get("offense_pct")
        if share is None:
            return "excluded", "snap_record_missing", evidence
        if (reference_share is not None and reference_games >= PARTIAL_REFERENCE_MIN_GAMES
                and share < PARTIAL_SHARE_RATIO * reference_share):
            return "excluded", "partial_game", evidence
        return "with", "appeared", evidence
    if elsewhere:
        evidence["usage_team"] = elsewhere[0]["team"]
        return "without", "other_team", evidence
    if here is not None and here.get("offense_snaps") == 0 and not (here.get("opportunities") or 0):
        return "without", "no_offensive_snaps", evidence           # a snap record: measured, not missing
    teams = sorted({r["team"] for r in roster})
    if len(teams) > 1:
        return "excluded", "ambiguous_roster", evidence
    if teams and teams[0] == team:
        statuses = sorted({r["status"] for r in roster})
        if "ACT" in statuses:
            return "excluded", "active_no_record", evidence
        status = statuses[0] if statuses else None
        if status in ABSENT_STATUS:
            return "without", ABSENT_STATUS[status][0], evidence
        return "without", "off_active_roster", evidence
    if teams:
        return "without", "other_team", evidence
    return "excluded", ("no_roster_record" if roster_known else "roster_data_missing"), evidence


def _eligible(game: dict | None, as_of: str | None) -> str | None:
    """None when the game counts, else the reason it does not (no game, not final, on or after the cutoff)."""
    if game is None:
        return "no_schedule"
    if as_of is not None and (game["gameday"] is None or game["gameday"] >= as_of):
        return "after_cutoff"
    if not game["final"]:
        return "not_final"
    return None


def _round(v, digits=4):
    return round(v, digits) if isinstance(v, float) else v


def _mean(values) -> tuple[float | None, int]:
    vals = [v for v in values if v is not None]
    return (round(sum(vals) / len(vals), 4) if vals else None), len(vals)


def _environment(weeks: list[dict], names: dict[str, dict]) -> dict:
    """Context of one group's games: points scored, implied totals, pass rate, plays, home games, QBs, opponents."""
    n = len(weeks)
    out: dict = {"games": n}
    if not n:
        return out
    coverage = {}
    for key, field in (("team_points_pg", "team_points"), ("implied_total_pg", "implied_total"),
                       ("neutral_pass_rate", "neutral_pass_rate"), ("offense_plays_pg", "offense_plays")):
        v, k = _mean(w.get(field) if field == "team_points" else w["env"].get(field) for w in weeks)
        out[key] = v
        if k < n:
            coverage[key] = k
    out["home_games"] = sum(1 for w in weeks if w["home"])
    qbs = Counter(w["primary_qb"] for w in weeks if w["primary_qb"])
    out["primary_qbs"] = [{"gsis_id": g, "name": (names.get(g) or {}).get("display_name") or g, "games": c}
                          for g, c in sorted(qbs.items(), key=lambda kv: (-kv[1], kv[0]))]
    unknown_qb = sum(1 for w in weeks if not w["primary_qb"])
    if unknown_qb:
        coverage["primary_qbs"] = n - unknown_qb
    out["opponents"] = sorted(Counter(w["opponent"] for w in weeks if w["opponent"]).items())
    out["opponents"] = [{"team": t, "games": c} for t, c in out["opponents"]]
    out["coverage"] = coverage
    return out


def _player_info(gsis: str, info: dict | None, usage: list[dict], mapping: dict) -> dict:
    last = usage[-1] if usage else None
    m = mapping.get(gsis, {"espn_player_id": None, "status": "unmapped"})
    return {"gsis_id": gsis, "name": (info or {}).get("display_name") or gsis,
            "position": (last or {}).get("position") or (info or {}).get("position"),
            "espn_player_id": m.get("espn_player_id"), "mapping_status": m["status"]}


# ----------------------------------------------------------------------- build --

def _missing_tables(con) -> list[str]:
    return [t for t in REQUIRED_TABLES if not table_exists(con, t)]


def _no_data(q: SplitsQuery, missing: list[str]) -> dict:
    return {"version": VERSION, "status": "no_data", "inputs": q.as_dict(), "cache_key": q.cache_key(),
            "message": f"Game splits need stored nflverse data that is missing here ({', '.join(missing)}). "
                       "Run scripts/sync_nfl.py.",
            "missing_tables": missing}


def _rules() -> dict:
    return {"min_group_games": MIN_GROUP_GAMES, "small_group_games": SMALL_GROUP_GAMES,
            "partial_share_ratio": PARTIAL_SHARE_RATIO, "partial_reference_min_games": PARTIAL_REFERENCE_MIN_GAMES,
            "max_seasons": MAX_SEASONS, "weeks": [FIRST_WEEK, LAST_WEEK], "reasons": dict(REASONS),
            "scoring_basis": "nflverse full PPR; xFP is nflverse ffopportunity expected PPR points"}


def teammate_options(subject_usage: list[dict], q: SplitsQuery, schedule: dict, con) -> list[dict]:
    """QB / RB / WR / TE who appeared for one of the player's teams in an eligible game of the window."""
    keys = sorted({(u["season"], u["week"], u["team"]) for u in subject_usage
                   if q.contains(u["season"], u["week"]) and _eligible(schedule.get((u["season"], u["week"], u["team"])),
                                                                        q.as_of) is None})
    if not keys:
        return []
    rows = fetch_dicts(con, f"""
        SELECT u.gsis_id, u.position, u.team, count(*) AS games
        FROM player_week_usage u
        JOIN (SELECT unnest(?::INT[]) AS season, unnest(?::INT[]) AS week, unnest(?::VARCHAR[]) AS team) k
          ON k.season = u.season AND k.week = u.week AND k.team = u.team
        WHERE u.gsis_id <> ? AND u.position IN ('QB', 'RB', 'WR', 'TE')
          AND (u.offense_snaps > 0 OR u.opportunities > 0 OR u.stats_source IN ('stats+snaps', 'stats_only'))
        GROUP BY 1, 2, 3""", [[k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys], q.player])
    by: dict[str, dict] = {}
    for r in rows:
        cur = by.setdefault(r["gsis_id"], {"gsis_id": r["gsis_id"], "position": r["position"], "teams": set(), "games": 0})
        cur["teams"].add(r["team"])
        cur["games"] += int(r["games"])
    names = _players(con, by)
    order = {p: i for i, p in enumerate(TEAMMATE_POSITIONS)}
    out = [{"gsis_id": g, "name": (names.get(g) or {}).get("display_name") or g, "position": v["position"],
            "teams": sorted(v["teams"]), "games_together_listed": v["games"]} for g, v in by.items()]
    out.sort(key=lambda o: (order.get(o["position"], 9), -o["games_together_listed"], o["name"], o["gsis_id"]))
    return out[:MAX_TEAMMATE_OPTIONS]


def build_game_splits(con, q: SplitsQuery) -> dict:
    missing = _missing_tables(con)
    if missing:
        return _no_data(q, missing)
    seasons = q.seasons
    subject_all = _usage_rows(con, seasons, q.player)
    subject_all = [u for u in subject_all if q.contains(u["season"], u["week"])]
    if not subject_all:
        raise SplitsNotFound("No usage is stored for this player in the window")
    schedule = _schedule(con, seasons)
    known = [u for u in subject_all if _eligible(schedule.get((u["season"], u["week"], u["team"])), q.as_of) is None]
    position = (known or subject_all)[-1]["position"]    # as of the cutoff when one is set
    if position not in SUBJECT_POSITIONS:
        raise SplitsError("Game splits cover WR, RB and TE players")
    ids = [q.player] + ([q.teammate] if q.teammate else [])
    info = _players(con, ids)
    mapping = espn_mapping(con, ids)
    base = {"version": VERSION, "inputs": q.as_dict(), "cache_key": q.cache_key(), "rules": _rules(),
            "confounding": list(CONFOUNDING), "metrics": [{"key": k, "label": lbl, "format": f} for k, lbl, f in METRICS[position]],
            "player": {**_player_info(q.player, info.get(q.player), known or subject_all, mapping), "position": position}}
    if q.teammate is None:
        return {**base, "status": "needs_teammate", "teammate": None,
                "teammate_options": teammate_options(subject_all, q, schedule, con),
                "message": "Choose a teammate to split this player's games by."}

    mate_usage = [u for u in _usage_rows(con, seasons, q.teammate) if q.contains(u["season"], u["week"])]
    mate_roster = [r for r in _roster_rows(con, seasons, [q.teammate, q.player]) if q.contains(r["season"], r["week"])]
    if not mate_usage and not any(r["gsis_id"] == q.teammate for r in mate_roster) and q.teammate not in info:
        raise SplitsNotFound("No record is stored for this teammate")
    mate_known = [u for u in mate_usage if _eligible(schedule.get((u["season"], u["week"], u["team"])), q.as_of) is None]
    mate_pos = ((mate_known or mate_usage)[-1]["position"] if mate_usage else (info.get(q.teammate) or {}).get("position"))
    if mate_pos is not None and mate_pos not in TEAMMATE_POSITIONS + ("FB",):
        raise SplitsError("The teammate must be a QB, RB, WR or TE")

    subject_roster = defaultdict(list)
    mate_roster_by = defaultdict(list)
    for r in mate_roster:
        (mate_roster_by if r["gsis_id"] == q.teammate else subject_roster)[(r["season"], r["week"])].append(r)
    mate_usage_by = defaultdict(list)
    for u in mate_usage:
        mate_usage_by[(u["season"], u["week"])].append(u)

    # Eligible weeks: final, before the cutoff, inside the window. Pair teams and the partial-game reference use
    # eligible weeks only, so nothing after the cutoff reaches an earlier week.
    def eligible_week(season: int, week: int, team: str | None) -> bool:
        return team is not None and _eligible(schedule.get((season, week, team)), q.as_of) is None

    subject_teams = sorted({u["team"] for u in subject_all if u["team"]})
    pair_teams = set()
    for (s, w), rows in mate_roster_by.items():
        pair_teams.update(r["team"] for r in rows if eligible_week(s, w, r["team"]))
    for (s, w), rows in mate_usage_by.items():
        pair_teams.update(u["team"] for u in rows if eligible_week(s, w, u["team"]))
    pair_teams &= set(subject_teams)
    ref: dict[str, list[float]] = defaultdict(list)
    for (s, w), rows in mate_usage_by.items():
        for u in rows:
            if appeared(u) and u.get("offense_pct") is not None and eligible_week(s, w, u["team"]):
                ref[u["team"]].append(u["offense_pct"])
    reference = {t: (median(v), len(v)) for t, v in ref.items()}

    coverage = _roster_coverage(con, seasons, subject_teams)
    qbs = _primary_qbs(con, seasons, subject_teams)
    env = _team_env(con, seasons, subject_teams)
    full_rows = {}
    for s in seasons:
        for r in load_usage(con, s, q.player):
            full_rows[(s, r["week"])] = r
    subject_by = {(u["season"], u["week"]): u for u in subject_all}

    scheduled_weeks = {(k[0], k[1]) for k in schedule}
    week_first_day: dict[tuple[int, int], str] = {}
    for (gs_, gw, _), g in schedule.items():
        if g["gameday"] is not None:
            cur = week_first_day.get((gs_, gw))
            week_first_day[(gs_, gw)] = g["gameday"] if cur is None else min(cur, g["gameday"])
    weeks, groups = [], {"with": [], "without": []}
    excluded, not_counted, without_reasons = Counter(), Counter(), Counter()
    for s in seasons:
        lo = q.week_from if s == q.season_from else FIRST_WEEK
        hi = q.week_to if s == q.season_to else LAST_WEEK
        for w in range(lo, hi + 1):
            first_day = week_first_day.get((s, w))
            if q.as_of is not None and (first_day is None or first_day >= q.as_of):
                # The whole week starts on or after the cutoff (by the schedule, which is pregame): nothing of it is
                # read, not even the player's team or a bye.
                reason = "after_cutoff" if first_day is not None else "no_schedule"
                weeks.append({"season": s, "week": w, "team": None, "opponent": None, "gameday": None, "home": None,
                              "final": None, "team_points": None, "primary_qb": None, "env": {}, "subject": None,
                              "teammate": None, "outcome": "not_counted", "reason": reason,
                              "reason_label": REASONS[reason]})
                not_counted[reason] += 1
                continue
            u = subject_by.get((s, w))
            roster_teams = sorted({r["team"] for r in subject_roster.get((s, w), [])})
            team = u["team"] if u else (roster_teams[0] if len(roster_teams) == 1 else None)
            game = schedule.get((s, w, team)) if team else None
            row = {"season": s, "week": w, "team": team, "opponent": game["opponent"] if game else None,
                   "gameday": game["gameday"] if game else None, "home": game["home"] if game else None,
                   "final": game["final"] if game else None,
                   "team_points": game["points_for"] if game else None,
                   "primary_qb": qbs.get((s, w, team)) if team else None,
                   "env": {k: _round(v) for k, v in env.get((s, w, team), {}).items()} if team else {},
                   "subject": None, "teammate": None}
            if u:
                row["subject"] = {k: u.get(k) for k in ("offense_pct", "targets", "carries", "opportunities", "routes",
                                                        "xfp", "fantasy_points_ppr")}
            outcome, reason = "not_counted", None
            if team is None:
                reason = "no_record"
            elif game is None:
                reason = "bye" if u is None and (s, w) in scheduled_weeks else "no_schedule"
            else:
                reason = _eligible(game, q.as_of)
                if reason is None:
                    if u is None or not played(u):
                        reason = "subject_did_not_play"
                    elif not is_qualifying(u):
                        reason = "subject_metrics_missing"
                    elif team not in pair_teams:
                        reason = "not_teammates"
                    else:
                        r_share, r_games = reference.get(team, (None, 0))
                        group, reason, evidence = teammate_status(
                            team, mate_usage_by.get((s, w), []), mate_roster_by.get((s, w), []),
                            (s, w, team) in coverage, r_share, r_games)
                        row["teammate"] = {**evidence, "reference_share": _round(r_share), "reference_games": r_games}
                        outcome = group
            if reason == "after_cutoff":        # nothing from on or after the cutoff is shown, not even the opponent
                row.update(team=None, opponent=None, gameday=None, home=None, final=None, team_points=None,
                           primary_qb=None, env={}, subject=None)
            row.update(outcome=outcome, reason=reason, reason_label=REASONS.get(reason, "Played"))
            if outcome == "with":
                groups["with"].append(row)
            elif outcome == "without":
                groups["without"].append(row)
                without_reasons[reason] += 1
            elif outcome == "excluded":
                excluded[reason] += 1
            else:
                not_counted[reason] += 1
            weeks.append(row)

    names = _players(con, {r["primary_qb"] for r in weeks if r["primary_qb"]})
    for r in weeks:
        r["primary_qb_name"] = (names.get(r["primary_qb"]) or {}).get("display_name") if r["primary_qb"] else None
    summary = {}
    for g, rows in groups.items():
        games = [full_rows[(r["season"], r["week"])] for r in rows if (r["season"], r["week"]) in full_rows]
        summary[g] = {"games": len(rows), "weeks": [{"season": r["season"], "week": r["week"]} for r in rows],
                      "metrics": summarize_window(games, position) if games else {"games": 0, "coverage": {}},
                      "environment": _environment(rows, names)}
    summary["without"]["reasons"] = dict(sorted(without_reasons.items()))
    n_with, n_without = summary["with"]["games"], summary["without"]["games"]
    enough = n_with >= MIN_GROUP_GAMES and n_without >= MIN_GROUP_GAMES
    differences = None
    if enough:
        differences = {}
        for key, _, _ in METRICS[position]:
            a, b = summary["with"]["metrics"].get(key), summary["without"]["metrics"].get(key)
            differences[key] = _round(b - a) if a is not None and b is not None else None
    abstention = None if enough else {
        "reason": "low_n",
        "message": (f"Differences are withheld: each group needs at least {MIN_GROUP_GAMES} games "
                    f"(with: {n_with}, without: {n_without})."),
    }
    cautions = []
    if enough and min(n_with, n_without) < SMALL_GROUP_GAMES:
        cautions.append(f"A group has fewer than {SMALL_GROUP_GAMES} games: one game moves its mean noticeably.")
    qb_with = {x["gsis_id"] for x in summary["with"]["environment"].get("primary_qbs", [])}
    qb_without = {x["gsis_id"] for x in summary["without"]["environment"].get("primary_qbs", [])}
    if len(qb_with | qb_without) > 1:
        cautions.append("More than one quarterback started across these games: compare the quarterback counts under "
                        "Environment before reading the split as being about this teammate.")
    if without_reasons.get("other_team"):
        cautions.append("Some games without him are from weeks he was on another team: the team around the player "
                        "may have changed in other ways too.")
    if len({r["season"] for r in groups["with"]} | {r["season"] for r in groups["without"]}) > 1:
        cautions.append("The window spans seasons: roles, coaches and rosters change between seasons.")
    if excluded:
        cautions.append(f"{sum(excluded.values())} games are excluded because his status is unknown; they are not "
                        "counted as games without him.")

    usage_weeks = con.execute(f"""SELECT max(week) FROM player_week_usage WHERE season = {int(q.season_to)}""").fetchone()
    return {
        **base,
        "status": "ok" if enough else "low_n",
        "message": None if enough else abstention["message"],
        "teammate": {**_player_info(q.teammate, info.get(q.teammate), mate_known or mate_usage, mapping),
                     "position": mate_pos, "teams_with_player": sorted(pair_teams)},
        "window": {"start": {"season": q.season_from, "week": q.week_from},
                   "end": {"season": q.season_to, "week": q.week_to}, "as_of": q.as_of,
                   "weeks_listed": len(weeks), "usage_through_week": usage_weeks[0] if usage_weeks else None},
        "groups": summary,
        "differences": differences,
        "abstention": abstention,
        "cautions": cautions,
        "counts": {"with": n_with, "without": n_without, "excluded": sum(excluded.values()),
                   "excluded_by_reason": dict(sorted(excluded.items())),
                   "not_counted": sum(not_counted.values()), "not_counted_by_reason": dict(sorted(not_counted.items()))},
        "weeks": weeks,
    }


def build_split_players(con, season: int | None) -> dict:
    """WR / RB / TE with at least one played game in the season: the picker for the game-splits page."""
    season = season if season is not None else latest_season(con)
    missing = _missing_tables(con)
    if season is None or missing:
        return {"version": VERSION, "season": season, "status": "no_data", "players": [],
                "missing_tables": missing or ["player_week_usage"],
                "message": "Game splits need stored nflverse usage, rosters and schedules. Run scripts/sync_nfl.py."}
    rows = fetch_dicts(con, """
        SELECT gsis_id, arg_max(position, week) AS position, arg_max(team, week) AS team, count(*) AS games
        FROM player_week_usage
        WHERE season = ? AND (offense_snaps > 0 OR opportunities > 0)
        GROUP BY 1""", [season])
    rows = [r for r in rows if r["position"] in SUBJECT_POSITIONS]
    names = _players(con, (r["gsis_id"] for r in rows))
    players = [{"gsis_id": r["gsis_id"], "name": (names.get(r["gsis_id"]) or {}).get("display_name") or r["gsis_id"],
                "position": r["position"], "team": r["team"], "games": int(r["games"])} for r in rows]
    players.sort(key=lambda p: (p["name"], p["gsis_id"]))
    seasons = [int(r[0]) for r in con.execute("SELECT DISTINCT season FROM player_week_usage ORDER BY 1").fetchall()]
    return {"version": VERSION, "season": season, "status": "ok" if players else "empty",
            "players": players[:MAX_PLAYER_OPTIONS], "truncated": len(players) > MAX_PLAYER_OPTIONS,
            "seasons": seasons}
