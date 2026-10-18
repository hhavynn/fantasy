"""The production model ifp-2.1.0 (app.analytics.injury_model): the iwh-0.1.1 participation fix, the training
artifact, the live run builder (handoff features, rest-of-season return-rate P(plays)), versioning and the
fallback. Synthetic data only (the worlds of test_injury_workload and test_ros_projections)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import injury_model as im
from app.analytics import injury_workload as iw
from app.analytics import production_forecasts as pf
from app.analytics import production_pin
from app.analytics import projection_source as ps
from app.analytics import weekly_learning as wl
from app.analytics.forecast_backtest import build_store
from app.analytics.independent_projection_store import build_live, register_approved_artifact, write_run

import test_injury_workload as tiw
from test_ros_projections import SCORING, W, build_world, by_id, participation_artifact

CREATED = "2026-08-01T00:00:00+00:00"
OUT_RB, DOUBTFUL_WR, HEALTHY_WR = 2, 11, 1          # test_ros_projections.PLAYERS (ESPN OUT / DOUBTFUL / ACTIVE)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """One synthetic world and its ifp-2.1.0 week-6 artifact (training takes a few seconds: shared)."""
    con, src = build_world(tmp_path_factory.mktemp("im") / "live.duckdb")
    art = wl.train_production(src, 2026, W, cfg=ch.ChallengerConfig.load(), scoring_desc=SCORING.describe(),
                              created_at=CREATED)
    yield con, src, art
    con.close()


def with_table(art, rate=0.25, n=100):
    """`art` with a return-rate table whose pooled cells (every horizon) are `rate` (the synthetic world has no
    injury history to fit one)."""
    cells = [{"position": "ALL", "missed": "ALL", "h": h, "rate": rate, "n": n} for h in range(1, im.RETURN_HORIZON + 1)]
    cfg = {**art.config, im.CONFIG_KEY: {**art.config[im.CONFIG_KEY], "return_rates": cells}}
    return replace(art, config=cfg)


# ----------------------------------------------------------------------------- versions and governance


def test_versions_are_wired_consistently():
    assert ps.MODEL_VERSION == im.MODEL_VERSION == "ifp-2.1.0"
    assert ps.FALLBACK_VERSION == im.BASE_VERSION == ch.CHALLENGER_VERSION == "ifp-2.0.0"
    assert ps.CONSUMER_VERSIONS == (ps.MODEL_VERSION, ps.FALLBACK_VERSION)
    assert production_pin.REGISTERED_MODEL_VERSIONS == ps.CONSUMER_VERSIONS
    assert production_pin.REGISTERED_MODEL_VERSION == ps.MODEL_VERSION
    assert production_pin.PINNED_MODEL_VERSION == ps.RETIRED_VERSION == "ifp-1.0.0"
    assert im.WORKLOAD_VERSION == iw.WORKLOAD_VERSION == "iwh-0.1.1"


def test_governance_notes_are_short_and_neutral():
    for v in ps.CONSUMER_VERSIONS:
        note = ps.GOVERNANCE[v]["status_note"]
        assert len(note) < 120 and "not shown to be better" not in note
    assert "not shown to be better" not in ps.NOTE and ps.MODEL_VERSION in ps.NOTE and ps.FALLBACK_VERSION in ps.NOTE
    g = ps.governance(ps.MODEL_VERSION)
    assert g["production_version"] == "ifp-2.1.0" and g["fallback_version"] == "ifp-2.0.0" and not g["is_fallback"]
    assert g["start_sit_ready"] is False and g["production_approval"] == "owner_override"
    assert ps.governance(ps.FALLBACK_VERSION)["is_fallback"] is True


def test_both_owner_approved_versions_report_the_override_status():
    stored_shadow = {p: {"status": "shadow"} for p in ps.POSITIONS}
    for v in ps.CONSUMER_VERSIONS:
        assert ps.consumer_status({"model_version": v, "model_status": stored_shadow}) == \
            {p: ps.OVERRIDE_STATUS for p in ps.POSITIONS}
    assert ps.consumer_status({"model_version": "ifp-1.0.0", "model_status": {"QB": {"status": "validated"}}}) == \
        {"QB": "validated"}


def test_both_weekly_versions_are_gated_by_approved_artifacts(trained):
    con, _, _ = trained
    for v in ps.CONSUMER_VERSIONS:
        register_approved_artifact(con, model_version=v, artifact_sha256=("c" if v == ps.MODEL_VERSION else "d") * 64,
                                   target_season=2026, target_week=W, training_cutoff_key=202605, source="manual_pin")
        gate = pf.artifact_gate(con, v)
        ok = "c" if v == ps.MODEL_VERSION else "d"
        bad = "d" if ok == "c" else "c"
        assert gate({"artifact_id": ok * 64, "season": 2026, "week": 7})[0] == "verified"
        # an artifact approved for the OTHER version does not count for this one
        assert gate({"artifact_id": bad * 64, "season": 2026, "week": 7}) == ("rejected", pf.NOT_REGISTERED)
    con.execute("DELETE FROM approved_artifacts")


# ----------------------------------------------------------------------------- iwh-0.1.1 participation fix


def test_a_questionable_and_inactive_game_is_an_injury_absence_in_0_1_1():
    """STAR was listed Questionable for week 4 and did not play: iwh-0.1.1 keeps his claim (an injury absence, not
    a role absence). An unlisted missed game (a healthy scratch) still lowers it."""
    rows = tiw.report(4, tiw.inj(tiw.STAR, "Questionable", 4, practice="Limited Participation in Practice"))
    src = tiw.world(range(1, 6), skip={tiw.STAR: (4,)}, injuries=rows)
    listed = tiw.rows_by_id(im.handoff(src, tiw.S, 6))[tiw.STAR]
    healthy_scratch = tiw.world(range(1, 6), skip={tiw.STAR: (4,)}, injuries=tiw.report(4))
    scratch = tiw.rows_by_id(im.handoff(healthy_scratch, tiw.S, 6))[tiw.STAR]
    assert listed["ho_participation"] == pytest.approx(1.0) and scratch["ho_participation"] == pytest.approx(0.75)
    assert listed["ho_car_share"] > scratch["ho_car_share"]


# ----------------------------------------------------------------------------- return-rate table


def test_return_rate_cell_pools_thin_cells_and_caps_the_horizon():
    rates = {("RB", "1", 1): {"rate": 0.5, "n": 10}, ("ALL", "1", 1): {"rate": 0.4, "n": 50},
             ("ALL", "ALL", 1): {"rate": 0.3, "n": 500}, ("ALL", "ALL", 3): {"rate": 0.6, "n": 400}}
    assert iw.return_rate_cell(rates, "RB", 1, 1) == (("ALL", "1", 1), {"rate": 0.4, "n": 50})   # RB cell too thin
    assert iw.return_rate_cell(rates, "WR", 5, 1)[0] == ("ALL", "ALL", 1)                        # bucket 4+ unseen
    assert iw.return_rate_cell(rates, "RB", 0, 9)[0] == ("ALL", "ALL", 3)                        # h capped at 3
    assert iw.return_rate_cell({}, "RB", 0, 1) is None


def test_return_table_counts_only_players_on_the_active_roster_at_the_cutoff():
    """ros-avail-0.1.1: a reserve-list player is withheld by build_live and never gets a rest-of-season P(plays), so
    his episode is not in the production table (the research default keeps every episode)."""
    rows = tiw.report(4, tiw.inj(tiw.STAR, "Out", 4))
    src = tiw.world(range(1, 6), skip={tiw.STAR: (4,)}, injuries=rows, roster={(tiw.STAR, 4): ("AAA", "RES")})
    assert iw.fit_return_rates(src, 202606)[("ALL", "ALL", 1)]["n"] == 1
    assert ("ALL", "ALL", 1) not in iw.fit_return_rates(src, 202606, active_roster_only=True)
    assert im.fit_return_table(src, tiw.S, 6) == []


def test_return_table_is_fitted_only_on_outcomes_before_the_cutoff():
    """An Out player who returns in week 5: the week-4 episode's h=1 outcome (week 5) counts for a forecast at week 6,
    not for one at week 5."""
    rows = tiw.report(4, tiw.inj(tiw.STAR, "Out", 4))
    src = tiw.world(range(1, 6), skip={tiw.STAR: (4,)}, injuries=rows)
    at6 = {(c["position"], c["missed"], c["h"]): c for c in im.fit_return_table(src, tiw.S, 6)}
    at5 = {(c["position"], c["missed"], c["h"]): c for c in im.fit_return_table(src, tiw.S, 5)}
    assert at6[("ALL", "ALL", 1)] == {"position": "ALL", "missed": "ALL", "h": 1, "rate": 1.0, "n": 1}
    assert ("ALL", "ALL", 1) not in at5
    assert max(c["h"] for c in at6.values()) <= im.RETURN_HORIZON


# ----------------------------------------------------------------------------- training artifact


def test_training_artifact_extends_ifp2_with_the_handoff_group(trained):
    con, src, art = trained
    assert art.version == im.MODEL_VERSION and im.is_injury_artifact(art)
    cfg = ch.ChallengerConfig.load()
    for pos, m in art.models.items():
        assert m.model == im.MODEL_NAME and m.features == im.features(cfg, pos)
        assert set(im.HANDOFF_FEATURES) <= set(m.features) if pos != "QB" else not set(im.HANDOFF_FEATURES) & set(m.features)
        assert [f for f in m.features if f not in im.HANDOFF_FEATURES] == cfg.features(pos)    # ifp-2.0.0's spec
    prov = art.config["provenance"]
    assert prov["role"] == "production" and prov["retrospective"] is False
    assert prov["trained_after_first_kickoff"] is False and prov["data_cutoff_key"] < 2026 * 100 + W
    c = art.config[im.CONFIG_KEY]
    assert c["workload_version"] == "iwh-0.1.1" and c["listing_absence"] is True and c["base"] == "ifp-2.0.0"
    assert art.config["config_hash"] == im.config_hash(cfg)
    # the same training rows as ifp-2.0.0 (challenger.train_rows) for every fitted position
    store = wl.training_store(src, 2026, W)
    assert prov["rows"] == {p: ch.train_rows(store, 2026 * 100 + W, p).height for p in art.models}


def test_training_handoff_uses_each_weeks_own_cutoff(trained):
    _, src, _ = trained
    store = build_store(src, [(2026, 3), (2026, 4)])
    out = im.with_training_handoff(src, store)
    assert out.height == store.height and set(im.HANDOFF_FEATURES) <= set(out.columns)
    wk4 = im.handoff(src, 2026, 4).select("gsis_id", "ho_exp_xfpl")
    got = out.filter(pl.col("week") == 4).select("gsis_id", "ho_exp_xfpl").join(wk4, on="gsis_id", suffix="_w")
    assert got.height and got.filter(pl.col("ho_exp_xfpl") != pl.col("ho_exp_xfpl_w")).height == 0
    assert im.handoff_hash(out) == im.handoff_hash(im.with_training_handoff(src, store))     # reproducible


def test_fit_refuses_a_store_without_the_handoff_columns(trained):
    _, src, _ = trained
    with pytest.raises(ValueError, match="with_training_handoff"):
        im.fit(wl.training_store(src, 2026, W), 2026, W, ch.ChallengerConfig.load())


def test_artifact_saves_registers_and_loads_like_any_weekly_artifact(trained, tmp_path):
    con, _, art = trained
    aid, path, created = wl.save_artifact(art, tmp_path)
    assert created and path.parent.name == im.MODEL_VERSION
    sha, new = wl.register_saved_artifact(con, art, path, tmp_path, "learning_cycle")
    assert new and con.execute("SELECT model_version FROM approved_artifacts WHERE artifact_sha256 = ?",
                               [sha]).fetchone()[0] == im.MODEL_VERSION
    loaded, aid2, _ = wl.latest_artifact(tmp_path, im.MODEL_VERSION, 2026, W, art.scoring_fingerprint)
    assert aid2 == aid and im.is_injury_artifact(loaded) and im.return_table(loaded) == im.return_table(art)
    con.execute("DELETE FROM approved_artifacts")


def test_an_artifact_introduced_after_kickoff_is_labeled_and_never_evidence(trained):
    _, src, _ = trained
    art = wl.train_production(src, 2026, W, cfg=ch.ChallengerConfig.load(), scoring_desc=SCORING.describe(),
                              created_at=CREATED, after_kickoff=True)
    assert art.config["provenance"]["trained_after_first_kickoff"] is True
    assert art.config["provenance"]["retrospective"] is False          # not a replay: it can be approved
    assert wl.artifact_id(art) != wl.artifact_id(wl.train_production(
        src, 2026, W, cfg=ch.ChallengerConfig.load(), scoring_desc=SCORING.describe(), created_at=CREATED))


# ----------------------------------------------------------------------------- live runs


def test_horizon_zero_run_reads_the_handoff_features(trained, monkeypatch):
    con, src, art = trained
    seen = {}
    real = im.augment

    def spy(*a, **kw):
        out = real(*a, **kw)
        seen["cols"] = out.columns
        seen["n"] = out.filter(pl.col("ho_exp_xfpl").is_not_null()).height
        return out

    monkeypatch.setattr(im, "augment", spy)
    meta, rows = build_live(con, src, art, 2026, W, participation=participation_artifact(), artifact_sha256="a" * 64)
    assert set(im.HANDOFF_FEATURES) <= set(seen["cols"]) and seen["n"] > 0
    assert meta["model_version"] == im.MODEL_VERSION and meta["forecast_horizon"] == 0
    r = by_id(rows)
    assert r[HEALTHY_WR]["coverage_status"] == "projected" and r[HEALTHY_WR]["model"] == "ifp21_injury_aware_ridge"
    assert r[OUT_RB]["coverage_status"] == "withheld_unavailable"           # ESPN OUT withholds the next game
    # no horizon-0 row gets the return-rate P(plays): the cutoff week's own report applies
    assert all((x["participation"] or {}).get("model_version") != im.RETURN_RATE_MODEL for x in rows)


def test_rest_of_season_out_or_doubtful_players_get_the_return_rate(trained):
    con, src, art = trained
    part = participation_artifact()
    tab = with_table(art, rate=0.25)
    meta, rows = build_live(con, src, tab, 2026, 7, participation=part, artifact_sha256="a" * 64, projected_from_week=W)
    r = by_id(rows)
    assert meta["forecast_horizon"] == 1 and meta["projected_from_week"] == W
    for pid, designation in ((OUT_RB, "out"), (DOUBTFUL_WR, "doubtful")):
        x = r[pid]
        assert x["coverage_status"] == "projected" and x["p_play"] == 0.25
        assert x["expected_points"] == round(0.25 * x["projected_points"], 2)
        q = x["participation"]
        assert q["model_version"] == im.RETURN_RATE_MODEL and q["return_rate"]["designation"] == designation
        assert q["return_rate"]["h"] == 1 and q["undesignated_p_play"] is not None
        assert any("historical return rate 25%" in f for f in x["flags"])
    healthy = r[HEALTHY_WR]
    assert healthy["participation"]["model_version"] == part.version and healthy["p_play"] != 0.25
    # h counts the TEAM's games after the cutoff: h = 2 still uses the table (its own cell) ...
    _, rows8 = build_live(con, src, with_table(art, rate=0.6), 2026, 8, participation=part, artifact_sha256="a" * 64,
                          projected_from_week=W)
    assert by_id(rows8)[OUT_RB]["participation"]["return_rate"]["h"] == 2 and by_id(rows8)[OUT_RB]["p_play"] == 0.6
    # ... and from h = 3 on the participation model's estimate stays (ros-avail-0.1.1: the table lost there)
    _, rows9 = build_live(con, src, with_table(art, rate=0.6), 2026, 9, participation=part, artifact_sha256="a" * 64,
                          projected_from_week=W)
    assert by_id(rows9)[OUT_RB]["participation"]["model_version"] == part.version
    assert im.PPLAY_HORIZONS == 2 and im.ROS_AVAILABILITY_VERSION == "ros-avail-0.1.1"


def test_without_a_fitted_table_the_undesignated_estimate_stays(trained):
    con, src, art = trained
    assert im.return_table(art) == {}           # the synthetic world has no injury history
    _, rows = build_live(con, src, art, 2026, 7, participation=participation_artifact(), artifact_sha256="a" * 64,
                         projected_from_week=W)
    assert all((x["participation"] or {}).get("model_version") != im.RETURN_RATE_MODEL for x in rows)


def test_earlier_model_runs_are_unchanged_by_the_new_code(trained):
    """An ifp-2.0.0 artifact (no injury_model config) never reads the handoff: identical rows with the module
    unavailable."""
    con, src, art = trained
    base = replace(art, version=ps.FALLBACK_VERSION, config={k: v for k, v in art.config.items() if k != im.CONFIG_KEY},
                   models={})
    assert not im.is_injury_artifact(base)
    _, rows = build_live(con, src, base, 2026, 7, participation=participation_artifact(), artifact_sha256="a" * 64,
                         projected_from_week=W)
    assert all(x["coverage_status"] != "projected" for x in rows)      # no models: nothing projected, nothing crashed


def test_switch_serves_ifp_2_1_and_falls_back_to_ifp_2_0(trained):
    con, src, art = trained
    part = participation_artifact()
    for v, sha in ((ps.FALLBACK_VERSION, "e"), (ps.MODEL_VERSION, "f")):
        register_approved_artifact(con, model_version=v, artifact_sha256=sha * 64, target_season=2026, target_week=W,
                                   training_cutoff_key=202605, source="learning_cycle")
    meta0, rows0 = build_live(con, src, replace(art, version=ps.FALLBACK_VERSION), 2026, W, participation=part,
                              artifact_sha256="e" * 64)
    write_run(con, meta0, rows0)
    w = ps.weekly(con, 2026, W, "independent")
    assert w["run"]["model_version"] == ps.FALLBACK_VERSION and w["governance"]["is_fallback"]
    assert ps.FALLBACK_VERSION in w["reason"] and ps.MODEL_VERSION in w["reason"]
    meta1, rows1 = build_live(con, src, art, 2026, W, participation=part, artifact_sha256="f" * 64)
    meta1["created_at"] = meta0["created_at"] - timedelta(minutes=5)       # older than the fallback run: still wins
    write_run(con, meta1, rows1)
    w = ps.weekly(con, 2026, W, "independent")
    assert w["run"]["model_version"] == ps.MODEL_VERSION and w["reason"] is None and not w["governance"]["is_fallback"]
    assert w["model_status"] == {p: ps.OVERRIDE_STATUS for p in ps.POSITIONS}
    # a K row is not in the skill run: it stays on ESPN for the kicker overlay to replace
    assert all(o in (ps.OURS, ps.OURS_ZERO, ps.ESPN) for o in w["origin"].values())
    for t in ("independent_projection_rows", "independent_projection_runs", "approved_artifacts"):
        con.execute(f"DELETE FROM {t}")


def test_describe_is_json_safe(trained):
    _, _, art = trained
    d = im.describe(with_table(art))
    json.dumps(d)
    assert d["version"] == im.MODEL_VERSION and d["return_rates"]["1"]["rate"] == 0.25
    assert d["features"]["RB"] == list(im.HANDOFF_FEATURES) and d["features"]["QB"] == []
