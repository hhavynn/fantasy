"""External utilization adapter: import contract, identity, validation, idempotency,
provider conflicts, nflverse canonicity and score isolation. Synthetic data only."""

import csv
import subprocess
from pathlib import Path

import pytest

from app.analytics.context import build_context
from app.analytics.player_scores import build_player_scores
from app.analytics.usage import build_player_week_usage
from app.analytics.validation import check_external_ratios, check_route_participation
from app.db.connection import fetch_dicts
from app.external.contract import derived_ratio, parse_rows, source_player_key
from app.external.importer import add_reviewed_mapping, import_external_usage
from app.external.selection import select_provider

SEASON = 2026
PLAYERS = [  # gsis, name, position, team, pfr
    ("G1", "Alpha Receiver", "WR", "KC", "AlphRe00"),
    ("G2", "Bravo Back", "RB", "BUF", "BravBa00"),
    ("G3", "Charlie Tight", "TE", "KC", "CharTi00"),
    ("G4", "Mike Williams", "WR", "NYJ", "WillMi00"),
    ("G5", "Mike Williams", "WR", "NYJ", "WillMi01"),
    ("G6", "Marvin Harrison Jr.", "WR", "ARI", "HarrMa00"),
    ("G7", "Traded Guy", "WR", "DAL", "TradGu00"),
    ("G8", "Zero Route", "WR", "KC", "ZeroRo00"),
]
STATS = {  # (gsis, week): (targets, receptions, receiving_yards, carries, ppr, xfp)
    ("G1", 1): (8, 6, 80.0, 0, 20.0, 14.0), ("G1", 2): (10, 7, 95.0, 0, 23.5, 16.0), ("G1", 3): (6, 4, 50.0, 0, 13.0, 11.0),
    ("G2", 1): (3, 3, 20.0, 15, 15.0, 13.0), ("G2", 2): (4, 3, 25.0, 18, 17.0, 15.0), ("G2", 3): (2, 2, 10.0, 12, 9.0, 10.0),
    ("G3", 1): (5, 4, 40.0, 0, 10.0, 8.0), ("G3", 2): (4, 3, 30.0, 0, 8.0, 7.0), ("G3", 3): (6, 5, 55.0, 0, 13.5, 9.0),
    ("G8", 1): (0, 0, 0.0, 0, 0.0, 0.0),
}


@pytest.fixture
def nfl(con):
    con.executemany("INSERT INTO nfl_players (gsis_id, display_name, position, latest_team, pfr_id) VALUES (?,?,?,?,?)",
                    [(g, n, p, t, pfr) for g, n, p, t, pfr in PLAYERS])
    con.execute("""CREATE TABLE nfl_rosters_weekly (season INT, week INT, gsis_id VARCHAR, full_name VARCHAR,
                   position VARCHAR, team VARCHAR, espn_id VARCHAR)""")
    for w in (1, 2, 3):
        con.executemany("INSERT INTO nfl_rosters_weekly VALUES (?,?,?,?,?,?,NULL)",
                        [(SEASON, w, g, n, p, t) for g, n, p, t, _ in PLAYERS])
    con.execute("""CREATE TABLE nfl_weekly_stats (season INT, week INT, season_type VARCHAR, player_id VARCHAR,
                   team VARCHAR, opponent_team VARCHAR, position VARCHAR, targets INT, receptions INT,
                   receiving_yards DOUBLE, receiving_tds INT, receiving_air_yards DOUBLE, target_share DOUBLE,
                   air_yards_share DOUBLE, wopr DOUBLE, carries INT, rushing_yards DOUBLE, rushing_tds INT,
                   fantasy_points_ppr DOUBLE)""")
    con.execute("""CREATE TABLE nfl_snap_counts (season INT, game_type VARCHAR, week INT, pfr_player_id VARCHAR,
                   team VARCHAR, opponent VARCHAR, position VARCHAR, offense_snaps INT, offense_pct DOUBLE)""")
    con.execute("CREATE TABLE nfl_ff_opportunity (season INT, week INT, player_id VARCHAR, total_fantasy_points_exp DOUBLE)")
    info = {g: (p, t, pfr) for g, _, p, t, pfr in PLAYERS}
    for (g, w), (tg, rec, yds, car, ppr, xfp) in STATS.items():
        p, t, pfr = info[g]
        con.execute("INSERT INTO nfl_weekly_stats VALUES (?,?, 'REG', ?,?, 'OPP', ?,?,?,?,0,?,0.2,0.2,0.4,?,?,0,?)",
                    [SEASON, w, g, t, p, tg, rec, yds, yds * 1.2, car, car * 4.0, ppr])
        con.execute("INSERT INTO nfl_snap_counts VALUES (?, 'REG', ?,?,?, 'OPP', ?, 50, 0.8)", [SEASON, w, pfr, t, p])
        con.execute("INSERT INTO nfl_ff_opportunity VALUES (?,?,?,?)", [SEASON, w, g, xfp])
    con.execute("""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   home_team VARCHAR, away_team VARCHAR, spread_line DOUBLE, total_line DOUBLE)""")
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 1, 'REG', 'g1', 'KC', 'BUF', 3.0, 48.0)")
    return con


def write_csv(tmp_path, name, rows, columns=None):
    columns = columns or sorted({k for r in rows for k in r})
    path = tmp_path / name
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})
    return path


def base_row(**kw):
    row = {"season": SEASON, "week": 1, "player_name": "Alpha Receiver", "team": "KC", "position": "WR",
           "routes": 30, "route_participation": 0.9}
    row.update(kw)
    return row


def imp(con, tmp_path, rows, name="f.csv", provider="test_provider", dataset="weekly_routes", columns=None):
    return import_external_usage(con, write_csv(tmp_path, name, rows, columns), provider=provider, dataset=dataset)


def ext_rows(con):
    return {(r["week"], r["player_name"]): r for r in fetch_dicts(con, "SELECT * FROM external_player_week_usage")}


# ------------------------------------------------------------- import contract --

def test_clean_normalized_csv_import(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [base_row(provider_player_id="P1", gsis_id="G1", slot_rate=0.6, wide_rate=0.4,
                                       source_updated_at="2026-09-15T10:00:00")])
    assert rec["status"] == "imported" and rec["matched_rows"] == 1 and rec["unmatched_rows"] == 0
    r = ext_rows(nfl)[(1, "Alpha Receiver")]
    assert r["routes"] == 30 and r["slot_rate"] == 0.6 and r["source_player_key"] == "id:P1"
    assert r["mapping_method"] == "gsis_id" and r["mapping_confidence"] == "high"
    assert str(r["source_updated_at"]).startswith("2026-09-15")


def test_optional_columns_absent_and_missing_is_null(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [{"season": SEASON, "week": 1, "gsis_id": "G1", "routes": 30}])
    assert rec["status"] == "imported"
    r = ext_rows(nfl)[(1, None)]
    assert r["routes"] == 30
    for col in ("route_participation", "pass_snaps", "slot_rate", "man_tprr", "two_minute_route_share"):
        assert r[col] is None                      # not provided -> NULL, never 0


def test_imported_zero_stays_zero_and_blank_stays_null(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1", pass_block_snaps=0, pass_snaps=34, slot_rate="", wide_rate="NA")])
    r = ext_rows(nfl)[(1, "Alpha Receiver")]
    assert r["pass_block_snaps"] == 0 and r["slot_rate"] is None and r["wide_rate"] is None


def test_minimum_identity_required(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [{"season": SEASON, "week": 1, "player_name": "Alpha Receiver", "routes": 5}])
    assert rec["status"] == "rejected"
    names = {i["check_name"] for i in fetch_dicts(nfl, "SELECT * FROM external_usage_issues")}
    assert "missing_identity" in names


def test_source_player_key_prefers_provider_id():
    assert source_player_key("P9", "G1", "A", "KC", "WR") == "id:P9"
    assert source_player_key(None, "G1", "A", "KC", "WR") == "gsis:G1"
    k1 = source_player_key(None, None, "Alpha  Receiver", "kc", "wr")
    assert k1 == source_player_key(None, None, "alpha receiver", "KC", "WR") and k1.startswith("nm:")


# ------------------------------------------------------------------- identity --

def test_direct_valid_and_invalid_gsis(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1"), base_row(gsis_id="00-NOPE", player_name="Bad Id", week=2)])
    rows = ext_rows(nfl)
    assert rows[(1, "Alpha Receiver")]["gsis_id"] == "G1"
    bad = rows[(2, "Bad Id")]
    assert bad["gsis_id"] is None and bad["mapping_method"] == "unmatched"
    assert bad["mapping_reason"].startswith("invalid_gsis_id")      # not re-guessed by name


def test_unique_exact_name_team_position_mapping(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(team="KAN")])                       # generic team alias KAN -> KC
    r = ext_rows(nfl)[(1, "Alpha Receiver")]
    assert r["gsis_id"] == "G1" and r["mapping_method"] == "name_team_position" and r["mapping_confidence"] == "medium"


def test_ambiguous_suffix_stale_team_and_unknown_are_preserved(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [
        base_row(player_name="Mike Williams", team="NYJ"),               # two nflverse players
        base_row(player_name="Marvin Harrison", team="ARI"),             # suffix ambiguity (Jr.)
        base_row(player_name="Traded Guy", team="PHI"),                  # stale/wrong team
        base_row(player_name="Nobody Known", team="KC", provider_player_id="UNK-1"),   # unknown provider id
    ])
    assert rec["status"] == "imported" and rec["row_count"] == 4
    assert rec["ambiguous_rows"] == 1 and rec["unmatched_rows"] == 3 and rec["matched_rows"] == 0
    rows = ext_rows(nfl)
    assert rows[(1, "Mike Williams")]["mapping_method"] == "ambiguous"
    assert rows[(1, "Marvin Harrison")]["mapping_reason"].startswith("suffix_or_punctuation_mismatch")
    assert rows[(1, "Traded Guy")]["mapping_reason"].startswith("team_mismatch")
    assert rows[(1, "Nobody Known")]["mapping_reason"].startswith("no_exact_match")
    assert all(r["gsis_id"] is None for r in rows.values())             # nothing guessed
    assert all(r["routes"] == 30 for r in rows.values())                # values kept for inspection
    checks = [i["check_name"] for i in fetch_dicts(nfl, "SELECT * FROM external_usage_issues")]
    assert checks.count("missing_gsis_match") == 3 and checks.count("ambiguous_identity") == 1


def test_reviewed_mapping_resolves_stored_unmatched_rows(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(player_name="Marvin Harrison", team="ARI", provider_player_id="MH1")])
    assert add_reviewed_mapping(nfl, "test_provider", "id:MH1", "G6") == 1
    r = ext_rows(nfl)[(1, "Marvin Harrison")]
    assert r["gsis_id"] == "G6" and r["mapping_method"] == "external_player_map" and r["mapping_confidence"] == "reviewed"
    with pytest.raises(ValueError):
        add_reviewed_mapping(nfl, "test_provider", "id:MH1", "00-NOPE")


# ----------------------------------------------------------------- validation --

@pytest.mark.parametrize("bad,check", [
    ({"routes": -1}, "negative_routes"),
    ({"pass_snaps": -2}, "negative_pass_snaps"),
    ({"route_participation": 1.2}, "route_participation_out_of_range"),
    ({"route_participation": -0.1}, "route_participation_out_of_range"),
    ({"slot_rate": 85}, "slot_rate_out_of_range"),                      # percents are rejected
    ({"wide_rate": -0.2}, "wide_rate_out_of_range"),
    ({"pass_snaps": 20, "pass_block_snaps": 25}, "pass_block_gt_pass_snaps"),
    ({"week": 0}, "invalid_week"),
    ({"week": 30}, "invalid_week"),
    ({"routes": "lots"}, "non_numeric_value"),
])
def test_hard_invalid_values_reject_the_file(nfl, tmp_path, bad, check):
    rec = imp(nfl, tmp_path, [base_row(gsis_id="G1", **bad), base_row(gsis_id="G3", player_name="Charlie Tight")])
    assert rec["status"] == "rejected" and rec["error_count"] >= 1
    assert nfl.execute("SELECT count(*) FROM external_player_week_usage").fetchone()[0] == 0   # no partial import
    issues = fetch_dicts(nfl, "SELECT * FROM external_usage_issues WHERE import_id = ?", [rec["import_id"]])
    assert check in {i["check_name"] for i in issues if i["severity"] == "error"}


def test_duplicate_source_rows_in_one_file_rejected(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [base_row(provider_player_id="P1"), base_row(provider_player_id="P1", routes=31)])
    assert rec["status"] == "rejected"
    assert {i["check_name"] for i in fetch_dicts(nfl, "SELECT * FROM external_usage_issues")} >= {"duplicate_source_key"}


def test_zero_routes_makes_ratios_null_not_zero(nfl, tmp_path):
    assert derived_ratio(0, 0) is None and derived_ratio(5, None) is None and derived_ratio(None, 10) is None
    assert derived_ratio(6, 30) == pytest.approx(0.2)
    imp(nfl, tmp_path, [base_row(gsis_id="G8", player_name="Zero Route", routes=0, source_tprr=0.0, source_yprr=0.0)])
    r = ext_rows(nfl)[(1, "Zero Route")]
    assert r["routes"] == 0 and r["source_tprr"] is None and r["source_yprr"] is None
    build_player_week_usage(nfl, SEASON)
    u = fetch_dicts(nfl, "SELECT * FROM player_week_usage WHERE gsis_id = 'G8'")[0]
    assert u["routes"] == 0 and u["tprr"] is None and u["yprr"] is None


def test_canonical_stat_columns_in_file_are_ignored(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [base_row(gsis_id="G1", targets=99, receiving_yards=999, fantasy_points=77)])
    assert rec["status"] == "imported"
    names = [i["check_name"] for i in fetch_dicts(nfl, "SELECT * FROM external_usage_issues")]
    assert names.count("canonical_stat_ignored") == 3
    cols = [c[0] for c in nfl.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'external_player_week_usage'").fetchall()]
    assert "targets" not in cols and "receiving_yards" not in cols
    build_player_week_usage(nfl, SEASON)
    u = fetch_dicts(nfl, "SELECT * FROM player_week_usage WHERE gsis_id = 'G1' AND week = 1")[0]
    assert u["targets"] == 8 and u["receiving_yards"] == 80.0 and u["fantasy_points_ppr"] == 20.0   # nflverse values
    assert u["tprr"] == pytest.approx(8 / 30) and u["yprr"] == pytest.approx(80 / 30)


def test_source_ratio_discrepancy_warns_and_keeps_derived(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1", source_tprr=0.40, source_yprr=2.70)])   # derived: 0.267 / 2.67
    build_player_week_usage(nfl, SEASON)
    rows = fetch_dicts(nfl, "SELECT * FROM player_week_usage WHERE routes IS NOT NULL")
    issues = check_external_ratios(rows)
    assert [i.check_name for i in issues] == ["external_tprr_discrepancy"]    # YPRR is within tolerance
    assert issues[0].severity == "warning"
    assert rows[0]["tprr"] == pytest.approx(8 / 30) and rows[0]["source_tprr"] == 0.40   # nothing overwritten


def test_route_participation_consistency_warning():
    ok = {"week": 1, "gsis_id": "G1", "routes": 30, "route_participation": 0.75, "dropbacks": 40}
    off = {**ok, "route_participation": 0.30}
    assert check_route_participation([ok]) == []
    assert check_route_participation([off])[0].check_name == "route_participation_inconsistent"


def test_partial_week_coverage_warning(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1")])     # KC covered, BUF (also played week 1) missing
    issues = fetch_dicts(nfl, "SELECT * FROM external_usage_issues WHERE check_name = 'partial_week_coverage'")
    assert issues and "1/2 teams" in issues[0]["detail"]


# --------------------------------------------------------- idempotency / history --

def test_same_file_twice_is_idempotent_noop(nfl, tmp_path):
    rows = [base_row(gsis_id="G1"), base_row(gsis_id="G3", player_name="Charlie Tight", routes=20)]
    path = write_csv(tmp_path, "same.csv", rows)
    first = import_external_usage(nfl, path, provider="test_provider", dataset="weekly_routes")
    second = import_external_usage(nfl, path, provider="test_provider", dataset="weekly_routes")
    assert first["status"] == "imported"
    assert second["status"] == "duplicate_noop" and second["duplicate_of"] == first["import_id"]
    assert nfl.execute("SELECT count(*) FROM external_player_week_usage").fetchone()[0] == 2
    assert nfl.execute("SELECT count(*) FROM external_usage_imports").fetchone()[0] == 2   # both recorded


def test_corrected_file_updates_row_and_keeps_history(nfl, tmp_path):
    first = imp(nfl, tmp_path, [base_row(provider_player_id="P1", gsis_id="G1", routes=30)], name="w1.csv")
    second = imp(nfl, tmp_path, [base_row(provider_player_id="P1", gsis_id="G1", routes=12)], name="w1_fix.csv")
    assert second["status"] == "imported"
    rows = fetch_dicts(nfl, "SELECT * FROM external_player_week_usage")
    assert len(rows) == 1 and rows[0]["routes"] == 12 and rows[0]["import_id"] == second["import_id"]
    imports = fetch_dicts(nfl, "SELECT import_id, source_filename FROM external_usage_imports ORDER BY imported_at")
    assert {i["source_filename"] for i in imports} == {"w1.csv", "w1_fix.csv"}       # provenance inspectable
    rev = fetch_dicts(nfl, "SELECT * FROM external_player_week_usage_revisions")
    assert len(rev) == 1 and rev[0]["import_id"] == first["import_id"] and rev[0]["superseded_by"] == second["import_id"]
    assert '"routes": 30' in rev[0]["previous"]
    sus = fetch_dicts(nfl, "SELECT * FROM external_usage_issues WHERE check_name = 'suspicious_provider_correction'")
    assert sus and "30 -> 12" in sus[0]["detail"]


# ---------------------------------------------------------------- providers --

def test_multiple_providers_never_silently_merge(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1", routes=30)], name="a.csv", provider="provider_a")
    imp(nfl, tmp_path, [base_row(gsis_id="G1", routes=25)], name="b.csv", provider="provider_b")
    sel = select_provider(nfl, SEASON)
    assert sel["provider"] is None and sel["status"] == "conflict_unresolved"
    build_player_week_usage(nfl, SEASON)
    u = fetch_dicts(nfl, "SELECT routes, route_data_provider FROM player_week_usage WHERE gsis_id='G1' AND week=1")[0]
    assert u["routes"] is None and u["route_data_provider"] is None          # neither value applied
    build_player_week_usage(nfl, SEASON, preferred_provider="provider_b")
    u = fetch_dicts(nfl, "SELECT routes, route_data_provider FROM player_week_usage WHERE gsis_id='G1' AND week=1")[0]
    assert u["routes"] == 25 and u["route_data_provider"] == "provider_b"
    assert select_provider(nfl, SEASON, "provider_c")["status"] == "conflict_unresolved"


def test_single_provider_is_used_and_conflicting_datasets_give_null(nfl, tmp_path):
    imp(nfl, tmp_path, [base_row(gsis_id="G1", routes=30, slot_rate=0.5)], name="r.csv", dataset="routes")
    imp(nfl, tmp_path, [base_row(gsis_id="G1", routes=28, wide_rate=0.5)], name="a.csv", dataset="alignment")
    build_player_week_usage(nfl, SEASON)
    u = fetch_dicts(nfl, "SELECT * FROM player_week_usage WHERE gsis_id='G1' AND week=1")[0]
    assert u["route_data_provider"] == "test_provider"
    assert u["routes"] is None                         # 30 vs 28 across datasets: refuse to pick
    assert u["slot_rate"] == 0.5 and u["wide_rate"] == 0.5
    assert u["route_data_dataset"] == "alignment,routes"


# ------------------------------------------------------------- score isolation --

SCORE_FIELDS = ["opportunity_score", "production_score", "usage_trend_score", "trend_label", "opportunity_tier",
                "role_trend", "market_signal", "classification", "value_score", "fp_per_game", "xfp_per_game",
                "fp_minus_xfp", "value_gap_label", "games", "opportunity_basis", "reasons", "components",
                "opportunity_score_100", "production_score_100", "value_score_100", "rating_scale_version"]
CANONICAL_USAGE = ["targets", "receptions", "receiving_yards", "air_yards", "target_share", "air_yards_share",
                   "wopr", "carries", "fantasy_points_ppr", "xfp", "offense_pct", "opportunities"]


def _snapshot(con):
    build_player_week_usage(con, SEASON)
    build_player_scores(con, SEASON)
    build_context(con, SEASON, 4)
    usage = {(r["gsis_id"], r["week"]): {c: r[c] for c in CANONICAL_USAGE}
             for r in fetch_dicts(con, "SELECT * FROM player_week_usage")}
    scores = {r["gsis_id"]: {c: r[c] for c in SCORE_FIELDS} for r in fetch_dicts(con, "SELECT * FROM player_scores")}
    labels = {r["gsis_id"]: r["labels"] for r in fetch_dicts(con, "SELECT gsis_id, labels FROM player_context")}
    return usage, scores, labels


def test_external_routes_never_change_scores_signals_or_context(nfl, tmp_path):
    before = _snapshot(nfl)
    assert before[1] and all(r["routes"] is None for r in fetch_dicts(nfl, "SELECT routes FROM player_week_usage"))
    imp(nfl, tmp_path, [base_row(gsis_id=g, player_name=n, week=w, routes=r, route_participation=rp, slot_rate=0.9,
                                 two_minute_route_share=1.0, source_tprr=0.9)
                        for (g, n, w, r, rp) in [("G1", "Alpha Receiver", 1, 35, 0.95), ("G1", "Alpha Receiver", 2, 5, 0.1),
                                                 ("G1", "Alpha Receiver", 3, 40, 1.0), ("G3", "Charlie Tight", 1, 2, 0.05),
                                                 ("G3", "Charlie Tight", 3, 38, 0.99)]])
    after = _snapshot(nfl)
    assert fetch_dicts(nfl, "SELECT count(*) n FROM player_week_usage WHERE routes IS NOT NULL")[0]["n"] == 5
    assert after[0] == before[0]       # canonical nflverse usage identical
    assert after[1] == before[1]       # opportunity/value/market signal/etc identical
    assert after[2] == before[2]       # context labels identical


def test_left_join_keeps_players_without_external_coverage(nfl, tmp_path):
    build_player_week_usage(nfl, SEASON)
    n_before = nfl.execute("SELECT count(*) FROM player_week_usage").fetchone()[0]
    imp(nfl, tmp_path, [base_row(gsis_id="G1")])
    build_player_week_usage(nfl, SEASON)
    rows = fetch_dicts(nfl, "SELECT gsis_id, week, routes FROM player_week_usage")
    assert len(rows) == n_before
    assert {(r["gsis_id"], r["week"]) for r in rows if r["routes"] is not None} == {("G1", 1)}
    assert any(r["gsis_id"] == "G2" and r["routes"] is None for r in rows)


def test_waiver_and_scoring_code_never_reference_external_metrics():
    import app.analytics.player_scores as ps
    import app.analytics.trends as tr
    import app.analytics.waivers as wv
    src = Path(ps.__file__).read_text() + Path(wv.__file__).read_text() + Path(tr.__file__).read_text()
    for name in ("routes", "route_participation", "tprr", "yprr", "slot_rate", "pass_block", "route_share"):
        assert name not in src, name


# ---------------------------------------------------------------- repo safety --

def test_staging_and_import_paths_are_gitignored():
    repo = Path(__file__).resolve().parents[2]
    if subprocess.run(["git", "-C", str(repo), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    for p in ("data/imports/external/sample.csv", "data/staging/provider_dump.csv", "data/raw/espn/x.json",
              "data/fantasy.duckdb"):
        res = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", p], capture_output=True)
        assert res.returncode == 0, f"{p} is not git-ignored"


def test_parse_rows_never_drops_rows_silently():
    rows = [base_row(player_name=f"P{i}", team="KC") for i in range(5)]
    parsed = parse_rows(list(rows[0].keys()), [{k: str(v) for k, v in r.items()} for r in rows],
                        provider="x", dataset="y")
    assert len(parsed.rows) == 5 and not parsed.errors


# ------------------------------------------------------------ timestamps (UTC-naive) --

from datetime import datetime  # noqa: E402

from app.external.contract import _parse_ts  # noqa: E402

SAME_INSTANT = ["2026-10-07T13:00:00Z", "2026-10-07T13:00:00+00:00", "2026-10-07T09:00:00-04:00",
                "2026-10-07T18:30:00+05:30", "2026-10-07T13:00:00"]


@pytest.fixture
def machine_tz():
    import os
    import time
    original = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()
    yield set_tz
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.mark.parametrize("value", SAME_INSTANT)
@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Asia/Kolkata", "Australia/Sydney"])
def test_timestamps_normalize_to_utc_naive_regardless_of_machine_tz(machine_tz, tz, value):
    machine_tz(tz)
    got = _parse_ts(value)
    assert got == datetime(2026, 10, 7, 13, 0, 0) and got.tzinfo is None


def test_offset_example_and_equivalent_instants():
    assert _parse_ts("2026-10-07T09:00:00-04:00") == datetime(2026, 10, 7, 13, 0)
    assert len({_parse_ts(v) for v in SAME_INSTANT}) == 1
    assert _parse_ts("2026-10-07T13:00:00") == datetime(2026, 10, 7, 13, 0)   # naive = already UTC
    assert _parse_ts("") is None and _parse_ts("NA") is None


def test_offset_timestamp_is_persisted_as_utc(nfl, tmp_path, machine_tz):
    machine_tz("America/New_York")
    rec = imp(nfl, tmp_path, [base_row(gsis_id="G1", source_updated_at="2026-10-07T09:00:00-04:00")])
    r = ext_rows(nfl)[(1, "Alpha Receiver")]
    assert r["source_updated_at"] == datetime(2026, 10, 7, 13, 0)
    imp_row = fetch_dicts(nfl, "SELECT source_updated_at FROM external_usage_imports WHERE import_id = ?",
                          [rec["import_id"]])[0]
    assert imp_row["source_updated_at"] == datetime(2026, 10, 7, 13, 0)


# ------------------------------------------------------------- one season per file --

def test_mixed_season_file_is_rejected_with_history_and_no_rows(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [base_row(gsis_id="G1", season=2026), base_row(gsis_id="G3", player_name="Charlie Tight",
                                                                            season=2025)])
    assert rec["status"] == "rejected" and rec["season"] is None
    imports = fetch_dicts(nfl, "SELECT * FROM external_usage_imports")
    assert len(imports) == 1 and imports[0]["status"] == "rejected"
    issues = fetch_dicts(nfl, "SELECT * FROM external_usage_issues WHERE import_id = ?", [rec["import_id"]])
    assert [i["check_name"] for i in issues if i["severity"] == "error"] == ["mixed_seasons"]
    assert "2025" in issues[0]["detail"] and "2026" in issues[0]["detail"]
    assert nfl.execute("SELECT count(*) FROM external_player_week_usage").fetchone()[0] == 0


def test_single_season_file_records_its_season(nfl, tmp_path):
    rec = imp(nfl, tmp_path, [base_row(gsis_id="G1"), base_row(gsis_id="G1", week=2)])
    assert rec["status"] == "imported" and rec["season"] == SEASON


def test_cli_rejects_mixed_seasons_without_running_analytics(tmp_path):
    import os
    import sys
    db = tmp_path / "cli.duckdb"
    path = write_csv(tmp_path, "mixed.csv", [base_row(gsis_id="G1", season=2026),
                                             base_row(gsis_id="G1", season=2025, week=2)])
    script = Path(__file__).resolve().parents[2] / "scripts" / "import_external_usage.py"
    env = {**os.environ, "FANTASY_DB_PATH": str(db)}
    res = subprocess.run([sys.executable, str(script), str(path), "--provider", "p", "--dataset", "d"],
                         capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parents[1])
    assert res.returncode == 1, res.stdout + res.stderr
    assert '"status": "rejected"' in res.stdout and "mixed_seasons" in res.stdout
    from app.db.connection import connect
    con = connect(db, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM external_usage_imports WHERE status = 'rejected'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM external_usage_issues WHERE check_name = 'mixed_seasons'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM external_player_week_usage").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM sync_runs WHERE source = 'analytics'").fetchone()[0] == 0
    finally:
        con.close()
