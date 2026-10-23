"""Actual league scoring fidelity, issue #254 phase 2 (docs: fantasy-intel/docs/compatibility/254-scoring-support.md).

Synthetic scoring only. Pins: exact outcomes for supported rules (unusual QB scoring, a computed zero, negative results,
FX-17's three FLEX and OP); no fabricated independent result for a TE slot premium, kicker per-yard rules, IDP rules or
a missing statistic; unsupported rule ids reaching the league-compatibility/v1 diagnostic from the league rules; a
source named only for the numbers it supplied (L2, S3); and scoring fingerprints and caches that cannot mix leagues."""
import json
import re
from pathlib import Path

import duckdb
import pytest

import compat_fixtures as cf
from app.analytics import projection_source as ps
from app.analytics.dst_store import scoring_fingerprint as dst_fingerprint
from app.analytics.kicker_scoring import expected_points as kicker_points
from app.analytics.kicker_scoring import resolve_kicker_scoring
from app.analytics.league_compatibility import build_compatibility, load_facts
from app.analytics.league_rules import build_league_rules
from app.analytics.league_scoring import points, resolve_skill_scoring
from app.analytics.legacy_league import latest_league
from app.analytics.lineup import lineup_slots
from app.analytics.rescore import apply_reference, rescore_kicker_rows
from app.analytics.start_sit import NO_PROJECTIONS, projection_source_label
from app.analytics.team_lineups import RosterPlayer, project_week, source_label
from app.analytics.trade_data import load_league_state
from app.db.connection import fetch_dicts, init_db
from app.sleeper.scoring import to_scoring_items, unsupported_family
from app.sleeper.write import write_sleeper

from synthetic_forecast import SCORING_ITEMS
from test_independent_projections import live  # noqa: F401  (fixture)
from test_projection_source import WEEKLY_SHA, _ifp2, _store
from test_rescore import _publish_reference, _set_scoring
from test_sleeper_write import FIX, payloads

APP = Path(__file__).resolve().parents[1] / "app"

# An unusual QB scoring (ESPN items): 6-point pass TD, 0.05 per passing yard, -3 per INT, +0.5 per completion,
# -0.25 per attempt, plus full PPR and 0.1 per rushing/receiving yard and -2 per fumble lost.
UNUSUAL_QB = [{"statId": 3, "points": 0.05}, {"statId": 4, "points": 6.0}, {"statId": 20, "points": -3.0},
              {"statId": 2005, "points": 0.5}, {"statId": 2004, "points": -0.25}, {"statId": 53, "points": 1.0},
              {"statId": 42, "points": 0.1}, {"statId": 24, "points": 0.1}, {"statId": 72, "points": -2.0}]
BOX = {"passing_yards": 0.0, "passing_tds": 0.0, "passing_interceptions": 0.0, "completions": 0.0, "attempts": 0.0,
       "receptions": 0.0, "receiving_yards": 0.0, "rushing_yards": 0.0, "fumbles_lost_total": 0.0}


@pytest.fixture
def league(tmp_path):
    opened = []

    def make(row):
        con = duckdb.connect(str(tmp_path / f"{len(opened)}.duckdb"))
        init_db(con)
        cf.import_row(con, cf.MATRIX[row])
        opened.append(con)
        return con
    yield make
    for c in opened:
        c.close()


# ---- exact outcomes for supported rules ---------------------------------------------------------------------------------

def test_unusual_qb_scoring_zero_and_negative_box_scores_score_exactly():
    s = resolve_skill_scoring(UNUSUAL_QB)
    assert s.supported
    qb = {**BOX, "passing_yards": 280.0, "passing_tds": 2.0, "passing_interceptions": 1.0, "completions": 24.0,
          "attempts": 36.0, "rushing_yards": 12.0}
    assert points(qb, s, "QB") == pytest.approx(14.0 + 12.0 - 3.0 + 12.0 - 9.0 + 1.2)        # 27.2
    assert points(BOX, s, "WR") == 0.0                                                       # a computed zero
    bad = {**BOX, "passing_yards": 40.0, "passing_interceptions": 3.0, "completions": 4.0, "attempts": 14.0,
           "fumbles_lost_total": 1.0}
    assert points(bad, s, "QB") == pytest.approx(2.0 - 9.0 + 2.0 - 3.5 - 2.0)               # -10.5
    assert points({**qb, "completions": None}, s, "QB") is None                              # missing is never 0


def test_fx17_three_flex_and_op_lineup_total_is_exact_with_zero_and_negative_projections():
    """Every one of the 17 starting slots filled once; a negative OP and K are starts the engine must make (no
    better eligible player), a 0.0 D/ST and FLEX are values, and the total is their exact sum."""
    slots = lineup_slots(cf.FX17)
    vals = {"QB": [26.0, 18.2, -1.5], "RB": [14.0, 9.5, 0.0, 6.0], "WR": [12.0, 11.0, 8.0, 7.5, 5.0, -0.5],
            "TE": [9.0, 4.0, 2.5], "K": [-2.0], "D/ST": [0.0]}
    players, proj, pid = [], {}, 1
    for pos, vs in vals.items():
        for v in vs:
            players.append(RosterPlayer(pid, pos, "KC"))
            proj[pid] = {3: v}
            pid += 1
    week = project_week(players, slots, 3, proj)
    starters = [a for a in week["assignment"] if a["player_id"] is not None]
    assert len(starters) == 17 == len({a["player_id"] for a in starters})
    assert sorted(a["slot"] for a in week["assignment"] if a["slot"] == "FLEX") == ["FLEX"] * 3
    assert week["projection_complete"] and week["projected_lineup_points"] == 130.2
    by_slot = {(a["slot"], a["player_id"]): a["points"] for a in starters}
    assert by_slot[("OP", 13)] == -0.5 and by_slot[("K", 17)] == -2.0 and by_slot[("D/ST", 18)] == 0.0
    assert week["projected_lineup_points"] == round(sum(a["points"] for a in starters), 2)


# ---- unsupported rules never become an independent number ----------------------------------------------------------

def test_a_kicker_row_without_a_scored_outcome_is_unknown_never_zero():
    full = {"fg": {o: {b: 0.4 for b in ("0_19", "20_29", "30_39", "40_49", "50_59", "60_plus")}
                   for o in ("made", "missed", "blocked")}, "pat": {"made": 2.0, "missed": 0.1, "blocked": 0.0}}
    k = resolve_kicker_scoring([{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0},
                                {"statId": 198, "points": 5.0}, {"statId": 201, "points": 5.0},
                                {"statId": 85, "points": -1.0}, {"statId": 86, "points": 1.0},
                                {"statId": 88, "points": -1.0}])
    assert kicker_points(k, full) == pytest.approx(0.4 * (3 + 3 + 3 + 4 + 5 + 5) - 0.4 * 6 * 2 + 2.0 - 0.1)
    no_missed = {**full, "fg": {o: v for o, v in full["fg"].items() if o != "missed"}}
    no_long = {**full, "fg": {**full["fg"], "made": {b: v for b, v in full["fg"]["made"].items() if b != "60_plus"}}}
    no_pat_miss = {**full, "pat": {"made": 2.0, "blocked": 0.0}}
    for row in (no_missed, no_long, no_pat_miss):
        assert kicker_points(k, row) is None
    run = [{"coverage_status": "projected", "projected_points": 7.0, "interval_low": 2.0, "interval_high": 12.0,
            "p_play": 1.0, "expected_points": 7.0, "flags": "[]", "stat_line": json.dumps({"scoring_stats": no_missed})}]
    assert rescore_kicker_rows(run, k) is None           # the run is not re-scored, so no K number is made up
    # a statistic the league does not score may be absent
    assert kicker_points(resolve_kicker_scoring([{"statId": 80, "points": 3.0}]),
                         {"fg": {"made": full["fg"]["made"]}, "pat": {}}) is not None


TE_SLOT_PREMIUM = [{**i, "pointsOverrides": {"6": i["points"] + 0.5}} if i["statId"] == 53 else i for i in SCORING_ITEMS]


@pytest.mark.parametrize("items", [
    pytest.param(TE_SLOT_PREMIUM, id="espn-te-slot-premium"),
    pytest.param(SCORING_ITEMS + [{"statId": 107, "points": 1.0}], id="espn-unimplemented-defensive-stat"),
])
def test_a_te_slot_premium_or_an_unimplemented_stat_gets_no_independent_skill_numbers(live, items):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    _publish_reference(con)
    _set_scoring(con, items)
    assert apply_reference(con, 2026)["skill"] == {"copied": 0, "rescored": 0, "skipped": 0}
    assert fetch_dicts(con, "SELECT count(*) AS n FROM independent_projection_rows")[0]["n"] == 0
    shown = ps.weekly(con, 2026, 6, "independent")
    assert shown["available"] is False and not set(shown["origin"].values()) - {ps.ESPN}


def test_sleeper_kicker_per_yard_and_idp_rules_never_feed_a_model():
    items, unsupported, _ = to_scoring_items({**cf.SLEEPER_HALF_PPR, "fgm_yds": 0.1, "idp_tkl": 1.0, "idp_sack": 3.0})
    assert sorted(unsupported) == ["fgm_yds", "idp_sack", "idp_tkl"]
    assert [unsupported_family(k) for k in sorted(unsupported)] == ["kicker", "idp", "idp"]
    assert not resolve_kicker_scoring(items).supported                    # the kicker model abstains
    skill = resolve_skill_scoring(items)
    plain = resolve_skill_scoring(to_scoring_items(cf.SLEEPER_HALF_PPR)[0])
    assert skill.supported and skill.fingerprint == plain.fingerprint     # IDP rules never reach the skill model
    assert dst_fingerprint(items) == dst_fingerprint(to_scoring_items(cf.SLEEPER_HALF_PPR)[0])


# ---- unsupported rule ids through the D-12 contract ----------------------------------------------------------------

def test_unsupported_rule_ids_reach_the_diagnostic_from_the_league_rules_per_family(league):
    con = league("M6s")                   # Sleeper half PPR + bonus_pass_yd_300 + an unknown key
    lg = latest_league(con)
    rows = {r["code"]: r for r in build_league_rules(lg)["engine_support"]}
    assert rows["skill_projections"]["unsupported_rules"] == ["bonus_pass_yd_300", "zz_new_rule"]
    # the kicker abstains on the skill keys too (their translated item carries base points a K slot would earn)
    assert rows["kicker_projections"]["unsupported_rules"] == ["bonus_pass_yd_300", "zz_new_rule"]
    assert "unsupported_rules" not in rows["dst_projections"]
    out = build_compatibility(lg, load_facts(con, lg))
    ev = {e["code"]: e for e in out["dimensions"]["scoring"]["evidence"]}
    assert ev["skill_projections"]["value"] == {"unsupported_rules": ["bonus_pass_yd_300", "zz_new_rule"]}
    assert ev["kicker_projections"]["value"] == {"unsupported_rules": ["bonus_pass_yd_300", "zz_new_rule"]}


def test_each_family_names_only_its_own_sleeper_keys():
    league = json.loads((FIX / "leagues" / "1180000000000000001" / "league.json").read_text())
    league["scoring_settings"] = {**cf.SLEEPER_HALF_PPR, "fgm_yds": 0.1, "yds_allow_bonus": 1.0, "idp_tkl": 1.0}
    con = duckdb.connect()
    init_db(con)
    con.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    write_sleeper(con, payloads(league=league))
    rows = {r["code"]: r for r in build_league_rules(fetch_dicts(con, "SELECT * FROM leagues")[0])["engine_support"]}
    assert rows["skill_projections"]["status"] == "handled" and "unsupported_rules" not in rows["skill_projections"]
    assert rows["kicker_projections"]["unsupported_rules"] == ["fgm_yds"]
    assert "fgm_yds" in rows["kicker_projections"]["detail"] and "idp_tkl" not in rows["kicker_projections"]["detail"]
    assert rows["dst_projections"]["unsupported_rules"] == ["yds_allow_bonus"]
    assert "fgm_yds" not in rows["dst_projections"]["detail"]


# ---- a source is named only for the numbers it supplied (L2) --------------------------------------------------------

def test_served_label_names_only_sources_that_supplied_numbers():
    assert ps.served_label([ps.OURS, "kicker_model", "dst_carried", ps.OURS_ZERO]) == "Ours"
    assert ps.served_label([ps.ESPN, ps.ESPN]) == "ESPN"
    assert ps.served_label([ps.OURS, ps.ESPN]) == "Ours and ESPN"
    assert ps.served_label([]) == "Unavailable"


def test_start_sit_names_espn_when_every_shown_number_is_the_providers_fallback():
    proj = {1: 12.0, 2: 8.5, 3: None}
    assert projection_source_label("independent", True, proj, {1: "espn", 2: "espn", 3: "espn"}) == "ESPN"
    assert projection_source_label("independent", True, proj, {1: "ours", 2: "espn"}) == "Independent model"
    assert projection_source_label("independent", True, {1: None}, {1: "ours"}) == NO_PROJECTIONS
    assert projection_source_label("espn", False, proj) == "ESPN"


def test_team_lineup_tools_never_name_ours_over_a_state_without_numbers(league):
    assert source_label("independent", {}) == "Unavailable"
    assert source_label("independent", {7: {}}) == "Unavailable"
    assert source_label("independent", {7: {3: 0.0}}) == "Ours"          # a projected 0 is a number
    assert source_label("espn", {}) == "ESPN" and source_label("independent") == "Ours"
    con = league("M3")                    # Sleeper: no provider projections and no Ours run stored
    state = load_league_state(con)
    assert state.projection_source == "independent" and source_label(state.projection_source, state.projections) \
        == "Unavailable"
    from app.analytics.projections import build_ros_projections
    build_ros_projections(con, cf.SEASON)
    labels = {r["projection_source"] for r in fetch_dicts(con, "SELECT projection_source FROM player_ros_projections")}
    assert labels == {"Unavailable"}


# ---- S3: each actual-points row names the source that supplied it -----------------------------------------------------

def test_sleeper_actual_points_name_their_source_per_row(tmp_path):
    from app.api.routes import league_points_sources
    con = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(con)
    con.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    write_sleeper(con, payloads())
    fa = fetch_dicts(con, """SELECT p.espn_player_id, p.position, min(m.gsis_id) AS gsis_id FROM fantasy_players p
                             JOIN nfl_ff_playerids m ON m.espn_id = p.espn_player_id
                             WHERE p.position IN ('QB', 'RB', 'WR', 'TE') AND m.gsis_id IS NOT NULL
                             GROUP BY 1, 2 ORDER BY 1 LIMIT 1""")
    # weeks 3 and 4 have no Sleeper matchup in the fixture: no roster scored him, so the box score fills them
    assert fa, "the Sleeper fixture has a mapped skill player"
    fa = fa[0]
    lg = fetch_dicts(con, "SELECT season, scoring_items FROM leagues")[0]
    scoring = resolve_skill_scoring(json.loads(lg["scoring_items"]))
    assert scoring.supported
    cols = sorted({c for w in (scoring.weights, *scoring.position_weights.values()) for c in w})
    con.execute(f"CREATE TABLE nfl_weekly_stats (player_id VARCHAR, season INTEGER, week INTEGER, season_type VARCHAR, "
                f"position VARCHAR, {', '.join(f'{c} DOUBLE' for c in cols)})")
    full = {c: 1.0 for c in cols}
    for week, line in ((3, full), (4, {**full, cols[0]: None})):
        con.execute(f"INSERT INTO nfl_weekly_stats VALUES (?, ?, ?, 'REG', ?, {', '.join('?' * len(cols))})",
                    [fa["gsis_id"], lg["season"], week, fa["position"], *[line[c] for c in cols]])
    write_sleeper(con, payloads())         # a rerun rewrites the season's points, box scores included
    rows = {r["scoring_period"]: r for r in fetch_dicts(
        con, "SELECT scoring_period, actual_points, actual_source FROM espn_player_weekly_points "
             "WHERE espn_player_id = ? ORDER BY 1", [fa["espn_player_id"]])}
    assert rows[3]["actual_source"] == "box_score" and rows[3]["actual_points"] == pytest.approx(
        round(points(full, scoring, fa["position"]), 2))
    assert rows[4]["actual_points"] is None and rows[4]["actual_source"] is None      # missing: no number, no source
    provider = fetch_dicts(con, "SELECT DISTINCT actual_source FROM espn_player_weekly_points WHERE scoring_period = 5 "
                                "AND actual_points IS NOT NULL")
    assert [r["actual_source"] for r in provider] == ["provider"]
    shown = league_points_sources(list(rows.values()))
    # his rows mix sources, so every row names its own (and a missing one names none)
    assert [r["actual_source"] for r in shown] == [r["actual_source"] for r in rows.values()]
    assert shown[0]["actual_source"] == "box_score" and shown[1]["actual_source"] is None
    # rows that are all the provider's keep the shape every ESPN league has
    assert league_points_sources([{"scoring_period": 5, "actual_points": 3.3, "actual_source": "provider"}]) == \
        [{"scoring_period": 5, "actual_points": 3.3}]
    con.close()


# ---- scoring fingerprints and caches never mix leagues ---------------------------------------------------------------

def test_every_scored_rule_changes_its_familys_fingerprint():
    base = resolve_skill_scoring(UNUSUAL_QB)
    for i, item in enumerate(UNUSUAL_QB):
        changed = [*UNUSUAL_QB[:i], {**item, "points": item["points"] + 0.5}, *UNUSUAL_QB[i + 1:]]
        assert resolve_skill_scoring(changed).fingerprint != base.fingerprint, item
    te = resolve_skill_scoring(to_scoring_items({**cf.SLEEPER_HALF_PPR, "bonus_rec_te": 0.5})[0])
    assert te.fingerprint != resolve_skill_scoring(to_scoring_items(cf.SLEEPER_HALF_PPR)[0]).fingerprint
    k_items = [{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0}, {"statId": 74, "points": 5.0},
               {"statId": 85, "points": -1.0}, {"statId": 86, "points": 1.0}, {"statId": 88, "points": -1.0}]
    k = resolve_kicker_scoring(k_items).fingerprint
    for i, item in enumerate(k_items):
        changed = [*k_items[:i], {**item, "points": item["points"] + 1.0}, *k_items[i + 1:]]
        assert resolve_kicker_scoring(changed).fingerprint != k, item
    d_items = [i for i in cf.espn_scoring() if (i.get("pointsOverrides") or {}).get("16")]
    d = dst_fingerprint(d_items)[0]
    for i, item in enumerate(d_items):
        changed = [*d_items[:i], {**item, "pointsOverrides": {"16": item["pointsOverrides"]["16"] + 1.0}},
                   *d_items[i + 1:]]
        assert dst_fingerprint(changed)[0] != d, item


def test_a_league_file_never_serves_a_run_made_for_another_scoring(live):  # noqa: F811
    """Another league's run (another fingerprint) in this league's file is never selected, so no consumer sees it;
    apply_reference rebuilds the derived tables from the league's own scoring (test_rescore)."""
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    assert ps.select_run(con, 2026, 6)["run"] is not None
    _set_scoring(con, [{**i, "points": 0.5} if i["statId"] == 53 else i for i in SCORING_ITEMS])
    sel = ps.select_run(con, 2026, 6)
    assert sel["run"] is None and sel["scoring_fingerprint"] != resolve_skill_scoring(SCORING_ITEMS).fingerprint
    assert ps.weekly(con, 2026, 6, "independent")["available"] is False
    detail: dict = {}
    ps.league_projections(con, 2026, [6], "independent", detail)
    assert {origin for origin, _ in detail.get(6, {}).values()} <= {ps.ESPN}     # only the provider's own numbers


def test_the_kicker_points_cache_is_keyed_by_the_scoring_itself():
    from app.analytics import kicker_model
    a = resolve_kicker_scoring([{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0}])
    b = resolve_kicker_scoring([{"statId": 80, "points": 3.0}, {"statId": 77, "points": 5.0}])
    pa, pb = kicker_model._pts_arrays(a), kicker_model._pts_arrays(b)
    assert a.fingerprint != b.fingerprint and a.fingerprint in kicker_model._PTS_CACHE
    assert (pa[0] != pb[0]).any()


def test_the_response_cache_key_covers_league_versions_and_perspective():
    from app.api.deps import LeagueScope
    import dataclasses
    fields = {f.name for f in dataclasses.fields(LeagueScope)}
    assert {"league_id", "data_version", "global_version", "visibility_epoch", "perspective"} <= fields
    key = LeagueScope.cache_key.fget
    base = dict(league_id=1, data_version="d1", global_version="g1", visibility_epoch=0, perspective=None)

    class S:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    k0 = key(S(**base))
    for change in ({"league_id": 2}, {"data_version": "d2"}, {"global_version": "g2"}, {"visibility_epoch": 1},
                   {"perspective": 3}):
        assert key(S(**{**base, **change})) != k0, change


# Every module-level mutable cache in app/, with why it cannot serve one league's rules or values to another. A new one
# fails this test until it is reviewed and listed (#254 acceptance: fingerprints and caches never leak across leagues).
MODULE_CACHES = {
    "analytics/kicker_model.py:_PTS_CACHE": "keyed by the kicker scoring fingerprint (the effective per-kick table)",
    "analytics/kicker_venues.py:@lru_cache": "the static stadium registry file; no league data",
    "api/action_center.py:_CACHES": "summary cache keyed by user, league, data/global versions, visibility, team, provider",
    "api/action_center.py:_POOLS": "thread pools; no data",
    "api/action_center.py:_ADMISSION": "a semaphore; no data",
    "api/action_center.py:_FLIGHTS": "in-flight builds per user, holding the league ids that user was authorized for",
    "api/deps.py:_MD_CONS": "connections per configured database name (operator setting)",
    "api/deps.py:_SNAP": "the snapshot cache and the classic deployment's single database",
    "api/deps.py:_LAYERED": "the per-process layered database registry; files are pinned per league and version",
    "api/league_scope.py:_CACHE": "the league file cache, keyed by league id and versions (league_files.pin_pair)",
    "api/league_scope.py:_RESPONSES": "responses keyed by LeagueScope.cache_key (league, versions, epoch, perspective)",
    "api/provider_copy.py:_CLASSIC": "the classic single-league file's provider, keyed by path and mtime",
    "api/trade_discovery.py:_CACHE": "searches keyed by data version, league id, season, team and search inputs",
    "api/trade_discovery.py:_VIEWS": "views keyed by data version, league id, season, team and search inputs",
    "api/trade_discovery.py:_NEED_CACHE": "needs keyed by data version, league id, season, team and need",
    "control_plane/jwt_auth.py:_CLIENTS": "JWKS clients per issuer URL; no league data",
    "control_plane/pg.py:_PLANES": "control-plane connections per DSN; no league data",
    "leagues/providers/registry.py:_cache": "provider adapters per provider and settings signature; no league data",
}


def test_every_module_level_cache_in_the_app_is_reviewed_for_league_isolation():
    assign = re.compile(r"^(_?[A-Za-z][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*(\{\}|dict\(\)|(?:collections\.)?OrderedDict\(\)"
                        r"|defaultdict\([^)]*\)|set\(\)|\[\])\s*(#.*)?$", re.M)
    decorated = re.compile(r"^\s*@(?:functools\.)?(lru_cache|cache)\b", re.M)
    found = set()
    for f in APP.rglob("*.py"):
        text, rel = f.read_text(), f.relative_to(APP).as_posix()
        found |= {f"{rel}:{m.group(1)}" for m in assign.finditer(text)}
        found |= {f"{rel}:@{m.group(1)}" for m in decorated.finditer(text)}
    assert found == set(MODULE_CACHES), (sorted(found - set(MODULE_CACHES)), sorted(set(MODULE_CACHES) - found))


# ---- review fixes on #276 ---------------------------------------------------------------------------------------------

def _served(tmp_path, monkeypatch, row: str, drop_source: bool = False):
    from fastapi.testclient import TestClient
    db = tmp_path / f"{row}.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    cf.import_row(con, cf.MATRIX[row])
    if drop_source:          # a league file built before the column existed
        con.execute("ALTER TABLE espn_player_weekly_points DROP COLUMN actual_source")
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return db, TestClient(app, raise_server_exceptions=False)


def test_kdst_streaming_provenance_on_a_sleeper_league_without_numbers_is_ours_unavailable(league, tmp_path,
                                                                                         monkeypatch):
    """The plan's label is "Unavailable" when the Ours state holds no number; the provenance decides the source from
    the league, never from that label (it used to fall to "ESPN weekly projections")."""
    from app.analytics.kdst_streaming import build_kdst_streaming
    from app.api.schedule_intelligence import kdst_streaming_provenance
    con = league("M3")
    out = build_kdst_streaming(con, team_id=1)
    assert out["projection_source"] == "Unavailable"
    sig = next(s for s in kdst_streaming_provenance(con, out)["signals"] if s["signal"] == "projections")
    assert sig["source"] == "fantasy_intel" and "ESPN" not in sig["label"] and sig["reason_unavailable"]
    _, api = _served(tmp_path, monkeypatch, "M3")
    r = api.get("/api/schedule-intelligence/kdst-streaming?team_id=1")
    assert r.status_code == 200, r.text[:300]
    sig = next(s for s in r.json()["provenance"]["signals"] if s["signal"] == "projections")
    assert sig["source"] == "fantasy_intel" and "ESPN" not in json.dumps(r.json()["provenance"])


def test_an_older_league_file_without_actual_source_reads_as_before(tmp_path, monkeypatch):
    db, api = _served(tmp_path, monkeypatch, "M1", drop_source=True)
    players = [p for p in cf.league_players(cf.MATRIX["M1"]) if p.position in ("QB", "RB", "WR", "TE")]
    a, b = players[0].pid, players[1].pid
    r = api.get(f"/api/players/{a}")
    assert r.status_code == 200, r.text[:300]
    assert all("actual_source" not in row for row in r.json()["league_points"])
    r = api.get(f"/api/compare?a={a}&b={b}")
    assert r.status_code == 200, r.text[:300]
    con = duckdb.connect(str(db), read_only=True)
    from app.db.connection import table_columns
    assert "actual_source" not in table_columns(con, "espn_player_weekly_points")       # still the older shape
    con.close()


def test_start_sit_names_its_source_from_the_teams_own_players(league, monkeypatch):
    """Codex P2 on #276: another team's model numbers never make this team's all-ESPN view "Independent model"."""
    from app.analytics.start_sit import load_start_sit_evaluation
    from datetime import datetime, timezone
    con = league("M2")
    players = cf.league_players(cf.MATRIX["M2"])
    other = next(p.pid for p in players if p.team == 2 and p.slot not in cf.RESERVE)
    real_weekly = ps.weekly

    def weekly(con, season, week, source):
        w = real_weekly(con, season, week, "espn")
        return {**w, "source": source, "available": True,
                "origin": {pid: (ps.OURS if pid == other else ps.ESPN) for pid in w["values"]}}
    monkeypatch.setattr(ps, "weekly", weekly)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    mine = load_start_sit_evaluation(con, team_id=1, now=now, source="independent")
    theirs = load_start_sit_evaluation(con, team_id=2, now=now, source="independent")
    assert mine["projection_source"] == "ESPN"
    assert theirs["projection_source"] == "Independent model"
