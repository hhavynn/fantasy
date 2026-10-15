"""A small synthetic snapshot (4 teams, 5 played weeks) shaped like stages/snapshot.py output."""

from __future__ import annotations

import pytest

from studio.core import Config, Episode, canonical_json, sha256_bytes, write_json

TEAMS = ["PHI", "DAL", "NYJ", "SEA"]


def _schedule() -> list[dict]:
    games = []
    for week in range(1, 19):
        pairs = [("PHI", "DAL"), ("NYJ", "SEA")] if week % 2 else [("PHI", "NYJ"), ("DAL", "SEA")]
        for home, away in pairs:
            games.append({"game_id": f"2026_{week:02d}_{away}_{home}", "week": week, "game_type": "REG",
                          "gameday": f"2026-10-{week:02d}", "home_team": home, "away_team": away,
                          "home_score": 20 if week < 6 else None, "away_score": 17 if week < 6 else None,
                          "home_qb_name": f"{home} Starter", "away_qb_name": f"{away} Starter",
                          "home_coach": f"{home} Coach", "away_coach": f"{away} Coach"})
    return games


def _rows(gsis_id: str, team: str, pos: str, snaps: list[float], tshare: list[float]) -> list[dict]:
    rows = []
    for week, (s, t) in enumerate(zip(snaps, tshare), start=1):
        if s is None:
            continue
        rows.append({"week": week, "gsis_id": gsis_id, "team": team, "opponent": "XXX", "position": pos,
                     "offense_snaps": int(s * 60), "team_offense_snaps": 60, "offense_pct": s, "targets": round(t * 35),
                     "target_share": t, "air_yards": 50.0, "air_yards_share": t, "adot": 9.5, "carries": 0,
                     "designed_rushes": 0, "backfield_rush_share": None, "team_rush_share": 0.0, "rz_targets": 0,
                     "rz_carries": 0, "gl_carries": 0})
    return rows


def make_snapshot(week: int = 6) -> dict:
    usage = (_rows("00-RISER", "PHI", "WR", [0.20, 0.25, 0.20, 0.85, 0.90], [0.05, 0.05, 0.05, 0.25, 0.28])
             + _rows("00-FALLER", "DAL", "WR", [0.90, 0.92, 0.90, 0.60, 0.55], [0.25, 0.27, 0.24, 0.10, 0.08])
             + _rows("00-FLAT", "NYJ", "WR", [0.80, 0.81, 0.79, 0.80, 0.82], [0.20, 0.21, 0.20, 0.21, 0.20])
             # One early injury exit (week 3), otherwise steady: must not read as a faller.
             + _rows("00-EXIT", "SEA", "WR", [0.90, 0.92, 0.10, 0.91, 0.90], [0.25, 0.26, 0.02, 0.25, 0.24]))
    players = [{"gsis_id": g, "display_name": n, "position": "WR"} for g, n in
               [("00-RISER", "Rising Receiver"), ("00-FALLER", "Falling Receiver"), ("00-FLAT", "Flat Receiver"),
                ("00-EXIT", "Exit Receiver")]]
    team_ctx = [{"team": t, "through_week": week - 1, "games": 5, "proe": p, "neutral_pass_rate": 0.55,
                 "dropbacks": 180} for t, p in zip(TEAMS, [-4.3, 3.1, 0.5, -1.0])]
    defense = [{"defteam": t, "through_week": week - 1, "games": 5, "pass_plays": 180, "pass_epa_allowed": 0.0,
                "rush_plays": 120, "rush_epa_allowed": 0.0, "targets": 160, "rb_target_share_allowed": 0.2,
                "te_target_share_allowed": 0.2,
                "ranks": {"pass_epa_allowed": r, "rush_epa_allowed": r, "te_target_share_allowed": r,
                          "rb_target_share_allowed": r}} for t, r in zip(TEAMS, [3, 30, 15, 24])]
    tables = {"player_week_usage": usage, "nfl_players": players, "nfl_schedules": _schedule(),
              "team_context": team_ctx, "defense_context": defense, "nfl_injuries": []}
    return {"season": 2026, "week": week, "data_through_week": week - 1, "source_sync": None, "tables": tables,
            "content_sha256": sha256_bytes(canonical_json(tables).encode())}


@pytest.fixture
def episode(tmp_path, monkeypatch) -> Episode:
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    cfg = Config.load()
    cfg.raw["llm"] = {**cfg.raw["llm"], "provider": "fake"}
    cfg.raw["selection"] = {**cfg.raw["selection"], "risers": 2, "fallers": 2}
    ep = Episode(2026, 6, "long", cfg, log=lambda *_: None)
    ep.dir.mkdir(parents=True)
    write_json(ep.path("snapshot.json"), make_snapshot())
    return ep
