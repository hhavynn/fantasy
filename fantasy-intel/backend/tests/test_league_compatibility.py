"""League-format compatibility certification (issue #253, phase 2): synthetic matrix leagues (tests/compat_fixtures.py)
run through the real import (ESPN fetch + write, Sleeper write), the league rules, the consuming engines and the
league-compatibility/v1 diagnostic (app/analytics/league_compatibility.py, GET /api/league-compatibility).

FX-17 is six teams with 2 QB, 3 RB, 4 WR, 2 TE, 1 OP, 3 FLEX, K, D/ST, 10 bench and 3 IR. Its slot counts must come out of
normalization unchanged, every lineup must start 17 distinct players with three interchangeable FLEX slots and no bench or
IR player, and every tool must report each of the four dimensions with a reason. Invented data only."""
import json
from datetime import datetime, timezone

import duckdb
import pytest
from fastapi.testclient import TestClient

import compat_fixtures as cf
from app.analytics.league_compatibility import CONTRACT, DIMENSIONS, TOOLS, active_dual_position_count, build_compatibility, load_facts
from app.analytics.league_rules import ENGINE_CODES, STATUSES, UNSUPPORTED, build_league_rules
from app.analytics.legacy_league import latest_league
from app.analytics.lineup import FLEX_ELIGIBILITY, assign_lineup
from app.analytics.start_sit import NO_PROJECTIONS, load_start_sit_evaluation
from app.analytics.team_lineups import project_team
from app.analytics.trade_data import load_league_state
from app.db.connection import init_db

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)      # before any synthetic kickoff


@pytest.fixture
def league(tmp_path):
    opened = []

    def make(row, missing=()):
        con = duckdb.connect(str(tmp_path / f"{len(opened)}.duckdb"))
        init_db(con)
        cf.import_row(con, cf.MATRIX[row] if isinstance(row, str) else row, missing)
        opened.append(con)
        return con
    yield make
    for c in opened:
        c.close()


def diagnostic(con) -> dict:
    lg = latest_league(con)
    return build_compatibility(lg, load_facts(con, lg))


def tool(out: dict, code: str) -> dict:
    return next(t for t in out["tools"] if t["tool"] == code)


def codes(entry: dict) -> set[str]:
    return {e["code"] for e in entry["evidence"] if e["status"] != "handled"}


# ---- import and normalization ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("row", ["M2", "M3"])
def test_fx17_slot_counts_survive_normalization_on_both_providers(league, row):
    con = league(row)
    lg = latest_league(con)
    assert json.loads(lg["roster_slots"]) == cf.FX17
    roster = build_league_rules(lg)["roster"]
    assert (roster["starters"]["value"], roster["bench"], roster["injured_reserve"]) == (17, 10, 3)
    assert roster["superflex"] and roster["flex_types"] == ["FLEX", "OP"]
    assert all(s["engine"]["status"] == "handled" for s in roster["slots"])


@pytest.mark.parametrize("row", list(cf.MATRIX))
def test_every_matrix_row_keeps_its_slot_counts(league, row):
    assert json.loads(latest_league(league(row))["roster_slots"]) == cf.MATRIX[row].slots


# ---- the engines on FX-17 -----------------------------------------------------------------------------------------------

def test_fx17_start_sit_starts_17_distinct_players_with_three_interchangeable_flex(league):
    con = league("M2")
    ir = {r[0] for r in con.execute("SELECT espn_player_id FROM lineup_snapshots WHERE lineup_slot_id = 21 "
                                    "AND team_id = 1 AND scoring_period = 3").fetchall()}
    assert len(ir) == 3
    out = load_start_sit_evaluation(con, team_id=1, now=NOW)
    rec = out["recommended_lineup"]
    assert len(rec) == 17 and len({r["slot_id"] for r in rec}) == 17
    starters = [r["player_id"] for r in rec]
    assert None not in starters and len(set(starters)) == 17 and not set(starters) & ir
    assert sorted(r["slot_id"] for r in rec if r["slot"] == "FLEX") == ["FLEX_0", "FLEX_1", "FLEX_2"]
    assert all(r["position"] in FLEX_ELIGIBILITY[r["slot"]] for r in rec if r["is_flex"])
    s = out["summary"]
    assert s["projection_complete"] is True and out["projection_source"] == "ESPN"
    assert s["recommended_projected_total"] == round(sum(r["projected_points"] for r in rec), 2)


def test_fx17_weekly_lineups_never_start_bench_ir_or_a_player_twice(league):
    con = league("M2")
    state = load_league_state(con, now=NOW)
    assert len(state.slots) == 17 and state.active_capacity == 27
    for team, players in state.rosters.items():
        ir = {p.espn_player_id for p in players if p.on_ir}
        assert len(players) == 30 and len(ir) == 3
        proj = project_team(players, state.slots, state.window(), state.projections)
        for week in proj["weeks"]:
            ids = [a["player_id"] for a in week["assignment"]]
            assert len(ids) == 17 and None not in ids and len(set(ids)) == 17 and not set(ids) & ir
            assert sum(1 for a in week["assignment"] if a["slot"] == "FLEX") == 3
            assert week["projection_complete"] and week["projected_lineup_points"] == round(
                sum(a["points"] for a in week["assignment"]), 2)


def test_tied_and_sparse_superflex_lineups_are_deterministic_and_unique(league):
    """M4: every player projects the same and there are no free agents: ties break by id, whatever the input order."""
    con = league("M4")
    state = load_league_state(con, now=NOW)
    week = state.window().weeks[0]
    for players in state.rosters.values():
        pool = [{"espn_player_id": p.espn_player_id, "position": p.position, "value": state.projections[p.espn_player_id][week]}
                for p in players if not p.on_ir]
        a = assign_lineup(pool, state.slots)
        b = assign_lineup(list(reversed(pool)), state.slots)
        assert a == b
        ids = [x["player_id"] for x in a]
        assert None not in ids and len(set(ids)) == len(state.slots) == 11


# ---- G1: an empty recommended slot is never a measured total ------------------------------------------------------------

def every_player(row_id: str) -> list[int]:
    return [p.pid for p in cf.league_players(cf.MATRIX[row_id])]


@pytest.mark.parametrize("row", ["M1", "M2", "M3"])
@pytest.mark.parametrize("week", [cf.CURRENT, cf.CURRENT + 1])
def test_a_league_without_projections_gets_no_total_and_an_honest_source_label(league, row, week):
    """An ordinary league (M1) and FX-17 on ESPN (M2) with every projection missing, and FX-17 on Sleeper (M3: no
    provider projections and no stored Ours run). Nothing can be recommended: every total is unknown (None), never a
    measured 0.0 or a complete total, and the source is not named after projections that do not exist."""
    con = league(row, missing=every_player(row) if cf.MATRIX[row].provider == "espn" else ())
    out = load_start_sit_evaluation(con, team_id=1, week=week, now=NOW)
    s = out["summary"]
    assert all(r["player_id"] is None and r["projected_points"] is None for r in out["recommended_lineup"])
    assert s["recommended_projected_total"] is None and s["unconstrained_total"] is None
    assert s["actual_projected_total"] is None and s["projected_gain"] is None
    assert s["projection_complete"] is False and s["missing_projections"]
    assert out["projection_source"] == NO_PROJECTIONS


@pytest.mark.parametrize("row", ["M1", "M2"])
def test_a_starter_slot_only_an_unprojected_player_can_fill_makes_the_recommendation_incomplete(league, row):
    """Every kicker unprojected, everyone else projected: the K slot stays empty because the number is missing, not
    because no kicker is rostered, so the recommended total is unknown (its known part is still reported)."""
    kickers = [p.pid for p in cf.league_players(cf.MATRIX[row]) if p.position == "K"]
    con = league(row, missing=kickers)
    out = load_start_sit_evaluation(con, team_id=1, week=cf.CURRENT + 1, now=NOW)
    s = out["summary"]
    k = next(r for r in out["recommended_lineup"] if r["slot"] == "K")
    assert k["player_id"] is None and k["projected_points"] is None
    filled = [r["projected_points"] for r in out["recommended_lineup"] if r["player_id"] is not None]
    assert len(filled) == len(out["recommended_lineup"]) - 1
    assert s["recommended_projected_total"] is None and s["unconstrained_total"] is None
    assert s["projection_complete"] is False
    assert s["known_recommended_total"] == round(sum(filled), 2) > 0
    assert set(kickers) & {m["player_id"] for m in s["missing_projections"]}
    assert out["projection_source"] == "ESPN"


def test_an_unavailable_source_label_survives_the_sleeper_wording(league):
    """S8: a Sleeper league's responses have ESPN wording rewritten (a bare "ESPN" source label becomes "Ours"); a week
    without any projection must stay "Unavailable", never "Ours" over missing numbers."""
    from app.api.provider_copy import rewrite

    out = rewrite(load_start_sit_evaluation(league("M3"), team_id=1, now=NOW))
    assert out["projection_source"] == NO_PROJECTIONS
    assert out["projection_source_detail"]["available"] is False


def test_a_verified_run_whose_rows_all_abstain_is_no_projections_not_ours(league, monkeypatch):
    """A verified independent run exists but every row abstains and the provider has no fallback number: the source is
    "Unavailable" (Start/Sit) and "none" with an unsupported missing-projections finding (diagnostic), never "Ours"."""
    from app.analytics import projection_source as ps
    from app.analytics.start_sit import projection_source_label

    assert projection_source_label("independent", True, {1: None, 2: None}) == NO_PROJECTIONS
    assert projection_source_label("independent", True, {1: None, 2: 3.5}) == "Independent model"
    con = league("M3")
    real_weekly = ps.weekly
    # the run is verified and shown, but none of its rows is a projection (and Sleeper has no fallback number)
    monkeypatch.setattr(ps, "weekly", lambda con, season, week, source: {
        **real_weekly(con, season, week, "espn"), "source": source, "available": True})
    monkeypatch.setattr(ps, "select_run", lambda *a, **k: {"run": {"run_id": "abstaining"}, "rows": [], "skipped": [],
                                                           "fallback": False, "unavailable_reason": None})
    out = load_start_sit_evaluation(con, team_id=1, now=NOW)
    assert out["projection_source"] == NO_PROJECTIONS and out["summary"]["recommended_projected_total"] is None

    lg = latest_league(con)
    facts = load_facts(con, lg)
    assert facts["independent"] == {"available": True} and facts["projection_source"] == "none"
    sc = build_compatibility(lg, facts)["dimensions"]["scoring"]
    assert sc["status"] == "unsupported" and "every one of its rows abstains" in sc["reason"]


def test_a_stored_run_whose_rows_all_abstain_reads_as_no_projections_end_to_end(league):
    """The same case through the real selection: a sealed independent run under M3's scoring whose rows are all
    `insufficient_data` (written with independent_projection_store.write_run, read by projection_source.weekly,
    select_run and league_projections / forward_ours._select_batch, nothing patched)."""
    from app.analytics import independent_projection_store as ips
    from app.analytics import projection_source as ps
    from app.analytics.league_scoring import resolve_skill_scoring

    con = league("M3")
    lg = latest_league(con)
    fp = resolve_skill_scoring(json.loads(lg["scoring_items"])).fingerprint
    created = datetime(cf.SEASON, 9, 1, 12, 0)
    rows = [{"season": cf.SEASON, "week": cf.CURRENT, "espn_player_id": p.pid, "gsis_id": f"00-FX{abs(p.pid):05d}",
             "position": p.position, "nfl_team": None, "opponent": None, "is_home": None, "projected_points": None,
             "interval_low": None, "interval_high": None, "model": "fx", "coverage_status": "insufficient_data",
             "games_used": 0, "latest_usage_season": None, "latest_usage_week": None, "missing_inputs": ["usage"],
             "flags": [], "explanation": [], "p_play": None, "expected_points": None, "participation": None}
            for p in cf.league_players(cf.MATRIX["M3"]) if p.position in ("QB", "RB", "WR", "TE")]
    ips.write_run(con, {"season": cf.SEASON, "week": cf.CURRENT, "forecast_horizon": 0, "projected_from_week": cf.CURRENT,
                        "data_through_season": cf.SEASON, "data_through_week": cf.CURRENT - 1, "created_at": created,
                        "artifact_id": "fx-abstaining", "artifact_created_at": created.isoformat(),
                        "run_id": "fx-abstaining", "model_version": ps.MODEL_VERSION, "scoring_fingerprint": fp,
                        "model_status": {}, "evaluation": {}, "participation_version": None,
                        "participation_status": None, "participation_evaluation": None, "nfl_synced_at": created}, rows)

    assert ps.select_run(con, cf.SEASON, cf.CURRENT)["run"]["run_id"] == "fx-abstaining"
    shown = ps.weekly(con, cf.SEASON, cf.CURRENT, "independent")
    assert shown["available"] is True and not any(v is not None for v in shown["values"].values())
    assert not ps.league_projections(con, cf.SEASON, [cf.CURRENT], "independent")

    out = load_start_sit_evaluation(con, team_id=1, now=NOW)
    assert out["projection_source"] == NO_PROJECTIONS and out["summary"]["recommended_projected_total"] is None
    facts = load_facts(con, lg)
    assert facts["independent"] == {"available": True} and facts["projection_source"] == "none"
    sc = build_compatibility(lg, facts)["dimensions"]["scoring"]
    assert sc["status"] == "unsupported" and "every one of its rows abstains" in sc["reason"]


# ---- S1: Ours re-scored from the reference scoring is partial ------------------------------------------------------------

def _run(con, run_id: str, fingerprint: str, evaluation: dict) -> None:
    con.execute("INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint, "
                "evaluation) VALUES (?, ?, ?, 'ifp-2.2.0', ?, ?)",
                [run_id, cf.SEASON, cf.CURRENT, fingerprint, json.dumps(evaluation)])


def test_ours_re_scored_from_the_reference_scoring_is_partial_not_league_scored(league):
    from app.analytics.league_rules import RESCORED_DETAIL, skill_runs_rescored
    from app.analytics.league_scoring import resolve_skill_scoring

    con = league("M3")
    lg = latest_league(con)
    fp = resolve_skill_scoring(json.loads(lg["scoring_items"])).fingerprint
    ours = lambda **kw: {r["code"]: r for r in build_league_rules(lg, **kw)["engine_support"]}["skill_projections"]  # noqa: E731

    assert skill_runs_rescored(con, lg) is None and ours()["status"] == "handled"       # no run: nothing to qualify
    _run(con, "native", fp, {"mae": 1.0})
    _run(con, "other-scoring", "0" * 16, {"rescored_from": {"run_id": "x"}})           # another league's scoring
    assert skill_runs_rescored(con, lg) is False
    _run(con, "derived", fp, {"rescored_from": {"run_id": "ref-1", "scoring_fingerprint": "f" * 16}})
    assert skill_runs_rescored(con, lg) is True
    row = ours(ours_rescored=True)
    assert row["status"] == "partial" and row["detail"].startswith(RESCORED_DETAIL)
    assert ours(ours_rescored=False)["status"] == "handled"

    out = diagnostic(con)
    ev = next(e for e in tool(out, "rankings")["scoring"]["evidence"] if e["code"] == "skill_projections")
    assert ev == {"code": "skill_projections", "status": "partial", "value": {"follow_up": "#254"}}


# ---- the diagnostic ----------------------------------------------------------------------------------------------------

def well_formed(out: dict) -> None:
    assert out["contract"] == CONTRACT
    entries = list(out["dimensions"].values()) + [t[d] for t in out["tools"] for d in DIMENSIONS]
    for e in entries:
        assert e["status"] in STATUSES and e["reason"]
        assert set(e) == {"status", "reason", "missing_input", "provider_boundary", "action", "evidence"}
        if e["status"] != "handled":
            assert any(x["status"] == e["status"] for x in e["evidence"])
    assert [t["tool"] for t in out["tools"]] == list(TOOLS)
    for t in out["tools"]:
        assert set(t["refs"]) <= set(ENGINE_CODES)
    text = json.dumps(out)
    assert "league_id" not in text and "FX " not in text          # no ids, no player names


@pytest.mark.parametrize("row", list(cf.MATRIX))
def test_every_row_answers_every_tool_on_four_dimensions(league, row):
    well_formed(diagnostic(league(row)))


def test_fx17_espn_is_handled_except_what_waivers_do_not_model(league):
    out = diagnostic(league("M2"))
    assert {d: e["status"] for d, e in out["dimensions"].items()} == {"import": "handled", "roster": "handled",
                                                                       "scoring": "handled"}
    for t in out["tools"]:
        if t["tool"] == "waivers":
            assert (t["scoring"]["status"], t["feature"]["status"]) == ("partial", "partial")
            assert codes(t["scoring"]) == {"player_value_basis"} and codes(t["feature"]) == {"missing_value"}
        else:
            assert {d: t[d]["status"] for d in DIMENSIONS} == dict.fromkeys(DIMENSIONS, "handled"), t["tool"]
    assert tool(out, "start_sit")["projection_source"] == "provider"
    assert tool(out, "bye_planner")["projection_source"] is None


def test_roster_support_is_not_scoring_fidelity(league):
    """M2b: the same FX-17 roster with an unmapped stat and a TE slot override. Fantasy Intel's own skill model abstains
    (league rules say so), yet the tools read ESPN's projections in the league's scoring, so the roster stays handled and
    only the rules page's model rows change."""
    con = league("M2b")
    rules = {r["code"]: r for r in build_league_rules(latest_league(con))["engine_support"]}
    assert rules["skill_projections"]["status"] == UNSUPPORTED and rules["te_premium"]["status"] == "partial"
    out = diagnostic(con)
    assert out["dimensions"]["roster"]["status"] == "handled"
    assert tool(out, "start_sit")["scoring"]["status"] == "handled"
    basis = next(e for e in tool(out, "waivers")["scoring"]["evidence"] if e["code"] == "player_value_basis")
    assert basis["value"] == ["te_premium"]                     # waiver values ignore the premium (full PPR xFP)


def test_fx17_sleeper_names_the_projection_boundary_and_the_roster_rules_it_cannot_read(league):
    out = diagnostic(league("M3"))
    assert out["dimensions"]["roster"]["status"] == "handled"
    sc = out["dimensions"]["scoring"]
    assert sc["status"] == "unsupported" and "independent_run" in codes(sc)      # no number at all, as on ESPN
    run = next(e for e in sc["evidence"] if e["code"] == "independent_run")
    assert run["value"] == ["skill_projections", "kicker_projections", "dst_projections"]
    assert "publishes no projections" in sc["provider_boundary"]
    assert tool(out, "start_sit")["projection_source"] == "none"
    assert tool(out, "start_sit")["feature"]["status"] == "unverified"           # totals known-only (G10)
    assert "roster_restrictions" in codes(tool(out, "waivers")["roster"])       # G6
    assert tool(out, "waivers")["roster"]["status"] == "unverified"
    assert codes(tool(out, "add_drop")["roster"]) == {"ir_eligibility"}          # G7: no reserve_allow_* synced
    assert tool(out, "bye_planner")["scoring"]["status"] == "handled"
    assert "ESPN" not in json.dumps(out)


def test_an_unprojected_rostered_player_makes_lineup_totals_known_only(league):
    kicker = next(p.pid for p in cf.league_players(cf.MATRIX["M2"]) if p.position == "K" and p.team == 1)
    out = diagnostic(league("M2", missing=[kicker]))
    for code in ("start_sit", "team_value", "trades", "playoff_race"):
        t = tool(out, code)
        assert t["scoring"]["status"] == "partial" and "projection_coverage" in codes(t["scoring"])
        assert t["feature"]["status"] == "unverified" and "totals_known_only" in codes(t["feature"])
    assert tool(out, "rankings")["feature"]["status"] == "handled"            # rankings read QB-TE only


def test_a_sparse_pool_flags_the_trade_replacement_level(league):
    out = diagnostic(league("M4"))                              # no free agent at all (G8)
    assert codes(tool(out, "trade_discovery")["feature"]) == {"replacement_level"}
    assert tool(out, "trades")["feature"]["status"] == "handled"


def test_idp_slots_are_partial_for_lineups_and_unverified_legality_only_when_players_are_missing(league):
    espn = diagnostic(league("M8"))
    assert espn["dimensions"]["roster"]["status"] == "partial"
    ss = tool(espn, "start_sit")["roster"]
    assert ss["status"] == "partial" and ss["evidence"][0]["value"] == ["DL", "LB"]
    assert ss["reason"] == ("DL, LB: no engine fills these slots, so your starters there are kept as they are, and "
                            "the submitted and recommended totals cover the other slots only and are marked partial.")
    for code in ("start_sit", "what_if"):                                          # G2 fixed: the gain is exact
        ft = tool(espn, code)["feature"]
        gain = next(f for f in ft["evidence"] if f["code"] == "gain_unfilled_slots")
        assert gain["status"] == "handled" and gain["value"] == 8                  # 2 IDP starters x 4 teams
    assert "gain_unfilled_slots" not in codes(tool(espn, "trades")["feature"])
    assert codes(tool(espn, "playoff_race")["roster"]) == {"unfilled_slots"}       # G3: totals partial
    race = tool(espn, "playoff_race")["roster"]["reason"]
    assert "are marked partial; a lineup change is still measured exactly" in race
    assert tool(espn, "bye_planner")["roster"]["status"] == "partial"
    assert tool(espn, "kdst_streaming")["roster"]["status"] == "handled"
    for code in ("trades", "add_drop", "team_value", "trade_discovery"):    # G4: ESPN stores IDP players: exact count
        r = tool(espn, code)["roster"]
        assert r["status"] == "partial" and codes(r) == {"unfilled_slots"}, code
        legal = next(e for e in r["evidence"] if e["code"] == "roster_legality")
        assert legal["status"] == "handled" and legal["value"] == ["DL", "LB"]

    sleeper = diagnostic(league("M8s"))
    assert sleeper["dimensions"]["import"]["status"] == "partial"                  # G4: IDP players not stored
    for code in ("trades", "trade_discovery", "add_drop", "team_value", "waivers"):
        r = tool(sleeper, code)["roster"]
        assert r["status"] == "unverified" and "players_missing" in codes(r), code
        assert "roster legality is unverified" in r["reason"]
    assert tool(sleeper, "start_sit")["roster"]["status"] == "partial"


def test_a_team_without_a_synced_roster_makes_capacity_tools_unverified(league):
    """N3: a standard league (M1) whose team 1 has no lineup rows this week: its roster cannot be counted, so every
    tool that checks roster capacity says legality is unverified; tools that do not check capacity are unaffected."""
    con = league("M1")
    con.execute("DELETE FROM lineup_snapshots WHERE team_id = 1 AND scoring_period = ?", [cf.CURRENT])
    out = diagnostic(con)
    for code, (_, _, _, capacity) in TOOLS.items():
        r = tool(out, code)["roster"]
        if capacity:
            assert r["status"] == "unverified" and "roster_legality" in codes(r), code
            assert "1 team of 4 have no synced roster" in r["reason"], code
        else:
            assert "roster_legality" not in {e["code"] for e in r["evidence"]}, code


def test_missing_players_and_teams_without_rosters_are_both_named(league):
    con = league("M8s")
    con.execute("DELETE FROM lineup_snapshots WHERE team_id = 1 AND scoring_period = ?", [cf.CURRENT])
    r = tool(diagnostic(con), "trades")["roster"]
    assert {"players_missing", "roster_legality"} <= codes(r)
    assert "not in the player data" in r["reason"] and "have no synced roster" in r["reason"]


def test_punter_and_head_coach_slots_are_reported_unsupported(league):
    con = league("M8p")
    out, rules = diagnostic(con), build_league_rules(latest_league(con))
    assert {s["code"]: s["engine"]["status"] for s in rules["roster"]["slots"]}["P"] == UNSUPPORTED
    assert {s["code"]: s["engine"]["status"] for s in rules["roster"]["slots"]}["HC"] == UNSUPPORTED
    ss = tool(out, "start_sit")["roster"]
    assert ss["status"] == "partial" and ss["evidence"][0]["value"] == ["P", "HC"]
    assert tool(out, "trades")["roster"]["status"] == "partial"                    # exact count, slots unvalued
    assert out["dimensions"]["import"]["status"] == "handled"                      # ESPN stores P and HC players


def test_two_kickers_and_two_defenses_are_streamed_as_one_slot_each_and_reported_partial(league):
    """G9 (M9: 2 K, 2 D/ST). The planner streams the slot the weakest incumbent holds and says so: the position carries
    `slot_coverage` partial (code streaming_slots), never a plan read as complete, and the diagnostic cites it."""
    from app.analytics.kdst_streaming import build_kdst_streaming

    con = league("M9")
    out = diagnostic(con)
    kdst = tool(out, "kdst_streaming")
    assert codes(kdst["roster"]) == {"streaming_slots"} and kdst["roster"]["status"] == "partial"
    ev = next(e for e in kdst["roster"]["evidence"] if e["code"] == "streaming_slots")
    assert ev["value"] == {"K": 2, "D/ST": 2}
    plan = build_kdst_streaming(con, team_id=1, now=NOW)
    for p in plan["positions"]:
        assert p["slots"] == 2
        assert p["slot_coverage"] == {"status": "partial", "code": "streaming_slots", "slots": 2, "planned_slots": 1,
                                      "reason": p["slot_coverage"]["reason"]}
        assert p["slot_coverage"]["reason"] in plan["notes"]
    standard = build_kdst_streaming(league("M1"), team_id=1, now=NOW)
    assert all("slot_coverage" not in p for p in standard["positions"])            # one slot: the shape is unchanged


# ---- Sleeper waiver legality and IR rules (G6, G7) ---------------------------------------------------------------------

def test_sleeper_waiver_legality_abstains_and_the_rules_page_and_diagnostic_say_why(league):
    """G6: Sleeper syncs no roster restrictions, so the waiver planner keeps abstaining (every move UNKNOWN); the league
    rules carry a `roster_restrictions` row (unverified) and the waivers diagnostic cites it by code."""
    from app.analytics.waiver_roster import plan_roster_move, roster_constraints

    con = league("M3")
    lg = latest_league(con)
    row = next(r for r in build_league_rules(lg)["engine_support"] if r["code"] == "roster_restrictions")
    assert row["status"] == "unverified" and "no roster restrictions" in row["detail"]
    c = roster_constraints(lg)
    assert c["settings_complete"] is False and c["capacity"] == 27
    state = {"players": [], "slots": ["QB"], "roster_slots": json.loads(lg["roster_slots"])}
    cand = {"espn_player_id": 1, "position": "QB", "raw_value": 1.0}
    assert plan_roster_move(state, cand, c)["status"] == "UNKNOWN"                 # the abstention is kept
    waivers = tool(diagnostic(con), "waivers")
    assert waivers["roster"]["status"] == "unverified" and "roster_restrictions" in waivers["refs"]
    espn = build_league_rules(latest_league(league("M2")))["engine_support"]
    assert "roster_restrictions" not in {r["code"] for r in espn}                  # ESPN syncs its restrictions


IR_RULES_ALL = {"reserve_allow_out": 1, "reserve_allow_na": 1, "reserve_allow_cov": 1, "reserve_allow_dnr": 1,
                "reserve_allow_doubtful": 1, "reserve_allow_sus": 0}


def _ir_add_drop_notes(con, status: str) -> dict:
    """Team 1's first IR occupant gets `status`; the add/drop simulator adds a free agent under the league's IR rules."""
    from app.analytics.add_drop import AddDropContext, evaluate_add_drop
    from app.analytics.waiver_roster import ir_rules, object_value

    lg = latest_league(con)
    ir = con.execute("SELECT min(espn_player_id) FROM lineup_snapshots WHERE team_id = 1 AND lineup_slot_id = 21 "
                     "AND scoring_period = ?", [cf.CURRENT]).fetchone()[0]
    con.execute("UPDATE fantasy_players SET injury_status = ? WHERE espn_player_id = ?", [status, ir])
    state = load_league_state(con, source="espn", now=NOW)
    add = next(p.espn_player_id for p in state.free_agents if p.position == "QB")
    ctx = AddDropContext(provider="Sleeper", ir_rules=ir_rules(object_value(lg["settings_raw"])))
    return {n["code"]: n for n in evaluate_add_drop(state, 1, add, None, ctx)["notes"]}


def test_sleeper_ir_rules_come_from_the_league_settings_and_abstain_when_absent(league):
    """G7, with invented reserve_allow_* settings (which keys real leagues carry is UNVERIFIED). A DOUBTFUL IR occupant
    in a league that allows doubtful players no longer blocks adds; one that disallows it blocks them; a league whose
    settings say nothing makes it unverified (never ESPN's statuses), and the rules page and diagnostic say which."""
    allows = cf.Row("M7a", "sleeper", cf.FX17, teams=6, sleeper_settings=IR_RULES_ALL)
    con = league(allows)
    rules = {r["code"]: r for r in build_league_rules(latest_league(con))["engine_support"]}
    assert rules["ir_eligibility"]["status"] == "handled" and "out, doubtful" in rules["ir_eligibility"]["detail"]
    assert not any(u["key"].startswith("reserve_allow_") for u in build_league_rules(latest_league(con))["unmapped"])
    ev = next(e for e in tool(diagnostic(con), "add_drop")["roster"]["evidence"] if e["code"] == "ir_eligibility")
    assert ev["status"] == "handled"
    notes = _ir_add_drop_notes(con, "DOUBTFUL")
    assert "IR_SLOT_INELIGIBLE" not in notes and "IR_ELIGIBILITY_UNKNOWN" not in notes

    strict = cf.Row("M7b", "sleeper", cf.FX17, teams=6, sleeper_settings={**IR_RULES_ALL, "reserve_allow_doubtful": 0})
    assert "IR_SLOT_INELIGIBLE" in _ir_add_drop_notes(league(strict), "DOUBTFUL")

    mixed = cf.Row("M7c", "sleeper", cf.FX17, teams=6, sleeper_settings={**IR_RULES_ALL, "reserve_allow_na": 0})
    con = league(mixed)
    row = next(r for r in build_league_rules(latest_league(con))["engine_support"] if r["code"] == "ir_eligibility")
    assert row["status"] == "partial" and row["undecided_statuses"] == ["OUT"]     # out, na, cov, dnr disagree
    assert "IR_ELIGIBILITY_UNKNOWN" in _ir_add_drop_notes(con, "OUT")

    con = league("M3")                                                             # no reserve_allow_* synced
    row = next(r for r in build_league_rules(latest_league(con))["engine_support"] if r["code"] == "ir_eligibility")
    assert row["status"] == "partial" and row["undecided_statuses"] == ["OUT", "DOUBTFUL", "SUSPENSION"]
    ev = next(e for e in tool(diagnostic(con), "waivers")["roster"]["evidence"] if e["code"] == "ir_eligibility")
    assert ev == {"code": "ir_eligibility", "status": "partial", "value": ["OUT", "DOUBTFUL", "SUSPENSION"]}
    notes = _ir_add_drop_notes(con, "DOUBTFUL")
    assert "IR_ELIGIBILITY_UNKNOWN" in notes and "IR_SLOT_INELIGIBLE" not in notes
    assert "ir_eligibility" not in {r["code"] for r in build_league_rules(latest_league(league("M2")))["engine_support"]}


def test_the_waiver_planner_abstains_on_an_undecided_ir_status_even_with_complete_settings():
    """G7 in the waiver planner: under a Sleeper league's full IR rules a QUESTIONABLE occupant (no setting covers it)
    leaves the move UNKNOWN, never legal, even with complete roster settings; a status the rules disallow blocks the
    add with wording that does not send a Sleeper manager to ESPN."""
    from app.analytics.reserve_slots import ir_rules
    from app.analytics.waiver_roster import plan_roster_move, roster_constraints

    slots = {"QB": 1, "BE": 2, "IR": 1}
    raw = {"provider": "sleeper", **IR_RULES_ALL, "rosterSettings": {"source": "explicit_slots"}}
    c = roster_constraints({"roster_slots": slots, "settings_raw": raw})
    assert c["capacity"] == 3 and c["settings_complete"] is True and c["ir_rules"] == ir_rules(raw)
    cand = {"espn_player_id": 9, "position": "QB", "raw_value": 1.0}

    def move(status):
        players = [{"espn_player_id": 1, "position": "QB", "lineup_slot_id": 0, "injury_status": None},
                   {"espn_player_id": 2, "position": "QB", "lineup_slot_id": 21, "injury_status": status}]
        return plan_roster_move({"players": players, "slots": ["QB"], "roster_slots": slots}, cand, c)

    for undecided in ("QUESTIONABLE", "PROBABLE"):
        out = move(undecided)
        assert out["status"] == "UNKNOWN" and "not decided by this league's synced IR rules" in out["summary"]
    blocked = move("SUSPENSION")                                                   # reserve_allow_sus = 0
    assert blocked["status"] == "UNKNOWN" and "ESPN" not in blocked["summary"]
    assert "does not allow the IR slot under this league's rules" in blocked["summary"]
    assert move("DOUBTFUL")["status"] != "UNKNOWN"                                 # allowed: the planner goes on
    espn = roster_constraints({"roster_slots": slots, "settings_raw": {"rosterSettings": {"source": "x"}}})
    assert espn["ir_rules"] is None
    assert "in ESPN" in plan_roster_move({"players": [{"espn_player_id": 2, "position": "QB", "lineup_slot_id": 21,
                                                       "injury_status": "QUESTIONABLE"}],
                                          "slots": ["QB"], "roster_slots": slots}, cand, espn)["summary"]


def test_a_league_starting_no_kicker_or_defense_has_nothing_to_stream(league):
    slots = {k: v for k, v in cf.STANDARD.items() if k not in ("K", "D/ST")}
    out = diagnostic(league(cf.Row("M10", "espn", slots)))
    assert tool(out, "kdst_streaming")["feature"]["status"] == "unsupported"


# ---- the rest of the matrix: PPR variants, known and unmapped custom scoring, taxi (M5, M6, M7) ----------------------

@pytest.mark.parametrize("row, fmt, ppr", [("M1", "ppr", 1.0), ("M5", "half_ppr", 0.5), ("M3", "half_ppr", 0.5),
                                           ("M5s", "ppr", 1.0)])
def test_ppr_variants_survive_the_import_and_the_full_ppr_waiver_basis_is_disclosed(league, row, fmt, ppr):
    con = league(row)
    rules = build_league_rules(latest_league(con))
    assert (rules["scoring"]["format"]["value"], rules["scoring"]["points_per_reception"]["value"]) == (fmt, ppr)
    support = {r["code"]: r for r in rules["engine_support"]}
    # xFP and player ratings are full PPR: a league scoring another PPR gets a partial `ppr` row, a full-PPR one none
    assert ("ppr" in support) is (ppr != 1.0)
    if ppr != 1.0:
        assert support["ppr"]["status"] == "partial" and f"use {ppr:g} per catch" in support["ppr"]["detail"]
    basis = next(e for e in tool(diagnostic(con), "waivers")["scoring"]["evidence"] if e["code"] == "player_value_basis")
    assert basis["status"] == "partial" and (basis["value"] or []) == (["ppr"] if ppr != 1.0 else [])
    if cf.MATRIX[row].provider == "espn":           # ESPN projects in the league's own scoring, whatever the PPR
        out = load_start_sit_evaluation(con, team_id=1, now=NOW)
        assert out["projection_source"] == "ESPN" and out["summary"]["projection_complete"] is True


def test_known_custom_scoring_is_read_and_espn_projections_stay_league_scored(league):
    """M6: a 6-point passing TD. The rules page shows it, Ours can score it, and the tools read ESPN's projections in
    that scoring, so every dimension is handled except what waivers never model."""
    con = league("M6")
    rules = build_league_rules(latest_league(con))
    assert {h["label"]: h["display"] for h in rules["scoring"]["highlights"]}["Passing TD"] == "6 per TD"
    assert {r["code"]: r["status"] for r in rules["engine_support"]}["skill_projections"] == "handled"
    out = diagnostic(con)
    assert {d: e["status"] for d, e in out["dimensions"].items()} == dict.fromkeys(("import", "roster", "scoring"),
                                                                                   "handled")
    assert tool(out, "start_sit")["scoring"]["status"] == "handled"
    assert tool(out, "waivers")["scoring"]["status"] == "partial"         # usage percentiles, not league points
    ss = load_start_sit_evaluation(con, team_id=1, now=NOW)
    assert ss["projection_source"] == "ESPN" and ss["summary"]["projection_complete"] is True


def test_unmapped_custom_scoring_abstains_and_never_becomes_a_measured_zero(league):
    """M6s: a Sleeper league scoring a 300-yard passing bonus and a key no reader knows. Ours and the kicker model
    abstain (named on the rules page and by the diagnostic), Sleeper publishes no projections, so every lineup total is
    unknown (None), never 0.0, and no source is named for numbers that do not exist."""
    con = league("M6s")
    lg = latest_league(con)
    rules = build_league_rules(lg)
    assert "zz_new_rule" in {u["key"] for u in rules["unmapped"]}
    support = {r["code"]: r for r in rules["engine_support"]}
    assert support["skill_projections"]["status"] == UNSUPPORTED
    assert "bonus_pass_yd_300" in support["skill_projections"]["detail"]
    assert support["kicker_projections"]["status"] == UNSUPPORTED and support["dst_projections"]["status"] == "handled"
    out = diagnostic(con)
    assert out["dimensions"]["scoring"]["status"] == "unsupported" and out["dimensions"]["roster"]["status"] == "handled"
    assert {"skill_projections", "kicker_projections"} <= codes(tool(out, "start_sit")["scoring"])
    assert tool(out, "start_sit")["projection_source"] == "none"

    ss = load_start_sit_evaluation(con, team_id=1, now=NOW)
    s = ss["summary"]
    assert ss["projection_source"] == NO_PROJECTIONS
    assert s["recommended_projected_total"] is None and s["actual_projected_total"] is None
    assert s["projected_gain"] is None and s["projection_complete"] is False
    state = load_league_state(con, now=NOW)
    for team, players in state.rosters.items():
        for week in project_team(players, state.slots, state.window(), state.projections)["weeks"]:
            assert week["projection_complete"] is False and week["projected_lineup_points"] is None, team


def test_fx17_with_a_taxi_squad_never_starts_taxi_or_ir_and_keeps_them_off_the_active_roster(league):
    """M7: FX-17 on Sleeper with three taxi players per team, each projected far above everyone else (provider-style
    rows, read with source="espn", as tests/test_sleeper_taxi.py does): no lineup starts them or an IR player, 17
    distinct players start with three FLEX, and taxi and IR spots are outside the 27 active roster spots."""
    con = league("M7")
    lg = latest_league(con)
    assert json.loads(lg["roster_slots"]) == {**cf.FX17, "TAXI": 3}
    taxi = {p.pid for p in cf.league_players(cf.MATRIX["M7"]) if p.slot == "TAXI"}
    rows = con.execute("SELECT espn_player_id, lineup_slot, is_starter FROM lineup_snapshots WHERE scoring_period = ? "
                       "AND lineup_slot = 'TAXI'", [cf.CURRENT]).fetchall()
    assert {r[0] for r in rows} == taxi and len(taxi) == 18 and not any(r[2] for r in rows)
    ids = [r[0] for r in con.execute("SELECT espn_player_id FROM fantasy_players WHERE availability_status = 'ONTEAM'")
           .fetchall()]
    con.executemany("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, "
                    "projected_points) VALUES (?, ?, ?, NULL, ?)",
                    [[cf.SEASON, pid, w, 40.0 if pid in taxi else cf.projection(cf.MATRIX["M7"], pid, w)]
                     for pid in ids for w in range(cf.CURRENT, cf.FINAL + 1)])
    assert build_league_rules(lg)["roster"]["starters"]["value"] == 17

    out = load_start_sit_evaluation(con, team_id=1, source="espn", now=NOW)
    rec = out["recommended_lineup"]
    reserved = {r[0] for r in con.execute("SELECT espn_player_id FROM lineup_snapshots WHERE team_id = 1 AND "
                                          "scoring_period = ? AND (lineup_slot_id = 21 OR lineup_slot = 'TAXI')",
                                          [cf.CURRENT]).fetchall()}
    assert len(reserved) == 6                                              # 3 IR + 3 taxi
    starters = [r["player_id"] for r in rec]
    assert len(rec) == 17 and None not in starters and len(set(starters)) == 17 and not set(starters) & reserved
    assert sorted(r["slot_id"] for r in rec if r["slot"] == "FLEX") == ["FLEX_0", "FLEX_1", "FLEX_2"]
    assert out["summary"]["projection_complete"] is True

    state = load_league_state(con, source="espn", now=NOW)
    assert state.active_capacity == 27
    for team, players in state.rosters.items():
        held = {p.espn_player_id for p in players if p.on_ir or p.on_taxi}
        assert len(players) == 33 and len(held) == 6, team
        for week in project_team(players, state.slots, state.window(), state.projections)["weeks"]:
            ids_ = [a["player_id"] for a in week["assignment"]]
            assert len(set(ids_)) == 17 and None not in ids_ and not set(ids_) & held
    rs = tool(diagnostic(con), "start_sit")["roster"]
    assert rs["status"] == "handled" and "IR and taxi players never start" in rs["reason"]


# ---- every tool, from the imported league file: an answer or an actionable abstention ---------------------------------

TOOL_ROUTES = ("/api/league-rules", "/api/league-compatibility", "/api/overview", "/api/start-sit?team_id=1",
               "/api/start-sit/what-if?team_id=1", "/api/lineup-projections", "/api/matchup?team_id=1",
               "/api/my-team/value?team_id=1&trades=false", "/api/waivers?team_id=1", "/api/waivers/add-drop?team_id=1",
               "/api/waivers/claim-plan?team_id=1", "/api/waivers/faab?team_id=1", "/api/trades/options",
               "/api/schedule-intelligence/bye-planner?team_id=1",
               "/api/schedule-intelligence/kdst-streaming?team_id=1", "/api/rankings", "/api/power-rankings",
               "/api/playoff-race", "/api/league-analyzer?team_id=1&vs=2")


# Pinned exceptions to "200": the only abstentions these imported files are expected to produce. A Sleeper league file
# that the analytics pipeline has not run on has no stored waiver suggestions for the other teams, and /api/waivers says
# so (503, "... have not been computed yet. Run the analytics refresh ..."); an ESPN file computes them on request.
NOT_COMPUTED = (503, "have not been computed yet")
EXPECTED = {("M3", "/api/waivers?team_id=1"): NOT_COMPUTED, ("M6s", "/api/waivers?team_id=1"): NOT_COMPUTED,
            ("M7", "/api/waivers?team_id=1"): NOT_COMPUTED, ("M8s", "/api/waivers?team_id=1"): NOT_COMPUTED}
# Trade discovery on standard-size rows only (about 5 s each). FX-17 (M2, M3, M7) is left out on purpose: one request
# took 451 s offline, with no budget and no abstention (STATUS follow-up 1p, branch fix/trade-discovery-budget).
DISCOVERY_ROWS = ("M6s", "M8p", "M8s")


@pytest.mark.parametrize("row", ["M2", "M3", "M6s", "M7", "M8p", "M8s"])
def test_every_tool_answers_or_abstains_with_a_reason_from_the_imported_file(tmp_path, monkeypatch, row):
    """The league file each matrix row's real import writes, served by the API: every tool's route answers (200), and
    the only abstention is the pinned one in EXPECTED, with its reason; no 404 and no 5xx anywhere else. An evaluated
    trade answers too. Team value runs with `trades=false`: its trade preview alone took 19 s on FX-17 (0.6 s without),
    the same discovery cost as above, so this test checks the value part and leaves the preview to that follow-up."""
    db = tmp_path / f"{row}.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    cf.import_row(con, cf.MATRIX[row])
    con.close()
    api = _client(monkeypatch, db)
    routes = TOOL_ROUTES + (("/api/trades/discover?team_id=1",) if row in DISCOVERY_ROWS else ())
    for route in routes:
        r = api.get(route)
        status, reason = EXPECTED.get((row, route), (200, None))
        assert r.status_code == status, (route, r.status_code, r.text[:300])
        if reason:
            assert reason in r.json()["detail"], (route, r.text[:300])
    players = cf.league_players(cf.MATRIX[row])
    give = next(p.pid for p in players if p.team == 1 and p.slot == "BE")
    get = next(p.pid for p in players if p.team == 2 and p.slot == "BE")
    r = api.post("/api/trades/evaluate", json={"team_a_id": 1, "team_b_id": 2, "team_a_sends": [give],
                                               "team_b_sends": [get]})
    assert r.status_code == 200, r.text[:300]
    if cf.MATRIX[row].provider == "sleeper":         # no projections in a Sleeper file without Ours runs: not measured
        assert r.json()["team_a"]["deltas"]["ros_delta"] is None


# ---- league rules codes ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("row", list(cf.MATRIX))
def test_engine_support_rows_carry_unique_stable_codes(league, row):
    support = build_league_rules(latest_league(league(row)))["engine_support"]
    assert [r["code"] for r in support] and all(r["code"] in ENGINE_CODES for r in support)
    assert len({r["code"] for r in support}) == len(support)


# ---- the endpoint ------------------------------------------------------------------------------------------------------

def _client(monkeypatch, path):
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_endpoint_reads_the_league_file(tmp_path, monkeypatch):
    db = tmp_path / "fx17.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    cf.import_row(con, cf.MATRIX["M2"])
    con.close()
    r = _client(monkeypatch, db).get("/api/league-compatibility")
    assert r.status_code == 200, r.text
    body = r.json()
    well_formed(body)
    assert str(cf.ef.LEAGUE_ID) not in r.text


def test_the_endpoint_answers_503_before_any_sync(tmp_path, monkeypatch):
    db = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db))
    init_db(con)
    con.close()
    r = _client(monkeypatch, db).get("/api/league-compatibility")
    assert r.status_code == 503 and "synced" in r.json()["detail"]


from league_fixtures import publish_local                                    # noqa: E402
from test_league_scope import client, e2e, served                            # noqa: E402,F401  (fixtures)
from test_leagues_api import bearer, env                                     # noqa: E402,F401  (fixtures)


def test_v2_league_compatibility_is_league_scoped(pg, served, client, e2e, tmp_path):  # noqa: F811
    from cp_seed import U1, U3, seed_membership, seed_user
    private = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U3)
        seed_membership(con, U1, private.league_id, "viewer")
        con.commit()
    for lg, headers in ((private.league_id, {}), (private.league_id, bearer(U3)), ("lg_" + "z" * 24, {}),
                        ("lg_" + "z" * 24, bearer(U1))):
        r = client.get(f"/v2/leagues/{lg}/api/league-compatibility", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (lg, headers)
    r = client.get(f"/v2/leagues/{private.league_id}/api/league-compatibility", headers=bearer(U1))
    assert r.status_code == 200 and r.json()["contract"] == CONTRACT
    assert r.headers["cache-control"] == "private, no-store"
    assert private.league_id not in r.text


def test_dual_position_count_needs_provider_base_position_evidence_not_flex_or_ir():
    def row(pid, position, eligible, slot_id=2):
        return {"espn_player_id": pid, "known_id": pid, "position": position, "eligible_slots": eligible,
                "lineup_slot_id": slot_id, "lineup_slot": "RB", "is_starter": slot_id != 20}
    rows = [
        row(100, "WR", [4, 2, 23]),  # second base position RB; flex id 23 by itself would not count
        row(100, "WR", [4, 2, 23]),  # duplicate snapshot is counted once
        row(101, "RB", [2, 23, 20]),  # flex and bench slots are not a second position
        row(102, "RB", [2, 4], 21),  # player is held on IR; not active
        row(103, "WR", "malformed JSON"),
        row(104, "WR", None),
        {**row(105, "RB", [2, 4]), "known_id": None},
    ]
    standard = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "D/ST", "K"]
    assert active_dual_position_count(rows, standard) == 1
    assert active_dual_position_count([], standard) == 0


def test_dual_position_count_follows_the_slots_the_solver_could_start_him_in():
    # A WR also listed at TE is dual only where some starting slot takes TE (lineup.dual_positions): with no TE and no
    # flex that takes TE the solver never starts him at TE, so the rankings finding must not count him either.
    wr_te = {"espn_player_id": 200, "known_id": 200, "position": "WR", "eligible_slots": [4, 6],
             "lineup_slot_id": 4, "lineup_slot": "WR", "is_starter": True}
    assert active_dual_position_count([wr_te], ["QB", "RB", "RB", "WR", "WR", "TE"]) == 1
    assert active_dual_position_count([wr_te], ["QB", "RB", "RB", "WR", "WR", "FLEX"]) == 1
    assert active_dual_position_count([wr_te], ["QB", "RB", "RB", "WR", "WR", "RB/WR"]) == 0
    assert active_dual_position_count([wr_te], []) == 0


def test_fx17_dual_position_diagnostic_does_not_claim_primary_position_rank_fidelity(league):
    con = league("M2")
    pid = con.execute("""
        SELECT DISTINCT l.espn_player_id
        FROM lineup_snapshots l JOIN fantasy_players p USING (espn_player_id)
        WHERE p.position = 'WR' AND l.is_starter = true AND l.scoring_period = 3
        LIMIT 1
    """).fetchone()[0]
    con.execute("UPDATE fantasy_players SET eligible_slots = ? WHERE espn_player_id = ?",
                [json.dumps([4, 2, 23]), pid])
    lg = latest_league(con)
    facts = load_facts(con, lg)
    assert facts["active_dual_position_players"] >= 1
    out = build_compatibility(lg, facts)
    rank = tool(out, "rankings")["feature"]
    assert rank["status"] == "partial"
    assert "dual_position_rankings" in codes(rank)
    assert "primary position" in rank["reason"]
    lineup = tool(out, "start_sit")
    assert "dual_position_rankings" not in codes(lineup["feature"])
    assert "espn_player_id" not in str(out) and "full_name" not in str(out)
