"""League rules (app/analytics/league_rules.py, GET /api/league-rules): read from the synced `leagues` row only, missing
values are null with a reason, unreadable provider rules are listed as unmapped, and each rule states engine support.

Synthetic fixtures for unusual combinations (TE premium, superflex, several flex types, IDP and unknown slots) run
through both providers' normalisation: the provider adapters (app/leagues/providers) and the league-file writers the
page reads from (app/espn/parse.parse_league, app/sleeper/write.write_sleeper). Neither writer is changed."""
import copy
import json
from datetime import datetime

import duckdb
import pytest
from fastapi.testclient import TestClient

import espn_fixture as ef
from app.analytics.league_rules import (CONTRACT, HANDLED, PARTIAL, UNSUPPORTED, UNVERIFIED, build_league_rules,
                                        sleeper_label)
from app.db.connection import fetch_dicts, init_db
from app.espn.parse import parse_league
from app.leagues.context import LeagueKey, Provider
from app.leagues.providers import espn as espn_provider
from app.leagues.providers import sleeper as sleeper_provider
from app.sleeper.models import parse_league as parse_sleeper_league
from app.sleeper.write import write_sleeper
from test_sleeper_write import FIX, payloads

NOW = datetime(2026, 10, 20, 12, 0, 0)


# ---- helpers ---------------------------------------------------------------------------------------------------------

def espn_row(raw: dict) -> dict:
    """The `leagues` row an ESPN sync writes (write_all upserts exactly parse_league's row)."""
    return parse_league(raw, swid=None, my_team_id=None, synced_at=NOW)["league"]


def unusual_espn() -> dict:
    """Superflex (OP), RB/WR + WR/TE + FLEX, an IDP linebacker, ESPN's unlabelled slot 24 (ER), a TE premium as a TE-slot
    override, a rule with no readable label, a lock rule, rolling waivers and a review period."""
    raw = ef.league_payload()
    s = raw["settings"]
    s["rosterSettings"] = {
        "lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "7": 1, "3": 1, "5": 1, "23": 1, "10": 1, "24": 1,
                             "16": 1, "17": 1, "20": 6, "21": 2},
        "positionLimits": {"1": 4, "2": 0, "99": 3},
        "lineupLocktimeType": "INDIVIDUAL_GAME",
        "isUsingUndroppableList": True,
    }
    s["scoringSettings"]["scoringItems"] = [
        {"statId": 53, "points": 1.0, "pointsOverrides": {"6": 1.5}},
        {"statId": 4, "points": 4.0}, {"statId": 3, "points": 0.04}, {"statId": 25, "points": 6.0},
        {"statId": 8888, "points": 2.0},
        {"statId": 99, "points": 0.0, "pointsOverrides": {"16": 1.0}},
    ]
    s["acquisitionSettings"] = {"isUsingAcquisitionBudget": False, "waiverOrderType": "ROLLING", "waiverHours": 48,
                                "waiverProcessDays": ["WEDNESDAY"], "minimumBid": 1}
    s["tradeSettings"]["reviewPeriodHours"] = 24
    s["scheduleSettings"]["playoffMatchupPeriodLength"] = 2
    return raw


UNUSUAL_SLEEPER_POSITIONS = ["QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "REC_FLEX", "WRRB_FLEX", "DL", "IDP_FLEX",
                             "K", "DEF", "BN", "BN", "BN", "BN"]
UNUSUAL_SLEEPER_SCORING = {"rec": 1.0, "bonus_rec_te": 0.75, "pass_td": 6.0, "pass_yd": 0.04, "rush_yd": 0.1,
                           "rec_yd": 0.1, "fgm": 3.0, "xpm": 1.0, "def_sack": 1.0, "idp_tkl": 1.5,
                           "bonus_pass_yd_300": 2.0, "pass_inc": 0.0, "zz_new_rule": 0.5}


def unusual_sleeper_league() -> dict:
    league = json.loads((FIX / "leagues" / "1180000000000000001" / "league.json").read_text())
    league["roster_positions"] = UNUSUAL_SLEEPER_POSITIONS
    league["scoring_settings"] = UNUSUAL_SLEEPER_SCORING
    league["settings"] = {**league["settings"], "trade_review_days": 2, "waiver_clear_days": 1, "bench_lock": 1}
    return league


@pytest.fixture
def sleeper_con(tmp_path):
    c = duckdb.connect(str(tmp_path / "s.duckdb"))
    init_db(c)
    c.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    yield c
    c.close()


def sleeper_row(con, league: dict | None = None) -> dict:
    write_sleeper(con, payloads(**({"league": league} if league else {})))
    return fetch_dicts(con, "SELECT * FROM leagues")[0]


def missing_values_have_reasons(node, path="$"):
    """Every {value, display, reason} fact: a None value carries a reason; a present one does not."""
    if isinstance(node, dict):
        if {"value", "display", "reason"} <= set(node):
            assert (node["value"] is None) == bool(node["reason"]), path
        for k, v in node.items():
            missing_values_have_reasons(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            missing_values_have_reasons(v, f"{path}[{i}]")


def by_rule(out: dict) -> dict:
    return {r["rule"]: r for r in out["engine_support"]}


def slots(out: dict) -> dict:
    return {s["code"]: s for s in out["roster"]["slots"]}


# ---- provider normalisation of the unusual fixtures (the adapters are unchanged) --------------------------------------

def test_espn_adapter_normalises_superflex_multi_flex_and_a_te_slot_premium():
    key = LeagueKey(Provider.ESPN, str(ef.LEAGUE_ID), ef.SEASON)
    st = espn_provider.settings_from(key, unusual_espn(), NOW)
    assert st.lineup.superflex_slots == 1 and st.lineup.flex_slots == 3          # FLEX, RB/WR, WR/TE
    assert {s.code for s in st.lineup.starters()} >= {"SUPERFLEX", "WRRB_FLEX", "REC_FLEX", "FLEX", "LB", "ER"}
    assert st.scoring.points_per_reception == 1.0
    assert "53@slot6" in st.scoring.unmapped and "8888" in st.scoring.unmapped       # premium kept, never dropped


def test_sleeper_adapter_normalises_superflex_multi_flex_idp_and_a_te_bonus():
    key = LeagueKey(Provider.SLEEPER, "1180000000000000001", 2026)
    st = sleeper_provider.settings_from(key, parse_sleeper_league(unusual_sleeper_league()))
    assert st.lineup.superflex_slots == 1 and st.lineup.flex_slots == 3          # FLEX, REC_FLEX, WRRB_FLEX
    assert st.scoring.canonical["rec_te_bonus"] == 0.75
    assert {"idp_tkl", "zz_new_rule", "bonus_pass_yd_300"} <= set(st.scoring.unmapped)


# ---- the rules page from the league file -----------------------------------------------------------------------------

def test_the_plain_espn_fixture_league():
    out = build_league_rules(espn_row(ef.league_payload()))
    assert out["contract"] == CONTRACT
    assert out["source"]["provider"] == "espn" and out["source"]["captured_at"] == NOW.isoformat()
    assert {c: s["count"] for c, s in slots(out).items()} == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1,
                                                               "D/ST": 1, "BE": 3, "IR": 1}
    assert slots(out)["FLEX"]["eligible"] == ["RB", "WR", "TE"]
    assert out["roster"]["starters"]["value"] == 9 and not out["roster"]["superflex"]
    assert out["scoring"]["format"]["value"] == "ppr" and out["scoring"]["te_premium"]["value"] == 0.0
    assert out["waivers"]["type"]["value"] == "faab" and out["waivers"]["faab_budget"]["value"] == 100
    # not in the synced settings: null with a reason, never 0 or a default
    assert out["lineup_lock"]["value"] is None and out["lineup_lock"]["reason"]
    assert out["waivers"]["period"]["value"] is None and out["trades"]["review"]["value"] is None
    passing_td = next(h for h in out["scoring"]["highlights"] if h["label"] == "Passing TD")
    assert passing_td["value"] is None and passing_td["reason"]
    assert by_rule(out)["Lineup lock"]["status"] == UNVERIFIED
    missing_values_have_reasons(out)


def test_unusual_espn_rules_and_engine_support():
    out = build_league_rules(espn_row(unusual_espn()))
    sl = slots(out)
    assert sl["OP"]["eligible"] == ["QB", "RB", "WR", "TE"] and sl["OP"]["engine"]["status"] == HANDLED
    assert sl["RB/WR"]["eligible"] == ["RB", "WR"] and sl["WR/TE"]["eligible"] == ["WR", "TE"]
    assert sl["LB"]["eligible"] == ["LB"] and sl["LB"]["engine"]["status"] == UNSUPPORTED
    assert sl["ER"]["kind"] == "unknown" and sl["ER"]["eligible"] is None and sl["ER"]["eligible_reason"]
    assert out["roster"]["superflex"] and out["roster"]["flex_types"] == ["RB/WR", "WR/TE", "FLEX", "OP"]
    assert out["roster"]["position_limits"]["value"] == {"QB": 4}
    sc = out["scoring"]
    assert sc["te_premium"]["value"] == 0.5 and sc["te_premium_scope"] == "slot"
    rec = next(r for r in sc["rules"] if r["key"] == "53")
    assert rec["by_slot"] == [{"slot": "TE", "points": 1.5}]
    assert next(r for r in sc["rules"] if r["key"] == "99")["by_slot"] == [{"slot": "D/ST", "points": 1.0}]
    assert out["lineup_lock"]["value"] == "individual_game"
    assert out["waivers"]["type"]["value"] == "rolling" and out["waivers"]["faab_budget"]["value"] is None
    assert out["waivers"]["period"]["value"] == 48 and out["waivers"]["process_days"]["value"] == ["Wednesday"]
    assert out["trades"]["review"]["value"] == 24 and out["playoffs"]["round_length"]["value"] == 2
    unmapped = {(u["area"], u["key"]) for u in out["unmapped"]}
    assert {("Roster slot", "ER"), ("Scoring rule", "stat 8888"), ("Roster limit", "position 99"),
            ("Setting", "acquisitionSettings.minimumBid")} <= unmapped
    rules = by_rule(out)
    assert rules["Superflex (OP) slot"]["status"] == PARTIAL
    assert rules["Several flex slot types"]["status"] == HANDLED
    assert rules["Starting slots no engine fills"]["status"] == UNSUPPORTED
    assert "LB" in rules["Starting slots no engine fills"]["detail"] and "ER" in rules["Starting slots no engine fills"]["detail"]
    assert rules["Ours projections (QB, RB, WR, TE)"]["status"] == UNSUPPORTED   # a TE-slot override is not modelled
    assert rules["TE premium"]["status"] == PARTIAL and "Ours does not" in rules["TE premium"]["detail"]
    assert rules["Lineup lock"]["status"] == HANDLED
    assert rules["Undroppable list"]["status"] == UNSUPPORTED
    assert rules["Per-position roster limits"]["status"] == HANDLED
    missing_values_have_reasons(out)


def test_an_unknown_espn_lock_rule_is_shown_verbatim_and_not_claimed_as_applied():
    raw = unusual_espn()
    raw["settings"]["rosterSettings"]["lineupLocktimeType"] = "SOMETHING_NEW"
    out = build_league_rules(espn_row(raw))
    assert out["lineup_lock"]["value"] == "SOMETHING_NEW"
    assert ("Lineup lock", "rosterSettings.lineupLocktimeType") in {(u["area"], u["key"]) for u in out["unmapped"]}
    assert by_rule(out)["Lineup lock"]["status"] == UNSUPPORTED


def test_unusual_sleeper_rules_from_the_league_file(sleeper_con):
    row = sleeper_row(sleeper_con, unusual_sleeper_league())
    out = build_league_rules(row)
    text = json.dumps(out, default=str)
    assert "1180000000000000001" not in text and "ESPN" not in text      # no league id; Sleeper's own wording
    assert out["source"]["provider"] == "sleeper"
    sl = slots(out)
    assert sl["OP"]["eligible"] == ["QB", "RB", "WR", "TE"]
    assert sl["WR/TE"]["eligible"] == ["WR", "TE"] and sl["RB/WR"]["eligible"] == ["RB", "WR"]
    assert sl["DL"]["eligible"] == ["DL"] and sl["DP"]["eligible"] == ["DL", "LB", "DB"]
    assert sl["DL"]["engine"]["status"] == UNSUPPORTED and sl["TAXI"]["engine"]["status"] == UNSUPPORTED
    assert sl["BE"]["count"] == 4 and sl["IR"]["count"] == 1 and sl["TAXI"]["count"] == 1
    sc = out["scoring"]
    assert sc["format"]["value"] == "ppr" and sc["te_premium"]["value"] == 0.75 and sc["te_premium_scope"] == "position"
    assert sc["zero_rules"] == 1                                              # pass_inc 0: counted, not listed
    labels = {r["key"]: r["label"] for r in sc["rules"]}
    assert labels["bonus_pass_yd_300"] == "300+ passing yards in a game" and labels["fgm"] == "Field goal made"
    unmapped = {(u["area"], u["key"]) for u in out["unmapped"]}
    assert {("Scoring rule", "idp_tkl"), ("Scoring rule", "zz_new_rule"), ("Setting", "bench_lock")} <= unmapped
    assert out["league"]["league_type"]["value"] == "Dynasty"
    assert out["waivers"]["type"]["value"] == "faab" and out["waivers"]["period"]["value"] == 1
    assert out["trades"]["deadline"]["display"] == "Week 11" and out["trades"]["review"]["value"] == 2
    assert out["lineup_lock"]["value"] is None and out["lineup_lock"]["reason"]
    rules = by_rule(out)
    assert rules["Superflex (OP) slot"]["status"] == PARTIAL
    assert rules["Several flex slot types"]["status"] == HANDLED
    assert rules["Taxi squad"]["status"] == UNSUPPORTED
    assert rules["Starting slots no engine fills"]["status"] == UNSUPPORTED
    ours = rules["Projections (Ours, QB, RB, WR, TE)"]
    assert ours["status"] == UNSUPPORTED and "zz_new_rule" in ours["detail"]
    assert rules["Lineup lock"]["status"] == UNVERIFIED
    missing_values_have_reasons(out)


def test_the_plain_sleeper_fixture_applies_the_te_bonus_in_ours(sleeper_con):
    league = json.loads((FIX / "leagues" / "1180000000000000001" / "league.json").read_text())
    league["scoring_settings"] = {**league["scoring_settings"], "bonus_rec_te": 0.5}
    out = build_league_rules(sleeper_row(sleeper_con, league))
    assert out["scoring"]["format"]["value"] == "half_ppr"
    ours = by_rule(out)["Projections (Ours, QB, RB, WR, TE)"]
    assert ours["status"] == HANDLED and "TE reception bonus" in ours["detail"]
    assert by_rule(out)["Points per reception"]["status"] == PARTIAL


def test_the_league_files_are_unchanged_by_the_unusual_fixtures(sleeper_con):
    """The writers still store the same shapes the engines read (legacy output untouched)."""
    row = sleeper_row(sleeper_con, unusual_sleeper_league())
    assert json.loads(row["roster_slots"]) == {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "FLEX": 1, "OP": 1, "WR/TE": 1,
                                                "RB/WR": 1, "DL": 1, "DP": 1, "K": 1, "D/ST": 1, "BE": 4, "IR": 1,
                                                "TAXI": 1}
    assert espn_row(unusual_espn())["roster_slots"]["OP"] == 1


def test_missing_scoring_and_slots_are_reasons_not_zeros():
    out = build_league_rules({"name": "Bare", "season": 2026, "roster_slots": None, "scoring_items": None,
                              "settings_raw": None, "synced_at": None})
    assert out["roster"]["slots"] == [] and out["roster"]["starters"]["value"] is None
    assert out["scoring"]["format"]["value"] is None and out["scoring"]["te_premium"]["value"] is None
    assert out["playoffs"]["teams"]["value"] is None and out["league"]["team_count"]["value"] is None
    missing_values_have_reasons(out)


def test_sleeper_rule_labels():
    assert sleeper_label("fgm_50p") == ("Kicking", "Field goal made, 50+ yards")
    assert sleeper_label("pts_allow_1_6") == ("Team defense", "Points allowed 1-6")
    assert sleeper_label("yds_allow_550p") == ("Team defense", "Yards allowed 550+")
    assert sleeper_label("idp_tkl") is None


# ---- the endpoint ------------------------------------------------------------------------------------------------------

def _client(monkeypatch, path):
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_endpoint_reads_the_synced_row(tmp_path, monkeypatch):
    db = tmp_path / "f.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    row = espn_row(unusual_espn())
    cols = [r[0] for r in con.execute("DESCRIBE leagues").fetchall()]
    con.execute(f"INSERT INTO leagues ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [json.dumps(row[c], default=str) if isinstance(row.get(c), (dict, list)) else row.get(c) for c in cols])
    con.close()
    r = _client(monkeypatch, db).get("/api/league-rules")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract"] == CONTRACT and body["roster"]["superflex"] is True
    assert "league_id" not in r.text and f"\"{ef.LEAGUE_ID}\"" not in r.text and f": {ef.LEAGUE_ID}," not in r.text


def test_the_endpoint_answers_503_before_any_sync(tmp_path, monkeypatch):
    db = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    con.close()
    r = _client(monkeypatch, db).get("/api/league-rules")
    assert r.status_code == 503 and "synced" in r.json()["detail"]


def test_no_sleeper_wording_leaks_into_an_espn_league():
    out = build_league_rules(espn_row(copy.deepcopy(unusual_espn())))
    assert "Sleeper" not in json.dumps(out, default=str)


# ---- the /v2 copy: authorized per league (app/api/league_scope.py) ------------------------------------------------------

from league_fixtures import publish_local                                    # noqa: E402
from test_league_scope import client, e2e, served                            # noqa: E402,F401  (fixtures)
from test_leagues_api import bearer, env                                     # noqa: E402,F401  (fixtures)


def test_v2_league_rules_are_404_for_unknown_and_inaccessible_leagues(pg, served, client, e2e, tmp_path):  # noqa: F811
    from cp_seed import U1, U3, seed_membership, seed_user
    private = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U3)
        seed_membership(con, U1, private.league_id, "viewer")
        con.commit()
    for lg, headers in ((private.league_id, {}), (private.league_id, bearer(U3)), ("lg_" + "z" * 24, {}),
                        ("lg_" + "z" * 24, bearer(U1))):
        r = client.get(f"/v2/leagues/{lg}/api/league-rules", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (lg, headers)
    r = client.get(f"/v2/leagues/{private.league_id}/api/league-rules", headers=bearer(U1))
    assert r.status_code == 200 and r.json()["contract"] == CONTRACT
    assert r.headers["cache-control"] == "private, no-store"
    assert private.league_id not in r.text
