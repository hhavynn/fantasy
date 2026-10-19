"""NGS passing ingestion (nfl_ngs_passing): loaded like the other NGS tables, stored as published.

Research input only (docs/research/savant_data_coverage.md): no ifp-2 feature reads it.
"""

from pathlib import Path

import polars as pl

from app.db.connection import connect, init_db
from app.nfl import sync


def _ngs_frame(season: int, weeks: list[int], ttt: list[float | None]) -> pl.DataFrame:
    n = len(weeks)
    return pl.DataFrame({
        "season": [season] * n, "season_type": ["REG"] * n, "week": weeks,
        "player_display_name": [f"QB {i}" for i in range(n)], "player_position": ["QB"] * n,
        "team_abbr": ["BUF"] * n, "player_gsis_id": [f"00-00{i:05d}" for i in range(n)],
        "avg_time_to_throw": ttt, "aggressiveness": [None] * n, "avg_intended_air_yards": [7.5] * n,
        "completion_percentage_above_expectation": [1.2] * n, "attempts": [30] * n,
    }, schema_overrides={"season": pl.Int32, "week": pl.Int32, "attempts": pl.Int32,
                         "aggressiveness": pl.Float64, "avg_time_to_throw": pl.Float64})


def _con(tmp_path: Path):
    con = connect(tmp_path / "t.duckdb")
    init_db(con)
    return con


def test_ngs_passing_is_a_season_partitioned_raw_dataset(monkeypatch):
    calls = []
    monkeypatch.setattr(sync.nfl, "load_nextgen_stats",
                        lambda seasons, stat_type: calls.append((seasons, stat_type)) or _ngs_frame(2026, [1], [2.7]))
    loader, season_col = sync.RAW_DATASETS["nfl_ngs_passing"]
    assert season_col == "season"
    assert loader(2026).height == 1
    assert calls == [([2026], "passing")]


def test_ngs_passing_keeps_nulls_and_season_totals(tmp_path):
    con = _con(tmp_path)
    frame = _ngs_frame(2026, [0, 5, 6], [2.8, None, 3.1])     # week 0 = NGS season totals
    stats = sync.write_all(con, sync.NflPayloads(season=2026, frames={"nfl_ngs_passing": frame}))
    assert stats["nfl_ngs_passing"] == 3 and stats["nfl_ngs_passing_max_week"] == 6
    rows = con.execute("SELECT week, avg_time_to_throw, aggressiveness FROM nfl_ngs_passing ORDER BY week").fetchall()
    assert rows == [(0, 2.8, None), (5, None, None), (6, 3.1, None)]   # missing stays missing, never 0


def test_ngs_passing_resync_replaces_only_its_season(tmp_path):
    con = _con(tmp_path)
    sync.write_all(con, sync.NflPayloads(season=2025, frames={"nfl_ngs_passing": _ngs_frame(2025, [1, 2], [2.5, 2.6])}))
    sync.write_all(con, sync.NflPayloads(season=2026, frames={"nfl_ngs_passing": _ngs_frame(2026, [1], [2.9])}))
    sync.write_all(con, sync.NflPayloads(season=2026, frames={"nfl_ngs_passing": _ngs_frame(2026, [1, 2], [3.0, 3.2])}))
    counts = dict(con.execute("SELECT season, count(*) FROM nfl_ngs_passing GROUP BY 1").fetchall())
    assert counts == {2025: 2, 2026: 2}


def test_ngs_passing_download_failure_is_a_warning_not_a_sync_failure(monkeypatch):
    def boom(_season):
        raise ConnectionError("release asset unavailable")

    monkeypatch.setattr(sync, "RAW_DATASETS", {
        "nfl_ngs_passing": (boom, "season"),
        "nfl_ngs_receiving": (lambda s: _ngs_frame(s, [1], [None]), "season"),
    })
    monkeypatch.setattr(sync.nfl, "load_players", lambda: None)
    monkeypatch.setattr(sync.nfl, "load_pbp", lambda seasons: None)
    monkeypatch.setattr(sync.nfl, "load_ftn_charting", lambda seasons: None)
    p = sync.fetch_all(2026)
    assert "nfl_ngs_passing" not in p.frames and "nfl_ngs_receiving" in p.frames
    assert any(w.startswith("nfl_ngs_passing failed: ConnectionError") for w in p.warnings)
