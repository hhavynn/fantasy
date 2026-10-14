"""Controlled synthetic NFL schedules and per-defense weekly aggregates for schedule-intelligence
tests. Values are invented test inputs (not real NFL outcomes) chosen so ranks and rates are known."""

from __future__ import annotations

# 32 nflverse-style codes; the ESPN fixture's teams (KC BUF DET PHI SF SEA TB WAS) are included.
TEAMS = ["KC", "BUF", "DET", "PHI", "SF", "SEA", "TB", "WAS", "ARI", "ATL", "BAL", "CAR", "CHI", "CIN", "CLE",
         "DAL", "DEN", "GB", "HOU", "IND", "JAX", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ",
         "PIT", "TEN"]
PAST_DAY = "2026-09-{:02d}"          # completed weeks (dates in the past)
FUTURE_DAY = "2099-10-{:02d}"        # not yet kicked off whatever the real clock says
FETCHED_AT = "2026-10-12 21:00:00"   # NFL sync start (naive UTC, as stored in sync_runs)
LIVE_DAY, LIVE_TIME = "2026-10-12", "16:25"   # kicked off 20:25 UTC: 35 minutes before the fetch


def rounds(teams: list[str], n: int) -> list[list[tuple[str, str]]]:
    """Circle-method round robin: n rounds of (home, away) pairs."""
    t = list(teams)
    out = []
    for r in range(n):
        pairs = [(t[i], t[-1 - i]) if (r + i) % 2 == 0 else (t[-1 - i], t[i]) for i in range(len(t) // 2)]
        out.append(pairs)
        t = [t[0]] + [t[-1]] + t[1:-1]
    return out


def schedule(weeks: int = 18, byes: dict[int, set[str]] | None = None) -> dict[int, list[tuple[str, str]]]:
    """week -> [(home, away)]. Teams on bye in a week are dropped with their scheduled opponent, whose
    week also becomes a bye (pass bye teams as pairs to control exactly who rests)."""
    byes = byes or {}
    out = {}
    for w, pairs in enumerate(rounds(TEAMS, weeks), start=1):
        off = byes.get(w, set())
        out[w] = [(h, a) for h, a in pairs if h not in off and a not in off]
    return out


def pass_rate(team: str) -> float:
    """EPA per pass play allowed: TEAMS[0] most permissive, TEAMS[-1] least."""
    return round(0.20 - 0.01 * TEAMS.index(team), 4)


def rush_rate(team: str) -> float:
    """EPA per designed rush allowed: reversed order (TEAMS[-1] most permissive)."""
    return round(-0.20 + 0.01 * TEAMS.index(team), 4)


def defense_row(season: int, week: int, defteam: str, opponent: str, **over) -> dict:
    i = TEAMS.index(defteam)
    row = {"season": season, "week": week, "defteam": defteam, "opponent": opponent,
           "pass_plays": 40, "pass_epa_sum": round(40 * pass_rate(defteam), 6),
           "rush_plays": 25, "rush_epa_sum": round(25 * rush_rate(defteam), 6), "rush_success": 5 + i % 10,
           "explosive_runs": 2, "targets": 30, "rb_targets": 6, "wr_targets": 15 + i % 4, "te_targets": 5 + i % 3}
    row.update(over)
    return row


def record_fetch(con, fetched_at: str = FETCHED_AT) -> None:
    """The successful NFL sync whose data is in the tables (its start is the fetch time)."""
    con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status) VALUES (?, 'nfl', ?, ?, 'ok')",
                [f"nfl-{fetched_at}", fetched_at, fetched_at])


def write(con, season: int, sched: dict[int, list[tuple[str, str]]], final_weeks: set[int],
          partial: dict[int, set[str]] | None = None, defense: bool = True, overrides: dict | None = None,
          prior_season: bool = True, live: dict[int, set[str]] | None = None,
          scores: dict[tuple[int, str], tuple] | None = None) -> None:
    """nfl_schedules + nfl_defense_week. Weeks in `final_weeks` are complete (past date, scores). In
    `partial` weeks, games whose home team is listed are final (past date) and the others have kicked
    off in the past without a final score (play-by-play rows exist: an in-progress game). Other weeks
    are future, unscored and have no defensive rows. In `live` weeks, games whose home team is listed
    kicked off 35 minutes before the fetch and already show a positive score (10-7): an in-progress
    game with a live score. `scores[(week, home)]` overrides a final score (e.g. 24-0 or 0-0).
    `overrides[(week, defteam)]` patches a row."""
    partial, overrides, live, scores = partial or {}, overrides or {}, live or {}, scores or {}
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR,
                   home_score INT, away_score INT)""")
    for w, pairs in sched.items():
        for h, a in pairs:
            gid = f"{season}_{w:02d}_{a}_{h}"
            if w in live and h in live[w]:
                vals = (LIVE_DAY, LIVE_TIME, 10, 7)                                # live score, not final
            elif w in final_weeks or (w in partial and h in partial[w]):
                vals = (PAST_DAY.format(min(w, 28)), "13:00", *scores.get((w, h), (24, 17)))
            elif w in partial:
                vals = (PAST_DAY.format(min(w, 28)), "16:25", None, None)       # kicked off, no final score
            else:
                vals = (FUTURE_DAY.format(min(w, 28)), "13:00", None, None)
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, ?, ?, ?, ?)",
                        [season, w, gid, vals[0], vals[1], h, a, vals[2], vals[3]])
            played = w in final_weeks or w in partial or w in live
            if defense and played:
                for d, o in ((h, a), (a, h)):
                    r = defense_row(season, w, d, o, **overrides.get((w, d), {}))
                    con.execute(f"INSERT INTO nfl_defense_week ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                                list(r.values()))
    if prior_season and defense:   # extreme previous-season rows that must never leak into this season
        for d in TEAMS:
            r = defense_row(season - 1, 17, d, "XXX", pass_epa_sum=9999.0, rush_epa_sum=9999.0)
            con.execute(f"INSERT INTO nfl_defense_week ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                        list(r.values()))
