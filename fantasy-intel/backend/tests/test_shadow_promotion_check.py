"""scripts/shadow_promotion_check.py on synthetic saved shadow results: a pass, a fail and too few weeks for every
shadow's result shape, the PROMOTION READY signal (log line, queue, latest summary), dry runs, unreadable files,
already-promoted candidates and the promotion brief. Files only: no database, no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
NOW = "2026-12-15T12:00:00+00:00"


@pytest.fixture(scope="module")
def spc():
    spec = importlib.util.spec_from_file_location("shadow_promotion_check", SCRIPTS / "shadow_promotion_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def no_promoted(spc, monkeypatch):
    """The ready / review paths are tested from an empty PROMOTED; the committed map is tested on its own below."""
    monkeypatch.setattr(spc, "PROMOTED", {})


def test_committed_promoted_map_is_what_ifp_2_2_ships(spc):
    """ifp-2.2.0's shipped components (context_model.SHIPPED), by shadow, are exactly the committed PROMOTED map."""
    spec = importlib.util.spec_from_file_location("shadow_promotion_check_committed", SCRIPTS / "shadow_promotion_check.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    real = fresh.PROMOTED
    from app.analytics import context_model as cm
    want: dict[str, set] = {}
    for pos, comps in cm.SHIPPED.items():
        for c in comps:
            want.setdefault(cm.COMPONENTS[c]["shadow"], set()).add(pos)
    assert {k: set(v["positions"]) for k, v in real.items()} == want
    assert all(v["version"] == cm.MODEL_VERSION for v in real.values())
    assert "ifp-fr-p1" not in real and "ipm-cal-0.1.0" not in real


@pytest.fixture
def dirs(tmp_path):
    return {"ifp": tmp_path / "models", "dst": tmp_path / "dst", "logs": tmp_path / "logs"}


def _args(spc, dirs, *extra, command="check"):
    return spc.parse([command, *extra, "--ifp-dir", str(dirs["ifp"]), "--dst-dir", str(dirs["dst"]),
                      "--logs-dir", str(dirs["logs"])])


def _put(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _gate_checks(ok: bool, weeks: int) -> dict:
    return {"weeks": weeks >= 8, "appearances": True, "mae_ci_upper_below_0": ok, "bias": True, "starters_pool": True}


def _status(ok: bool, weeks: int) -> str:
    return "insufficient_data" if weeks < 8 else "meets_gate" if ok else "fails_gate"


# ----------------------------------------------------------------------------- writers: each shadow's saved shape


def write_ctx(d, ok, weeks):
    _put(d["ifp"] / "research" / "context" / "shadow" / "scores.json", {
        "scored_at": NOW, "candidate": "ifp-ctx-p1",
        "prospective": {"scored_weeks": weeks, "gate": {p: {"checks": _gate_checks(ok, weeks),
                                                            "status": _status(ok, weeks)} for p in ("RB", "WR")}}})
    _put(d["ifp"] / "research" / "context" / "prospective.json", {"report": {"RB": {"n": 640}, "WR": {"n": 910}}})


def write_p2q(d, ok, weeks):
    _put(d["ifp"] / "research" / "qb_scenario_shadow" / "prospective.json", {
        "status": "scored", "scored_weeks": weeks, "matched_weeks": {"QB": weeks}, "report": {"QB": {"n": 420}},
        "gate": {"QB": {"checks": _gate_checks(ok, weeks), "status": _status(ok, weeks)}}})


def write_ipm(d, ok, weeks):
    checks = {"weeks": weeks >= 8, "rows": True, "log_loss_ci_upper_below_0": ok, "brier_ci_upper_below_0": True,
              "unconditional_mae": True}
    _put(d["ifp"] / "research" / "participation_cal_shadow" / "scores.json", {
        "scored_at": NOW, "weeks": weeks, "n": 450, "gate": {"checks": checks, "status": _status(ok, weeks)}})


def write_dst(d, ok, weeks):
    cand = ({"status": "accumulating", "reason": f"{weeks} of 8 scored weeks"} if weeks < 8 else
            {"status": "passes" if ok else "fails",
             "checks": {"mae_better_than_espn": ok, "ranking_not_worse": True, "bias_ok": True}})
    _put(d["dst"] / "shadow" / "scores.json", {"scored_at": NOW, "scored_weeks": weeks,
                                               "gate": {"weeks": weeks, "n": 32 * weeks,
                                                        "candidates": {"dfp-r1": cand}}})


def write_dist(d, ok, weeks):
    def block(n, status):
        return {"n": n, "weeks": weeks, "bounds": {"status": status, "reasons": []}}
    status = "insufficient_data" if weeks < 3 else "within_bounds" if ok else "outside_bounds"
    pos = {"QB": {"policy": {"conditional": "restricted", "unconditional": "restricted"},
                  "conditional": block(500, "within_bounds"), "unconditional": block(500, "within_bounds")},
           "RB": {"policy": {"conditional": "candidate", "unconditional": "withheld"},
                  "conditional": block(900, status), "unconditional": {"withheld": "RB participation"}},
           "WR": {"policy": {"conditional": "candidate", "unconditional": "candidate"},
                  "conditional": block(1200, status), "unconditional": block(1300, "within_bounds")}}
    _put(d["ifp"] / "research" / "point_distributions" / "ledger" / "scores.json", {
        "scored_at": NOW, "weeks_scored": [f"2026-{w:02d}" for w in range(7, 7 + weeks)],
        "by_mean": {"ifp-1.0.0": {"positions": pos}, "ifp-2.0.0": {"positions": {}}}})


def write_iwh(d, ok, weeks):
    per = {p: {"n": 700, "checks": {**_gate_checks(ok, weeks), "c3_mse_ci_lower_not_above_0": True}}
           for p in ("RB", "WR", "TE")}
    _put(d["ifp"] / "research" / "injury_workload" / "prospective_ifp-iwh-0.1.1.json", {
        "version": "ifp-iwh-0.1.1", "gate": {"weeks": weeks, "per_position": per,
                                             "pooled_checks": {"c1_pooled_mse": ok, "c7_mechanism": True,
                                                               "self_returning_n": True},
                                             "outcome": _status(ok, weeks)}})


def write_signal(d, ok, weeks, *, only=None):
    def cand(name, positions):
        good = ok if only in (None, name) else False
        crit = [{"name": "frozen_pregame_evidence", "result": "pass"},
                {"name": "prospective_weeks", "result": "pass" if weeks >= 8 else "insufficient_data"},
                {"name": "matched_appearances", "result": "pass"},
                {"name": "mae_vs_champion", "result": "pass" if good else "fail"}]
        status = ("insufficient_evidence" if weeks < 8 else "eligible_for_review" if good else "failed_evaluation")
        return {"report": {"positions": {p: {"n": 500, "weeks": weeks} for p in positions}},
                "gate": {p: {"status": status, "criteria": crit, "blocking": []} for p in positions}}
    _put(d["ifp"] / "research" / "signal_shadows" / "scores.json", {
        "scored_at": NOW, "scored_weeks": list(range(202607, 202607 + weeks)),
        "candidates": {"ifp-fr-p1": cand("ifp-fr-p1", ["RB", "WR"]), "ifp-dct-p1": cand("ifp-dct-p1", ["QB"]),
                       "ifp-awd-p1": cand("ifp-awd-p1", ["RB", "WR", "TE"])}})


def write_injury_return(d, ok, weeks, *, choice=None):
    def cand():
        checks = {"cohort_ci_below_0": ok, "overall_not_worse": True, "no_position_clearly_worse": True}
        status = "accumulating" if weeks < 8 else "passes" if ok else "fails"
        return {"positions": {p: {"n": 300} for p in ("RB", "WR", "TE")},
                "gate": {"status": status, "checks": checks, "weeks": weeks}}
    _put(d["ifp"] / "research" / "injury_return_shadow" / "scores.json", {
        "scored_at": NOW, "scored_weeks": list(range(202608, 202608 + weeks)),
        "candidates": {"ifp-irs-p1": cand(), "ifp-irb-p1": cand()},
        "both": {"reported_only": True, "choice": choice}})


WRITERS = {"ifp-ctx-p1": (write_ctx, ["RB", "WR"]), "ifp-ctx-p2q": (write_p2q, ["QB"]),
           "ipm-cal-0.1.0": (write_ipm, ["QB"]), "dfp-r1": (write_dst, ["DST"]),
           "ifpd-0.1.0": (write_dist, ["RB", "WR"]), "ifp-iwh-0.1.1": (write_iwh, ["RB", "TE", "WR"]),
           "ifp-fr-p1": (write_signal, ["RB", "WR"]), "ifp-dct-p1": (write_signal, ["QB"]),
           "ifp-awd-p1": (write_signal, ["RB", "TE", "WR"]),
           "ifp-irs-p1": (write_injury_return, ["RB", "TE", "WR"]),
           "ifp-irb-p1": (write_injury_return, ["RB", "TE", "WR"])}


def _cand(summary, name):
    return next(c for c in summary["candidates"] if c["candidate"] == name)


# ----------------------------------------------------------------------------- per-shadow pass / fail / too few weeks


def test_every_shadow_in_the_brief_is_watched(spc):
    assert [s["candidate"] for s in spc.SHADOWS] == list(WRITERS)


def test_min_weeks_is_the_shadow_gate_minimum(spc):
    from app.analytics.challenger import SHADOW_GATE
    assert spc.MIN_WEEKS == SHADOW_GATE["min_prospective_weeks"] == 8


@pytest.mark.parametrize("name", list(WRITERS))
def test_a_passing_gate_is_ready_to_promote(spc, dirs, name):
    write, positions = WRITERS[name]
    write(dirs, True, 9)
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), name)
    assert c["status"] == spc.READY and c["ready_positions"] == positions
    assert c["weeks_scored"] == 9
    for p in positions:
        v = c["positions"][p]
        assert v["ready_to_promote"] and v["failing_checks"] == [] and v["weeks_scored"] == 9
        assert v["appearances"] and all(v["checks"].values())


@pytest.mark.parametrize("name", list(WRITERS))
def test_a_failing_gate_lists_the_failing_checks(spc, dirs, name):
    write, positions = WRITERS[name]
    write(dirs, False, 10)
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), name)
    assert c["status"] == spc.FAILS and c["ready_positions"] == []
    for p in positions:
        assert c["positions"][p]["status"] == spc.FAILS and c["positions"][p]["failing_checks"]


@pytest.mark.parametrize("name", list(WRITERS))
def test_too_few_weeks_is_accumulating_even_if_every_other_check_passes(spc, dirs, name):
    write, positions = WRITERS[name]
    write(dirs, True, 5)
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), name)
    assert c["status"] == spc.ACCUMULATING and c["ready_positions"] == []
    for p in c["positions"].values():
        assert not p["ready_to_promote"] and any("min_weeks (5 of 8)" in f for f in p["failing_checks"])


def test_the_distribution_ledger_gets_the_week_floor_and_qb_stays_restricted(spc, dirs):
    write_dist(dirs, True, 6)               # within bounds on 6 weeks: the ledger's own gate has no week count
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), "ifpd-0.1.0")
    assert c["positions"]["RB"]["gate_status"] == "within_bounds" and c["status"] == spc.ACCUMULATING
    write_dist(dirs, True, 9)
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), "ifpd-0.1.0")
    assert c["ready_positions"] == ["RB", "WR"]
    assert c["positions"]["QB"]["failing_checks"] == ["position_not_restricted"]


@pytest.mark.parametrize("ship,ready", [("ifp-irs-p1", ["ifp-irs-p1"]), ("ifp-irb-p1", ["ifp-irb-p1"]),
                                        ("both", ["ifp-irs-p1", "ifp-irb-p1"])])
def test_injury_return_only_the_jointly_selected_implementation_is_ready(spc, dirs, ship, ready):
    """Both candidates pass: the shadow's joint rule (both.choice) selects one implementation, and only it is ready
    and queued; the other passing single is not (its `selected_by_joint_rule` check fails)."""
    write_injury_return(dirs, True, 11, choice={"ship": ship, "added": []})
    s = spc.run_check(_args(spc, dirs))
    assert sorted(r["candidate"] for r in s["ready"]) == sorted(ready)
    queue = json.loads((dirs["logs"] / "promotion-queue.json").read_text())["entries"]
    assert sorted(k for k in queue if k.startswith("ifp-ir")) == sorted(ready)
    for name in {"ifp-irs-p1", "ifp-irb-p1"} - set(ready):
        c = _cand(s, name)
        assert c["status"] == spc.FAILS and c["ready_positions"] == []
        assert c["positions"]["RB"]["failing_checks"] == ["selected_by_joint_rule"]


def test_signal_shadows_are_judged_per_candidate(spc, dirs):
    write_signal(dirs, True, 9, only="ifp-awd-p1")
    s = spc.run_check(_args(spc, dirs, "--dry-run"))
    assert [r["candidate"] for r in s["ready"]] == ["ifp-awd-p1"]
    assert _cand(s, "ifp-fr-p1")["positions"]["RB"]["failing_checks"] == ["mae_vs_champion"]


def test_no_result_is_no_evidence_and_nothing_is_ready(spc, dirs):
    s = spc.run_check(_args(spc, dirs, "--dry-run"))
    assert s["counts"][spc.NO_EVIDENCE] == len(spc.SHADOWS) and s["ready"] == [] and s["lines"] == []
    assert not dirs["logs"].exists()


def test_a_gate_without_checks_is_never_ready(spc, dirs):
    _put(dirs["ifp"] / "research" / "participation_cal_shadow" / "scores.json",
         {"weeks": 12, "n": 0, "gate": {"status": "meets_gate"}})
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), "ipm-cal-0.1.0")
    assert c["status"] != spc.READY and not c["positions"]["QB"]["ready_to_promote"]


def test_an_unreadable_result_is_a_warning_and_exit_3(spc, dirs, capsys):
    p = dirs["ifp"] / "research" / "signal_shadows" / "scores.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    code = spc.main(["check", "--dry-run", "--ifp-dir", str(dirs["ifp"]), "--dst-dir", str(dirs["dst"]),
                     "--logs-dir", str(dirs["logs"])])
    out = json.loads(capsys.readouterr().out)
    assert code == spc.EXIT_WARNINGS and _cand(out, "ifp-fr-p1")["status"] == spc.NO_EVIDENCE
    assert any("could not read" in w for w in out["warnings"])


def test_an_unexpected_shape_is_a_warning_not_a_pass(spc, dirs):
    _put(dirs["ifp"] / "research" / "qb_scenario_shadow" / "prospective.json", {"gate": ["not", "a", "dict"]})
    c = _cand(spc.run_check(_args(spc, dirs, "--dry-run")), "ifp-ctx-p2q")
    assert c["status"] == spc.NO_EVIDENCE and c["warnings"]


# ----------------------------------------------------------------------------- the signal: log line, queue, summary


def test_ready_writes_the_line_the_queue_and_the_summary(spc, dirs, capsys):
    write_signal(dirs, True, 9, only="ifp-fr-p1")
    write_ctx(dirs, False, 9)
    code = spc.main(["check", "--ifp-dir", str(dirs["ifp"]), "--dst-dir", str(dirs["dst"]),
                     "--logs-dir", str(dirs["logs"])])
    err = capsys.readouterr().err
    assert code == spc.EXIT_OK
    assert "PROMOTION READY: ifp-fr-p1 RB WR" in err
    log = (dirs["logs"] / "shadow-promotion.log").read_text()
    assert log.count("PROMOTION READY: ifp-fr-p1 RB WR") == 1 and "plan ifp-fr-p1" in log
    q = json.loads((dirs["logs"] / "promotion-queue.json").read_text())
    e = q["entries"]["ifp-fr-p1"]
    assert q["schema"] == spc.QUEUE_SCHEMA and list(q["entries"]) == ["ifp-fr-p1"]
    assert (e["status"], e["positions"], e["target"], e["weeks_scored"]) == ("ready", ["RB", "WR"], "ours", 9)
    assert e["next_version"] == spc.next_version(spc.production_version())
    latest = json.loads((dirs["logs"] / "shadow-promotion-latest.json").read_text())
    assert latest["ready"] == [{"candidate": "ifp-fr-p1", "positions": ["RB", "WR"], "target": "ours"}]


def test_the_queue_keeps_first_ready_and_marks_a_candidate_that_stops_passing(spc, dirs):
    write_dst(dirs, True, 8)
    spc.run_check(_args(spc, dirs), spc.datetime.fromisoformat("2026-12-01T12:00:00+00:00"))
    spc.run_check(_args(spc, dirs), spc.datetime.fromisoformat("2026-12-08T12:00:00+00:00"))
    e = json.loads((dirs["logs"] / "promotion-queue.json").read_text())["entries"]["dfp-r1"]
    assert e["first_ready_at"].startswith("2026-12-01") and e["last_ready_at"].startswith("2026-12-08")
    assert e["next_version"] is None and e["target"] == "dst"
    write_dst(dirs, False, 9)
    spc.run_check(_args(spc, dirs), spc.datetime.fromisoformat("2026-12-15T12:00:00+00:00"))
    e = json.loads((dirs["logs"] / "promotion-queue.json").read_text())["entries"]["dfp-r1"]
    assert e["status"] == "no_longer_ready" and e["first_ready_at"].startswith("2026-12-01")
    assert (dirs["logs"] / "shadow-promotion.log").read_text().splitlines()[-1].endswith(
        "no candidate ready (0 accumulating, 1 failing, 10 without a scored week)")


def test_a_dry_run_or_a_simulated_clock_writes_nothing(spc, dirs):
    write_ctx(dirs, True, 9)
    assert spc.run_check(_args(spc, dirs, "--dry-run"))["ready"]
    assert spc.run_check(_args(spc, dirs, "--now", NOW))["dry_run"] is True
    assert not dirs["logs"].exists()


def test_a_promoted_candidate_is_confirmed_or_flagged_for_review(spc, dirs, monkeypatch):
    monkeypatch.setitem(spc.PROMOTED, "ifp-awd-p1", {"version": "ifp-2.2.0", "positions": ["RB", "WR", "TE"]})
    write_signal(dirs, True, 9, only="ifp-awd-p1")
    s = spc.run_check(_args(spc, dirs))
    assert s["ready"] == [] and s["confirmed"] == [{"candidate": "ifp-awd-p1", "positions": ["RB", "TE", "WR"]}]
    assert s["lines"] == ["PROMOTION CONFIRMED: ifp-awd-p1 RB TE WR (already in ifp-2.2.0; 9 weeks scored)"]
    assert "ifp-awd-p1" not in json.loads((dirs["logs"] / "promotion-queue.json").read_text())["entries"]
    write_signal(dirs, False, 10)
    s = spc.run_check(_args(spc, dirs, "--dry-run"))
    assert s["lines"] == ["PROMOTION REVIEW: ifp-awd-p1 RB TE WR fails its gate but is in ifp-2.2.0 "
                          "(10 weeks scored)"]


def test_a_queued_candidate_listed_in_promoted_is_marked_promoted(spc, dirs, monkeypatch):
    write_iwh(dirs, True, 9)
    spc.run_check(_args(spc, dirs))
    monkeypatch.setitem(spc.PROMOTED, "ifp-iwh-0.1.1", {"version": "ifp-2.2.0", "positions": ["RB", "WR", "TE"]})
    spc.run_check(_args(spc, dirs))
    e = json.loads((dirs["logs"] / "promotion-queue.json").read_text())["entries"]["ifp-iwh-0.1.1"]
    assert e["status"] == "promoted" and e["promoted"]["version"] == "ifp-2.2.0"


# ----------------------------------------------------------------------------- versions and the promotion brief


def test_next_version_and_the_production_version(spc):
    assert spc.next_version("ifp-2.1.0") == "ifp-2.2.0" and spc.next_version("ifp-2.9.3") == "ifp-2.10.0"
    assert spc.next_version(None) is None and spc.next_version("dfp-r1") is None
    from app.analytics import projection_source as ps
    assert spc.production_version() == ps.MODEL_VERSION


def test_plan_gives_the_steps_for_a_ready_candidate(spc, dirs):
    write_signal(dirs, True, 9, only="ifp-dct-p1")
    plan = spc.cmd_plan(_args(spc, dirs, "ifp-dct-p1", command="plan"))
    nxt = spc.next_version(spc.production_version())
    assert plan["status"] == spc.READY and plan["ready_positions"] == ["QB"] and plan["next_version"] == nxt
    assert plan["runbook"] == "docs/shadow_promotion.md" and "note" not in plan
    assert any(f"Build {nxt}" in s and "on QB only" in s for s in plan["steps"])
    assert any('PROMOTED' in s and f'"version": "{nxt}"' in s for s in plan["steps"])
    other = spc.cmd_plan(_args(spc, dirs, "dfp-r1", command="plan"))
    assert other["status"] == spc.NO_EVIDENCE and other["next_version"] is None and "note" in other
    with pytest.raises(SystemExit):
        spc.cmd_plan(_args(spc, dirs, "ifp-nope", command="plan"))


def test_the_runbook_and_every_research_doc_exist(spc):
    root = SCRIPTS.parent
    assert (root / "docs" / "shadow_promotion.md").exists()
    for s in spc.SHADOWS:
        if s["shadow"] == "signal_shadows.py":          # added with its doc by the signal-shadows pull request
            continue
        assert (SCRIPTS / s["shadow"]).exists() and (root / s["doc"]).exists(), s["candidate"]
