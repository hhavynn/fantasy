"""Build the synthetic Sleeper league DuckDB the browser smoke test (frontend/e2e) runs against (multi-league Phase 4b).
No network: a Sleeper league is made from the ESPN fixture's players (backend/tests/espn_fixture.py, each carrying its
ESPN id as Sleeper's `espn_id`), written with app/sleeper/write.py, given the synthetic NFL schedule, Fantasy
Intel's own projections for the weeks to come (sealed runs; Sleeper publishes no projections) and the analytics
pipeline. Half-PPR, a superflex and a TE premium, so nothing is ESPN-shaped. Every number is an invented test input.

    cd backend && uv run python ../scripts/build_e2e_sleeper.py [path]   # default: ../data/e2e-sleeper.duckdb

Serve it with FANTASY_DB_PATH=<path> ESPN_SEASON=2026.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import _bootstrap  # noqa: F401

sys.path.insert(0, str(_bootstrap.BACKEND / "tests"))

import synthetic_nfl as nfl  # noqa: E402
from espn_fixture import CURRENT, PLAYERS, ROSTERS, SEASON  # noqa: E402

from app.analytics import dst_store, independent_projection_store as ips, kicker_store  # noqa: E402
from app.analytics.kicker_scoring import resolve_kicker_scoring  # noqa: E402
from app.analytics.league_scoring import resolve_skill_scoring  # noqa: E402
from app.analytics.pipeline import run_analytics  # noqa: E402
from app.db.connection import connect, init_db  # noqa: E402
from app.espn.constants import PRO_TEAM_MAP, nflverse_team  # noqa: E402
from app.sleeper.scoring import to_scoring_items  # noqa: E402
from app.sleeper.write import SleeperPayloads, write_sleeper  # noqa: E402

LEAGUE_ID = "1190000000000000001"
REGULAR_PERIODS = 14
FINAL = 17
TEAMS = [1, 2, 3, 4]
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF", "BN", "BN", "BN", "BN", "BN"]
SCORING = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "bonus_rec_te": 0.5, "fum_lost": -2, "fum_rec_td": 6,
    "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50p": 5, "fgmiss": -1, "xpm": 1, "xpmiss": -1,
    "sack": 1, "int": 2, "fum_rec": 2, "ff": 1, "safe": 2, "blk_kick": 2, "def_td": 6,
    "pts_allow_0": 10, "pts_allow_1_6": 7, "pts_allow_7_13": 4, "pts_allow_14_20": 1, "pts_allow_21_27": 0,
    "pts_allow_28_34": -1, "pts_allow_35p": -4,
}
NAMES = {1: "Sleeper Alpha", 2: "Sleeper Bravo", 3: "Sleeper Charlie", 4: "Sleeper Delta"}


def sleeper_id(espn_id: int) -> str:
    """D/ST: Sleeper's team code; players: a made-up Sleeper id (Sleeper's `espn_id` carries the real mapping)."""
    if espn_id < 0:
        return PRO_TEAM_MAP[-16000 - espn_id]
    return str(90000 + espn_id)


def round_robin(team_ids: list[int], periods: int) -> dict[int, list[tuple[int, int]]]:
    t, out = list(team_ids), {}
    for p in range(1, periods + 1):
        out[p] = [(t[i], t[-1 - i]) if (p + i) % 2 else (t[-1 - i], t[i]) for i in range(len(t) // 2)]
        t = [t[0]] + [t[-1]] + t[1:-1]
    return out


def players() -> dict[str, dict]:
    out = {}
    for pid, (name, pos, team) in PLAYERS.items():
        s = sleeper_id(pid)
        code = nflverse_team(team)
        out[s] = {"player_id": s, "full_name": name, "position": POSITIONS[pos], "fantasy_positions": [POSITIONS[pos]],
                  "team": code, "status": "Active", "active": True, "espn_id": None if pid < 0 else pid,
                  "injury_status": "Questionable" if pid == 202 else None}
    return out


def starters(entries: list[int]) -> tuple[list[str], list[str]]:
    """Fill the starting slots from a roster in the fixture's order; the rest is the bench."""
    pos = {pid: POSITIONS[PLAYERS[pid][1]] for pid in entries}
    slots = [p for p in ROSTER_POSITIONS if p != "BN"]
    used, line = set(), []
    eligible = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}}
    for slot in slots:
        ok = eligible.get(slot, {slot})
        pick = next((p for p in entries if p not in used and pos[p] in ok), None)
        if pick is not None:
            used.add(pick)
        line.append(sleeper_id(pick) if pick is not None else "0")
    return line, [sleeper_id(p) for p in entries if p not in used]


def payloads() -> SleeperPayloads:
    rosters, lines = [], {}
    for tid in TEAMS:
        entries = [pid for pid, _ in ROSTERS[tid]]
        line, bench = starters(entries)
        lines[tid] = (line, [*[s for s in line if s != "0"], *bench])
        rosters.append({"roster_id": tid, "owner_id": f"7000000000000001{tid:02d}", "co_owners": None,
                        "players": lines[tid][1], "starters": line, "reserve": None, "taxi": None,
                        "settings": {"wins": 0, "losses": 0, "ties": 0, "fpts": 0, "waiver_position": tid,
                                     "waiver_budget_used": 0, "total_moves": 1, "division": 1}})
    users = [{"user_id": f"7000000000000001{t:02d}", "display_name": f"manager{t}",
              "metadata": {"team_name": NAMES[t]}, "is_owner": t == 1} for t in TEAMS]
    matchups = {}
    for week, pairs in round_robin(TEAMS, REGULAR_PERIODS).items():
        rows = []
        for mid, (h, a) in enumerate(pairs, start=1):
            for tid in (h, a):
                played = week < CURRENT
                pts = {s: round(8.0 + (int(s) % 7 if s.isdigit() else 3) + week * 0.5, 2) for s in lines[tid][1]} \
                    if played else {}
                rows.append({"roster_id": tid, "matchup_id": mid, "points": round(sum(pts[s] for s in lines[tid][0]
                                                                                       if s in pts), 2) if played else 0,
                             "starters": lines[tid][0], "players": lines[tid][1], "players_points": pts})
        matchups[week] = rows
    for week in range(REGULAR_PERIODS + 1, FINAL + 1):
        matchups[week] = []
    tx = {2: [{"type": "free_agent", "transaction_id": "1190000000000009001", "status": "complete",
               "status_updated": 1757600000000, "created": 1757600000000, "roster_ids": [3],
               "adds": {sleeper_id(209): 3}, "drops": None, "leg": 2, "creator": "700000000000000103",
               "settings": None},
              {"type": "waiver", "transaction_id": "1190000000000009002", "status": "complete",
               "status_updated": 1757700000000, "created": 1757650000000, "roster_ids": [4],
               "adds": {sleeper_id(309): 4}, "drops": None, "leg": 2, "creator": "700000000000000104",
               "settings": {"waiver_bid": 7}}]}
    league = {"league_id": LEAGUE_ID, "name": "Sleeper Fixture League", "season": str(SEASON), "status": "in_season",
              "sport": "nfl", "season_type": "regular", "total_rosters": len(TEAMS),
              "roster_positions": ROSTER_POSITIONS, "scoring_settings": SCORING, "previous_league_id": None,
              "draft_id": None, "bracket_id": None,
              "settings": {"playoff_week_start": REGULAR_PERIODS + 1, "playoff_teams": 4, "start_week": 1,
                           "waiver_type": 2, "waiver_budget": 100, "trade_deadline": 11, "playoff_round_type": 0,
                           "reserve_slots": 1}}
    state = {"week": CURRENT, "season": str(SEASON), "season_type": "regular", "display_week": CURRENT, "leg": CURRENT}
    return SleeperPayloads(league=league, users=users, rosters=rosters, state=state, matchups=matchups,
                           transactions=tx, players=players())


def projections(con) -> dict:
    """Sealed Ours, K and D/ST runs in the league's scoring for weeks CURRENT..FINAL (horizon = weeks ahead)."""
    items, _, _ = to_scoring_items(SCORING)
    skill_fp = resolve_skill_scoring(items).fingerprint
    ks = resolve_kicker_scoring(items)
    dst_fp, _ = dst_store.scoring_fingerprint(items)
    created = datetime(SEASON, 9, 20, 12, 0)
    base = {1: 18.0, 2: 12.0, 3: 11.0, 4: 8.0}
    counts = {"skill": 0, "kicker": 0, "dst": 0}
    for week in range(CURRENT, FINAL + 1):
        k = week - CURRENT
        common = {"season": SEASON, "week": week, "forecast_horizon": k, "projected_from_week": CURRENT,
                  "data_through_season": SEASON, "data_through_week": CURRENT - 1, "created_at": created,
                  "artifact_id": "e2e-fixture", "artifact_created_at": created.isoformat()}
        rows = []
        for pid, (name, pos, team) in PLAYERS.items():
            if pos not in base:
                continue
            pts = round(base[pos] + (pid % 5) * 0.7 - k * 0.1, 2)
            rows.append({"season": SEASON, "week": week, "espn_player_id": pid, "gsis_id": f"00-E2E{pid:04d}",
                         "position": POSITIONS[pos], "nfl_team": nflverse_team(team), "opponent": None, "is_home": None,
                         "projected_points": pts, "interval_low": round(pts * 0.4, 2), "interval_high": round(pts * 1.7, 2),
                         "model": "e2e", "coverage_status": "projected", "games_used": CURRENT - 1,
                         "latest_usage_season": SEASON, "latest_usage_week": CURRENT - 1, "missing_inputs": [],
                         "flags": [], "explanation": [], "p_play": 0.95, "expected_points": round(pts * 0.95, 2),
                         "participation": None})
        meta = {**common, "run_id": f"e2e-s{SEASON}w{week:02d}", "model_version": "ifp-2.2.0",
                "scoring_fingerprint": skill_fp, "model_status": {}, "evaluation": {}, "participation_version": None,
                "participation_status": None, "participation_evaluation": None, "nfl_synced_at": created}
        ips.write_run(con, meta, rows)
        counts["skill"] += 1
        krows = [{"season": SEASON, "week": week, "espn_player_id": pid, "gsis_id": None, "nfl_team": nflverse_team(team),
                  "opponent": None, "is_home": None, "venue_id": None, "coverage_status": "projected",
                  "projected_points": 8.4, "interval_low": 3.0, "interval_high": 14.0, "p_play": 1.0,
                  "expected_points": 8.4, "stat_line": {}, "context": {}, "explanation": [], "flags": [],
                  "missing_inputs": []} for pid, (_, pos, team) in PLAYERS.items() if pos == 5]
        kicker_store.write_run(con, {**common, "run_id": f"e2e-k-s{SEASON}w{week:02d}", "model_version": "kfp-1.1.0",
                                     "scoring_fingerprint": ks.fingerprint, "scoring": ks.describe(),
                                     "model_status": {}, "evaluation": {}, "weather_summary": None}, krows)
        counts["kicker"] += 1
        if k == 0:     # the D/ST model projects the coming week only
            drows = [{"season": SEASON, "week": week, "espn_player_id": pid, "nfl_team": nflverse_team(team),
                      "opponent": None, "is_home": None, "coverage_status": "projected", "projected_points": 7.1,
                      "interval_low": 1.0, "interval_high": 15.0, "expected_points": 7.1, "stat_line": {},
                      "context": {}, "explanation": [], "flags": [], "missing_inputs": []}
                     for pid, (_, pos, team) in PLAYERS.items() if pos == 16]
            dst_store.write_run(con, {**common, "run_id": f"e2e-d-s{SEASON}w{week:02d}", "model_version": "dfp-r1",
                                      "scoring_fingerprint": dst_fp, "scoring": dst_store.describe_scoring(items),
                                      "model_status": {}, "evaluation": {}, "inputs": {}}, drows)
            counts["dst"] += 1
    return counts


def build(db_path: Path) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (db_path, db_path.with_suffix(db_path.suffix + ".wal")):
        p.unlink(missing_ok=True)
    con = connect(db_path)
    try:
        init_db(con)
        stats = {"sleeper": write_sleeper(con, payloads())}
        nfl.write(con, SEASON, nfl.schedule(18), set(range(1, CURRENT)))
        nfl.record_fetch(con)
        stats["projections"] = projections(con)
        stats["analytics"] = run_analytics(con, SEASON)
        return stats
    finally:
        con.close()


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else _bootstrap.BACKEND.parent / "data" / "e2e-sleeper.duckdb").resolve()
    stats = build(db_path)
    print(json.dumps({"db_path": str(db_path), "stats": stats}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
