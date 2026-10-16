"""Approved production model registry (app.analytics.model_registry): the committed manifest keeps
ifp-1.0.0 as production, every promotion check fails closed, a fixture approval is accepted only in
isolation, rollback is planned (never applied), and nothing in the hosted app reads the registry."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

from app.analytics import model_registry as reg
from app.analytics import projection_source as ps
from app.analytics.independent_projections import MODEL_VERSION

from test_independent_projections import live  # noqa: F401  (fixture)
from test_projection_source import stored  # noqa: F401  (fixture)

FP = "e165b59686110c7d"
CAND = "ifp-9.0.0"
AT = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


# ----------------------------------------------------------------------------- fixtures


def _write(root: pathlib.Path, rel: str, obj) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1))
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _prospective(weeks: int = 8) -> dict:
    wk = [[2026, w] for w in range(1, weeks + 1)]
    pos = {"n": 500, "metrics": {"champion": {"mae": 4.2, "bias": 0.1}, "challenger": {"mae": 4.0, "bias": 0.12}},
           "challenger_minus_champion_mae": {"diff": -0.2, "ci95": [-0.3, -0.1]},
           "interval80_coverage": {"champion": 0.79, "challenger": 0.80},
           "starters_pool": {"n": 200, "mae": {"champion": 5.0, "challenger": 4.8}}}
    return {"pregame": {"kind": "pregame", "weeks": wk,
                        "finality": {f"{s}-{w:02d}": {"fully_final": True} for s, w in wk},
                        "positions": {p: deepcopy(pos) for p in reg.POSITIONS}},
            "retrospective_not_evidence": {"kind": "retrospective"}}


def _heldout() -> dict:
    pos = {"common_support_n": 900, "relative_mae_improvement": 0.05,
           "primary_minus_best_baseline_mae": {"ci95": [-0.4, -0.1]}}
    return {"positions": {p: dict(pos) for p in reg.POSITIONS},
            "espn_comparison": {"by_position": {p: {"n": 350, "primary": {"mae": 4.0}, "espn": {"mae": 4.1}}
                                                for p in reg.POSITIONS}}}


@pytest.fixture
def env(tmp_path):
    """A model directory with a pinned champion and a fully evidenced candidate; the committed
    manifest with the champion pinned (old) and a proposed promotion with an owner approval (new)."""
    root = tmp_path / "models"
    champ_sha = _write(root, "independent_projections_ifp-1.0.0.json",
                       {"schema": "independent-projections-artifact/v1", "version": MODEL_VERSION,
                        "scoring_fingerprint": FP})
    cand_sha = _write(root, f"candidates/{CAND}.json", {"version": CAND, "scoring_fingerprint": FP})
    pro_sha = _write(root, "reports/prospective.json", _prospective())
    held_sha = _write(root, "reports/heldout.json", _heldout())
    ledger = root / "weekly" / "ledger" / "s2026w08"
    ledger.mkdir(parents=True)
    (ledger / "pregame-run.json").write_text('{"run_id": "frozen"}')

    old = reg.load_manifest()
    ch = reg.entry(old, MODEL_VERSION)
    ch["artifact"]["sha256"] = champ_sha
    ch["training_cutoff"] = {"season": 2026, "week": 0}
    new = deepcopy(old)
    reg.entry(new, MODEL_VERSION)["state"] = reg.RETIRED
    new["models"].append({
        "family": "ifp", "version": CAND, "state": reg.APPROVED,
        "artifact": {"path": f"candidates/{CAND}.json", "sha256": cand_sha},
        "scoring_fingerprint": FP, "training_cutoff": {"season": 2026, "week": 16},
        "approval": {"kind": reg.OWNER_APPROVAL, "approved_by": "fixture-owner", "approved_at": AT.isoformat(),
                     "start_sit_ready": False,
                     "evidence_sha256": {reg.PROSPECTIVE: pro_sha, reg.HELDOUT: held_sha}},
        "validation_reports": [{"kind": reg.PROSPECTIVE, "path": "reports/prospective.json", "sha256": pro_sha},
                               {"kind": reg.HELDOUT, "path": "reports/heldout.json", "sha256": held_sha}],
        "rollback_to": MODEL_VERSION, "reason": "isolated test fixture"})
    new["production"] = {"family": "ifp", "version": CAND}
    new["history"].append({"at": AT.isoformat(), "event": "promotion", "from": MODEL_VERSION, "production": CAND,
                           "by": "fixture-owner", "reason": "isolated test fixture"})
    return root, old, new


def _gate(env, new=None, fp=FP):
    root, old, prop = env
    return reg.evaluate_promotion(old, prop if new is None else new, model_root=root, live_scoring_fp=fp)


def _results(rep: dict) -> dict:
    return {c["name"]: c["result"] for c in rep["checks"]}


def _rewrite(root, rel, mutate) -> str:
    """Change an evidence file and return its new hash (for fixtures that re-pin it)."""
    p = root / rel
    obj = json.loads(p.read_text())
    mutate(obj)
    return _write(root, rel, obj)


def _repin(new, kind, sha):
    e = reg.entry(new, CAND)
    next(r for r in e["validation_reports"] if r["kind"] == kind)["sha256"] = sha
    e["approval"]["evidence_sha256"][kind] = sha


# ----------------------------------------------------------------------------- default production


def test_default_approved_champion_is_ifp_1():
    m = reg.load_manifest()
    p = reg.production(m)
    assert p["version"] == MODEL_VERSION == ps.MODEL_VERSION == "ifp-1.0.0"
    assert p["state"] == reg.APPROVED and p["approval"]["kind"] == reg.INCUMBENT
    assert p["scoring_fingerprint"] == FP
    # no owner approval anywhere, and nothing else is approved
    assert [e["version"] for e in m["models"] if e["state"] == reg.APPROVED] == [MODEL_VERSION]
    assert not [e for e in m["models"] if (e.get("approval") or {}).get("kind") == reg.OWNER_APPROVAL]
    assert reg.entry(m, "ifp-2.0.0")["state"] == reg.SHADOW
    assert [h["event"] for h in m["history"]] == ["registry_initialized"]


def test_dry_run_report_on_the_committed_manifest(tmp_path):
    m = reg.load_manifest()
    rep = reg.dry_run_report(m, model_root=tmp_path, live_scoring_fp=FP, consumer_version=ps.MODEL_VERSION)
    assert rep["production"]["version"] == MODEL_VERSION and rep["production"]["consumers_match"]
    assert rep["production"]["artifact_pinned"] is False and rep["production"]["note"]
    assert {c["version"] for c in rep["candidates"]} == {"ifp-2.0.0", "ifp-hgb-0.1.0"}
    assert not any(c["activatable"] or c["eligible_for_review"] or c["activates"] for c in rep["candidates"])


def test_incumbent_record_cannot_be_reused_by_another_model():
    m = reg.load_manifest()
    bad = deepcopy(m)
    reg.entry(bad, MODEL_VERSION)["state"] = reg.RETIRED
    e = reg.entry(bad, "ifp-2.0.0")
    e["state"], e["approval"] = reg.APPROVED, {"kind": reg.INCUMBENT}
    bad["production"]["version"] = "ifp-2.0.0"
    assert any("incumbent" in p for p in reg.validate_manifest(bad))


@pytest.mark.parametrize("mutate", [
    lambda m: m.pop("production"),
    lambda m: m["models"][0].update(state="approved"),                          # unknown state
    lambda m: m["models"][1].update(state=reg.APPROVED),                        # two approved
    lambda m: m["models"][0].pop("reason"),
    lambda m: m["models"][0]["artifact"].update(sha256="abc"),                  # not a full digest
    lambda m: m.update(history=[]),
    lambda m: m.update(schema="model-registry/v0"),
])
def test_malformed_manifest_fails_closed(tmp_path, mutate):
    m = reg.load_manifest()
    mutate(m)
    assert reg.validate_manifest(m)
    p = tmp_path / "m.json"
    p.write_text(json.dumps(m))
    with pytest.raises(reg.RegistryError):
        reg.load_manifest(p)
    with pytest.raises(reg.RegistryError):
        reg.production(m)


# ----------------------------------------------------------------------------- the future approval (isolated)


def test_explicit_future_approval_fixture_is_accepted_in_isolation(env):
    rep = _gate(env)
    assert rep["activatable"], rep["blocking"]
    assert rep["activates"] is False and rep["dry_run"] is True
    r = _results(rep)
    for pos in reg.POSITIONS:
        for name in ("mae_vs_champion", "starters_pool_mae", "bias_guard", "interval_coverage", "prospective_weeks",
                     "matched_appearances", "production_baseline"):
            assert r[f"{pos}:{name}"] == reg.PASS, (pos, name)
    assert r["espn_rule"] == reg.NOT_APPLICABLE and r["human_approval"] == reg.PASS
    # the committed manifest is untouched by evaluating a proposal
    assert reg.production(reg.load_manifest())["version"] == MODEL_VERSION


def test_espn_rule_required_when_start_sit_readiness_is_claimed(env):
    root, old, new = env
    reg.entry(new, CAND)["approval"]["start_sit_ready"] = True
    assert _gate(env, new)["activatable"]
    _repin(new, reg.HELDOUT, _rewrite(root, "reports/heldout.json",
                                      lambda o: o["espn_comparison"]["by_position"]["TE"].update(n=10)))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["TE:espn_rule"] == reg.INSUFFICIENT


# ----------------------------------------------------------------------------- rejections


def test_unapproved_model_rejected(env):
    root, old, new = env
    # eligible for review on evidence, but no approval: never activatable on the current manifest
    cur = deepcopy(old)
    e = deepcopy(reg.entry(new, CAND))
    e["state"], e["approval"] = reg.ELIGIBLE, None
    cur["models"].append(e)
    rev = reg.review_candidate(cur, CAND, model_root=root, live_scoring_fp=FP)
    assert rev["eligible_for_review"] and not rev["activatable"]
    # a proposal whose approval lacks an approver / timestamp / record
    for change in ({"approved_by": ""}, {"approved_at": "2027-01-05"}, {"kind": reg.INCUMBENT}):
        bad = deepcopy(new)
        reg.entry(bad, CAND)["approval"].update(change)
        assert not _gate(env, bad)["activatable"]
    bad = deepcopy(new)
    reg.entry(bad, CAND)["approval"] = None
    assert not _gate(env, bad)["activatable"]


def test_approval_must_cite_the_verified_evidence(env):
    _, _, new = env
    reg.entry(new, CAND)["approval"]["evidence_sha256"][reg.PROSPECTIVE] = "0" * 64
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["human_approval"] == reg.FAIL


def test_shadow_model_rejected(env):
    root, old, new = env
    rev = reg.review_candidate(old, "ifp-2.0.0", model_root=root, live_scoring_fp=FP)
    assert not rev["activatable"] and not rev["eligible_for_review"]
    assert _results(rev)["lifecycle_state"] == reg.FAIL
    bad = deepcopy(new)
    reg.entry(bad, CAND)["state"] = reg.SHADOW               # production pointer to a shadow model
    assert not _gate(env, bad)["activatable"]


def test_missing_artifact_rejected(env):
    root, _, _ = env
    (root / "candidates" / f"{CAND}.json").unlink()
    rep = _gate(env)
    assert not rep["activatable"] and _results(rep)["artifact_integrity"] == reg.FAIL


def test_unpinned_or_escaping_artifact_rejected(env):
    _, _, new = env
    bad = deepcopy(new)
    reg.entry(bad, CAND)["artifact"]["sha256"] = None
    assert _results(_gate(env, bad))["artifact_integrity"] == reg.INSUFFICIENT
    bad = deepcopy(new)
    reg.entry(bad, CAND)["artifact"]["path"] = "../../etc/passwd"
    assert _results(_gate(env, bad))["artifact_integrity"] == reg.FAIL


def test_tampered_artifact_rejected(env):
    root, _, _ = env
    p = root / "candidates" / f"{CAND}.json"
    p.write_bytes(p.read_bytes() + b" ")                    # one byte changes the digest
    rep = _gate(env)
    assert not rep["activatable"] and _results(rep)["artifact_integrity"] == reg.FAIL


def test_latest_artifact_is_not_approved(env):
    """A newer file next to the approved one changes nothing; replacing the approved one is rejected."""
    root, _, _ = env
    _write(root, f"candidates/{CAND}-newer.json", {"version": CAND, "scoring_fingerprint": FP})
    assert _gate(env)["activatable"]
    _write(root, f"candidates/{CAND}.json", {"version": CAND, "scoring_fingerprint": FP, "retrained": True})
    assert not _gate(env)["activatable"]


def test_wrong_scoring_fingerprint_rejected(env):
    root, _, new = env
    assert _results(_gate(env, fp="0000000000000000"))["scoring_fingerprint"] == reg.FAIL
    assert _results(_gate(env, fp=None))["scoring_fingerprint"] == reg.INSUFFICIENT
    # the artifact itself trained under other scoring, though the manifest claims the league's
    bad = deepcopy(new)
    reg.entry(bad, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json",
                                                         {"version": CAND, "scoring_fingerprint": "other"})
    rep = _gate(env, bad)
    assert not rep["activatable"] and _results(rep)["scoring_fingerprint"] == reg.FAIL


@pytest.mark.parametrize("mutate,check,result", [
    (lambda o: o.update(pregame=_prospective(5)["pregame"]), "QB:prospective_weeks", reg.INSUFFICIENT),
    (lambda o: o["pregame"]["finality"]["2026-08"].update(fully_final=False), "finalized_evaluation", reg.FAIL),
    (lambda o: o["pregame"].update(preliminary={"weeks": [[2026, 8]]}), "finalized_evaluation", reg.FAIL),
    (lambda o: o["pregame"].update(kind="retrospective"), "pregame_provenance", reg.FAIL),
    (lambda o: o["pregame"]["positions"]["WR"].update(n=120), "WR:matched_appearances", reg.INSUFFICIENT),
    (lambda o: o["pregame"]["positions"].pop("TE"), "TE:position_report", reg.INSUFFICIENT),
    (lambda o: o["pregame"]["positions"]["QB"]["challenger_minus_champion_mae"].update(ci95=[-0.3, 0.05]),
     "QB:mae_vs_champion", reg.FAIL),
    (lambda o: o["pregame"]["positions"]["RB"]["starters_pool"]["mae"].update(challenger=5.5),
     "RB:starters_pool_mae", reg.FAIL),
    (lambda o: o["pregame"]["positions"]["RB"]["metrics"]["challenger"].update(bias=0.9), "RB:bias_guard", reg.FAIL),
    (lambda o: o["pregame"]["positions"]["WR"]["interval80_coverage"].update(challenger=0.6),
     "WR:interval_coverage", reg.FAIL),
])
def test_incomplete_or_failing_prospective_evidence_rejected(env, mutate, check, result):
    root, _, new = env
    _repin(new, reg.PROSPECTIVE, _rewrite(root, "reports/prospective.json", mutate))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)[check] == result


@pytest.mark.parametrize("mutate,check", [
    (lambda o: o["pregame"]["positions"]["RB"].pop("starters_pool"), "RB:starters_pool_mae"),
    (lambda o: o["pregame"]["positions"]["QB"].pop("interval80_coverage"), "QB:interval_coverage"),
    (lambda o: o["pregame"]["positions"]["TE"]["metrics"]["champion"].pop("bias"), "TE:bias_guard"),
    (lambda o: o["pregame"]["positions"]["WR"].pop("challenger_minus_champion_mae"), "WR:mae_vs_champion"),
])
def test_missing_validation_metric_rejected(env, mutate, check):
    root, _, new = env
    _repin(new, reg.PROSPECTIVE, _rewrite(root, "reports/prospective.json", mutate))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)[check] == reg.INSUFFICIENT


@pytest.mark.parametrize("mutate", [
    lambda o: o["positions"]["QB"].pop("relative_mae_improvement"),
    lambda o: o["positions"]["RB"].update(relative_mae_improvement=0.01),
    lambda o: o["positions"]["WR"].update(common_support_n=100),
    lambda o: o["positions"].pop("TE"),
])
def test_production_baseline_gate_still_applies(env, mutate):
    root, _, new = env
    _repin(new, reg.HELDOUT, _rewrite(root, "reports/heldout.json", mutate))
    rep = _gate(env, new)
    assert not rep["activatable"]
    assert any(c["name"].endswith("production_baseline") and c["result"] != reg.PASS for c in rep["checks"])


def test_historical_report_cannot_replace_prospective_confirmation(env):
    root, _, new = env
    e = reg.entry(new, CAND)
    for r in e["validation_reports"]:
        if r["kind"] == reg.PROSPECTIVE:
            r["kind"] = reg.HISTORICAL                  # same strong numbers, but a backtest
    rep = _gate(env, new)
    assert not rep["activatable"]
    c = next(c for c in rep["checks"] if c["name"] == f"{reg.PROSPECTIVE}_report")
    assert c["result"] == reg.INSUFFICIENT and "cannot replace prospective" in c["explanation"]


def test_missing_or_tampered_report_rejected(env):
    root, _, _ = env
    p = root / "reports" / "heldout.json"
    p.write_text(p.read_text().replace("0.05", "0.50"))
    assert _results(_gate(env))[f"{reg.HELDOUT}_report_integrity"] == reg.FAIL
    p.unlink()
    assert not _gate(env)["activatable"]


def test_promotion_requires_a_restorable_rollback_target(env):
    root, old, new = env
    unpinned = deepcopy(old)
    reg.entry(unpinned, MODEL_VERSION)["artifact"]["sha256"] = None    # the committed state today
    rep = reg.evaluate_promotion(unpinned, new, model_root=root, live_scoring_fp=FP)
    assert rep["activatable"]                       # pinning in the proposal itself is allowed ...
    (root / "independent_projections_ifp-1.0.0.json").unlink()
    rep = _gate(env)                                 # ... but the pinned bytes must exist
    assert not rep["activatable"] and _results(rep)["rollback_artifact_integrity"] == reg.FAIL
    bad = deepcopy(new)
    reg.entry(bad, CAND)["rollback_to"] = "ifp-2.0.0"
    assert _results(_gate(env, bad))["rollback_reference"] == reg.FAIL


@pytest.mark.parametrize("mutate,needle", [
    (lambda n: n["models"].pop(1), "removed"),
    (lambda n: n["history"].__setitem__(0, {**n["history"][0], "reason": "edited"}), "append-only"),
    (lambda n: n["history"].pop(), "history event"),
    (lambda n: reg.entry(n, MODEL_VERSION)["artifact"].update(sha256="1" * 64), "pinned artifact"),
    (lambda n: reg.entry(n, MODEL_VERSION).update(scoring_fingerprint="x"), "scoring fingerprint"),
])
def test_transition_must_preserve_history_and_artifacts(env, mutate, needle):
    _, old, new = env
    bad = deepcopy(new)
    mutate(bad)
    problems = reg.verify_transition(old, bad)
    assert any(needle in p for p in problems), problems
    assert not _gate(env, bad)["activatable"]


# ----------------------------------------------------------------------------- rollback


def _tree(root: pathlib.Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_rollback_fixture_restores_previous_model_and_keeps_everything(env, tmp_path):
    root, _, promoted = env
    before = _tree(root)
    rolled = reg.plan_rollback(promoted, reason="prospective MAE regressed", by="fixture-owner", at=AT,
                               model_root=root, live_scoring_fp=FP)
    assert rolled["production"]["version"] == MODEL_VERSION
    restored, retired = reg.entry(rolled, MODEL_VERSION), reg.entry(rolled, CAND)
    assert restored["state"] == reg.APPROVED and restored["approval"]["kind"] == reg.ROLLBACK_RESTORE
    assert restored["approval"]["previous_approval"]["kind"] == reg.INCUMBENT
    assert retired["state"] == reg.RETIRED and retired["retired"]["reason"] == "prospective MAE regressed"
    assert retired["artifact"] == reg.entry(promoted, CAND)["artifact"]          # artifact reference kept
    assert rolled["history"][:-1] == promoted["history"]
    assert rolled["history"][-1] == {"at": AT.isoformat(), "event": "rollback", "from": CAND,
                                     "production": MODEL_VERSION, "by": "fixture-owner",
                                     "reason": "prospective MAE regressed"}
    assert reg.verify_transition(promoted, rolled) == []
    assert _tree(root) == before                     # artifacts, reports and ledgers untouched
    assert reg.production(promoted)["version"] == CAND   # pure: the input manifest is unchanged
    out = reg.write_manifest(rolled, tmp_path / "proposal.json")
    assert json.loads(out.read_text()) == rolled
    with pytest.raises(reg.RegistryError):
        reg.write_manifest(rolled, out)               # write-once
    with pytest.raises(reg.RegistryError):
        reg.write_manifest(rolled, reg.MANIFEST_PATH)  # never the committed registry


def test_rollback_refuses_incompatible_scoring_or_tampered_target(env):
    root, _, promoted = env
    kw = {"reason": "r", "by": "o", "at": AT, "model_root": root}
    with pytest.raises(reg.RegistryError, match="scoring"):
        reg.plan_rollback(promoted, live_scoring_fp="0000000000000000", **kw)
    with pytest.raises(reg.RegistryError):
        reg.plan_rollback(promoted, live_scoring_fp=FP, **{**kw, "reason": " "})
    with pytest.raises(reg.RegistryError):
        reg.plan_rollback(promoted, live_scoring_fp=FP, **{**kw, "at": AT.replace(tzinfo=None)})
    p = root / "independent_projections_ifp-1.0.0.json"
    p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(reg.RegistryError, match="changed or replaced"):
        reg.plan_rollback(promoted, live_scoring_fp=FP, **kw)
    with pytest.raises(reg.RegistryError, match="no registered rollback target"):
        reg.plan_rollback(reg.load_manifest(), live_scoring_fp=FP, **kw)


# ----------------------------------------------------------------------------- production unchanged


def test_same_production_projections_before_and_after_registry(stored, env):  # noqa: F811
    before = {s: ps.weekly(stored, 2026, 6, s) for s in ps.SOURCES}
    root, old, new = env
    reg.dry_run_report(old, model_root=root, live_scoring_fp=FP, consumer_version=ps.MODEL_VERSION)
    assert _gate(env)["activatable"]                 # even an accepted fixture approval changes nothing
    reg.plan_rollback(new, reason="r", by="o", at=AT, model_root=root, live_scoring_fp=FP)
    after = {s: ps.weekly(stored, 2026, 6, s) for s in ps.SOURCES}
    assert after == before
    assert before["independent"]["run"]["model_version"] == MODEL_VERSION == reg.production(reg.load_manifest())["version"]


def test_hosted_app_never_reads_the_registry_or_research_packages():
    for path in APP.rglob("*.py"):
        if path.name != "model_registry.py":
            assert "model_registry" not in path.read_text(), path
    code = ("import sys, app.main; "
            "bad = [m for m in ('app.analytics.model_registry', 'sklearn', 'scipy') if m in sys.modules]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True)
    # reading and verifying a manifest needs only the standard library
    code = ("import sys; sys.modules['polars'] = None; sys.modules['numpy'] = None; "
            "from app.analytics import model_registry as r; print(r.production(r.load_manifest())['version'])")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True, capture_output=True, text=True)
    assert out.stdout.strip() == MODEL_VERSION
    runtime = (BACKEND / "pyproject.toml").read_text().split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "scikit" not in runtime


def test_cli_status_is_read_only(tmp_path):
    import importlib.util

    scripts = BACKEND.parent / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("model_registry_cli", scripts / "model_registry.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    committed = reg.MANIFEST_PATH.read_bytes()
    out = tmp_path / "report.json"
    assert cli.main(["status", "--models", str(tmp_path), "--live-db", str(tmp_path / "none.duckdb"),
                     "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["production"]["version"] == MODEL_VERSION and rep["dry_run"] is True
    assert cli.main(["status", "--models", str(tmp_path), "--live-db", str(tmp_path / "none.duckdb"),
                     "--out", str(out)]) == 2                       # write-once
    assert cli.main(["plan-rollback", "--reason", "r", "--by", "o", "--models", str(tmp_path),
                     "--live-db", str(tmp_path / "none.duckdb"), "--out", str(tmp_path / "p.json")]) == 2
    assert reg.MANIFEST_PATH.read_bytes() == committed


@pytest.mark.parametrize("kind,rel,mutate", [
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o["pregame"].update(weeks=["2026-01"])),
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o["pregame"]["positions"]["QB"].update(metrics=[1])),
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o["pregame"]["positions"]["QB"]
     ["challenger_minus_champion_mae"].update(ci95=["-1", "-0.5"])),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o["positions"]["QB"].update(common_support_n="900")),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o.update(positions=[])),
])
def test_malformed_report_fails_closed_without_crashing(env, kind, rel, mutate):
    root, _, new = env
    _repin(new, kind, _rewrite(root, rel, mutate))
    rep = _gate(env, new)
    assert not rep["activatable"] and rep["blocking"]
