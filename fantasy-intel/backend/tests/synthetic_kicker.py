"""A small deterministic kicker world for tests: 4 teams, seasons 2024-2025 complete, 2026 weeks 1-3 played and
weeks 4-5 scheduled (week 5: CCC and DDD on bye). Invented data only.

Kickers (gsis): K-A (AAA), K-B (BBB), K-C (CCC), K-D (DDD through 2026 week 2, then released), K-E (DDD from 2026
week 3), K-H (signed by CCC for week 4; K-C released then; no kick in the data = cold start).
Home venues: AAA Ford Field (fixed roof), BBB Lambeau (open air), CCC Mercedes-Benz (retractable), DDD Denver.
"""

from __future__ import annotations

import json
import math
import random

import polars as pl

from app.db.connection import init_db, insert_rows

TEAMS = ("AAA", "BBB", "CCC", "DDD")
VENUE = {"AAA": "DET00", "BBB": "GNB00", "CCC": "ATL97", "DDD": "DEN00"}
PAIRS = (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)))
KICKER_ITEMS = [{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0}, {"statId": 198, "points": 5.0},
                {"statId": 201, "points": 6.0}, {"statId": 85, "points": -1.0}, {"statId": 86, "points": 1.0}]
# ESPN id -> (name, gsis, mapping confidence, ESPN injury status, ESPN pro team)
ESPN_KICKERS = {
    101: ("Kicker A", "K-A", "high", "ACTIVE", "AAA"),
    102: ("Kicker B", "K-B", "high", "OUT", "BBB"),
    103: ("Kicker C", "K-C", "high", "ACTIVE", "CCC"),
    104: ("Kicker D", "K-D", "high", "ACTIVE", "DDD"),     # released after 2026 week 2: never gets DDD's chances
    105: ("Kicker E", "K-E", "high", "ACTIVE", "DDD"),
    106: ("Kicker F", "K-F", "high", "ACTIVE", None),      # free agent, never on a roster
    107: ("Kicker G", "K-G", "medium", "ACTIVE", "AAA"),   # uncertain mapping
    108: ("Kicker H", "K-H", "high", "ACTIVE", "CCC"),     # new signing, no kicks yet
}


def kicker_for(team: str, season: int, week: int) -> str:
    if team == "DDD" and season == 2026 and week >= 3:
        return "K-E"
    if team == "CCC" and season == 2026 and week >= 4:
        return "K-H"
    return f"K-{team[0]}"


def _games(season: int, weeks) -> list[dict]:
    out = []
    for w in weeks:
        pairs = PAIRS[(w - 1) % 3] if not (season == 2026 and w == 5) else ((0, 1),)
        for i, (h, a) in enumerate(pairs):
            home, away = TEAMS[h], TEAMS[a]
            out.append({"game_id": f"{season}_{w:02d}_{away}_{home}", "season": season, "week": w, "home_team": home,
                        "away_team": away, "gameday": f"{season}-09-{10 + w:02d}" if w <= 20 else None,
                        "gametime": "13:00", "location": "Home", "stadium_id": VENUE[home], "stadium": VENUE[home],
                        "roof": {"DET00": "dome", "ATL97": "closed"}.get(VENUE[home], "outdoors"),
                        "surface": "grass", "temp": None if VENUE[home] in ("DET00", "ATL97") else 55.0,
                        "wind": None if VENUE[home] in ("DET00", "ATL97") else 6.0 + i})
    return out


def world(seed: int = 7) -> dict[str, pl.DataFrame]:
    rnd = random.Random(seed)
    games = _games(2024, range(1, 18)) + _games(2025, range(1, 18)) + _games(2026, range(1, 6))
    strength = {"AAA": 0.6, "BBB": 0.0, "CCC": -0.3, "DDD": 0.2}
    teams, kicks, sched = [], [], []
    for g in games:
        played = not (g["season"] == 2026 and g["week"] >= 4)
        scores = {}
        for side, opp, home in ((g["home_team"], g["away_team"], True), (g["away_team"], g["home_team"], False)):
            if not played:
                continue
            k = kicker_for(side, g["season"], g["week"])
            td = min(6, max(0, int(round(rnd.gauss(2.3 + strength[side], 1.1)))))
            fga = min(6, max(0, int(round(rnd.gauss(1.9, 1.1)))))
            made = 0
            for j in range(fga):
                d = rnd.randint(19, 58)
                p = 1 / (1 + math.exp(-(4.5 - 0.09 * (d - 18))))
                res = "made" if rnd.random() < p else ("blocked" if rnd.random() < 0.05 else "missed")
                made += res == "made"
                kicks.append({"season": g["season"], "week": g["week"], "game_id": g["game_id"], "play_id": float(100 + j + (0 if home else 50)),
                              "posteam": side, "defteam": opp, "home_team": g["home_team"], "away_team": g["away_team"],
                              "kicker_player_id": k, "kicker_player_name": k, "kick_type": "FG", "kick_distance": float(d),
                              "result": res, "qtr": 2.0, "half_seconds_remaining": 600.0, "score_differential": 0.0,
                              "roof": g["roof"], "surface": "grass", "temp": g["temp"], "wind": g["wind"],
                              "stadium_id": g["stadium_id"], "location": "Home"})
            for j in range(td):
                kicks.append({"season": g["season"], "week": g["week"], "game_id": g["game_id"], "play_id": float(200 + j + (0 if home else 50)),
                              "posteam": side, "defteam": opp, "home_team": g["home_team"], "away_team": g["away_team"],
                              "kicker_player_id": k, "kicker_player_name": k, "kick_type": "PAT", "kick_distance": 33.0,
                              "result": "made" if rnd.random() < 0.95 else "missed", "qtr": 3.0,
                              "half_seconds_remaining": 600.0, "score_differential": 0.0, "roof": g["roof"],
                              "surface": "grass", "temp": g["temp"], "wind": g["wind"], "stadium_id": g["stadium_id"],
                              "location": "Home"})
            scores[side] = 7 * td + 3 * made
            teams.append({"season": g["season"], "week": g["week"], "game_id": g["game_id"], "team": side, "opponent": opp,
                          "is_home": home, "location": "Home", "roof": g["roof"], "surface": "grass", "temp": g["temp"],
                          "wind": g["wind"], "stadium_id": g["stadium_id"], "drives": 11, "td_drives": td,
                          "fg_drives": fga, "punt_drives": max(0, 11 - td - fga - 1), "turnover_drives": 1,
                          "downs_drives": 0, "end_half_drives": 0, "fg_range_drives": td + fga + 1, "rz_drives": td + 1,
                          "rz_td_drives": td, "rz_fg_drives": 1, "avg_start_yardline": 72.0, "fg_att": fga, "fg_made": made,
                          "fg_att_50": 0, "pat_att": td, "pat_made": td, "two_pt_att": 0, "fourth_go_in_range": 1,
                          "fourth_in_range": 3, "plays": 62, "epa_per_play": 0.02, "success_rate": 0.45, "turnovers": 1,
                          "points": float(7 * td + 3 * made), "kicker_id": k if (fga or td) else None,
                          "kickoff_kicker_id": k, "game_kicker_id": k})
        sched.append({**g, "home_score": scores.get(g["home_team"]), "away_score": scores.get(g["away_team"]),
                      "result": None if not played else scores[g["home_team"]] - scores[g["away_team"]],
                      "game_type": "REG"})
    return {"schedule": pl.DataFrame(sched), "teams": pl.DataFrame(teams), "kicks": pl.DataFrame(kicks)}


def scoring_items(extra=None) -> list[dict]:
    from synthetic_forecast import SCORING_ITEMS

    return SCORING_ITEMS + KICKER_ITEMS + (extra or [])


def database(con, w: dict | None = None, *, items=None) -> dict:
    """Write the world into an initialised DuckDB connection (live layout). Returns the frames."""
    w = w or world()
    init_db(con)
    insert_rows(con, "nfl_team_game_drives", w["teams"].to_dicts())
    insert_rows(con, "nfl_kick_attempts", w["kicks"].to_dicts())
    con.register("_s", w["schedule"].to_arrow())
    con.execute("CREATE TABLE nfl_schedules AS SELECT * FROM _s")
    con.unregister("_s")
    roster = []
    for wk in range(1, 5):
        for t in TEAMS:
            roster.append({"season": 2026, "week": wk, "gsis_id": kicker_for(t, 2026, wk), "team": t, "position": "K",
                           "status": "ACT", "game_type": "REG"})
    con.register("_r", pl.DataFrame(roster).to_arrow())
    con.execute("CREATE TABLE nfl_rosters_weekly AS SELECT * FROM _r")
    con.unregister("_r")
    con.execute("INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period, scoring_items, "
                "synced_at) VALUES (99, 2026, 'Synthetic', 4, 17, ?, now())", [json.dumps(items or scoring_items())])
    for pid, (name, g, conf, inj, team) in ESPN_KICKERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status, on_team_id) "
                    "VALUES (?, ?, 'K', ?, ?, ?)", [pid, name, team, inj, 1 if pid % 2 else 0])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES (?, ?, ?, ?)",
                    [pid, g, "espn_id:nfl_players", conf])
        for wk in (4, 5):
            con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points, synced_at) VALUES (2026, ?, ?, NULL, ?, '2026-09-01 12:00:00')",
                        [pid, wk, 7.0 + pid % 3])
    return w


def artifact(w: dict, scoring):
    """A kfp artifact trained the production way on the synthetic seasons before 2026."""
    from app.analytics import kicker_backtest as kb
    from app.analytics import kicker_model as km

    cfg = kb.PRODUCTION
    data = kb.build_dataset(w, scoring, cfg)
    train, _ = kb._attach_skill(data["rows"], data["conv"], cfg, 2025)
    train = train.filter(pl.col("completed"))
    comps = kb.fit_components(train, cfg)
    conv = km.fit_conversion(data["conv"].filter(pl.col("season") < 2026), cfg)
    return km.KickerArtifact(version=km.MODEL_VERSION, created_at="2026-09-01T00:00:00+00:00",
                             scoring_fingerprint=scoring.fingerprint, scoring=scoring.describe(),
                             config={"kconfig": kb.cfg_dict(cfg)}, lambda_fg=comps["lam_fg"], lambda_pat=comps["lam_pat"],
                             conversion=conv, direct=comps["direct"],
                             intervals={"nominal_coverage": 0.8, "bands": [{"low": None, "high": None, "q_low": -5.0,
                                                                            "q_high": 6.0, "n": 100}]},
                             participation={"none": {"p": 0.99, "n": 100}, "questionable": {"p": 0.8, "n": 10}},
                             status={"K": {"status": "test"}}, evaluation={"league_median_points": 8.0},
                             train_window=[2025, 2025], data_cutoff_key=202599)
