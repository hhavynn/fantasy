"""Approved production model registry (app.analytics.model_registry): the committed manifest records the
owner's promotion of ifp-2.0.0 over ifp-1.0.0 as a valid transition, every promotion check fails closed, a
fixture approval is accepted only in isolation, rollback is planned (never applied), and nothing in the hosted
app reads the registry. Promotion fixtures start from the pre-promotion manifest (ifp-1.0.0 production,
tests/fixtures/model_registry_pre_ifp2.json)."""

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
from app.analytics.independent_projections import ARTIFACT_SCHEMA, MODEL_VERSION

from test_independent_projections import live  # noqa: F401  (fixture)
from test_projection_source import stored  # noqa: F401  (fixture)

FP = "e165b59686110c7d"
CAND = "ifp-9.0.0"
AT = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
PRE_IFP2 = pathlib.Path(__file__).resolve().parent / "fixtures" / "model_registry_pre_ifp2.json"


def _pre() -> dict:
    """The manifest before the ifp-2.0.0 promotion: ifp-1.0.0 production under its incumbent record."""
    return reg.load_manifest(PRE_IFP2)


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
    return {"subject": {"challenger_version": CAND, "champion_version": MODEL_VERSION, "scoring_fingerprint": FP},
            "pregame": {"kind": "pregame", "weeks": wk,
                        "finality": {f"{s}-{w:02d}": {"fully_final": True} for s, w in wk},
                        "positions": {p: deepcopy(pos) for p in reg.POSITIONS}},
            "retrospective_not_evidence": {"kind": "retrospective"}}


def _heldout() -> dict:
    pos = {"common_support_n": 900, "relative_mae_improvement": 0.05,
           "primary_minus_best_baseline_mae": {"ci95": [-0.4, -0.1]}}
    return {"model_version": CAND, "scoring_fingerprint": FP, "positions": {p: dict(pos) for p in reg.POSITIONS},
            "espn_comparison": {"by_position": {p: {"n": 350, "primary": {"mae": 4.0}, "espn": {"mae": 4.1}}
                                                for p in reg.POSITIONS}}}


def _ifp_artifact(version: str, fp: str = FP, config: dict | None = None, evaluation: dict | None = None) -> dict:
    """A minimal artifact that the production loader (Artifact.from_dict) accepts."""
    model = {"features": ["x"], "columns": ["x"], "means": [1.0], "stds": [2.0], "coef": [0.5], "intercept": 3.0,
             "lam": 1.0, "n_train": 500, "model": "C", "train_window": [202501, 202616]}
    return {"schema": ARTIFACT_SCHEMA, "version": version, "created_at": AT.isoformat(), "scoring_fingerprint": fp,
            "scoring": {"fingerprint": fp}, "config": config or {},
            "models": {p: {**model, "position": p} for p in reg.POSITIONS}, "intervals": {},
            "status": {p: {"status": "experimental"} for p in reg.POSITIONS}, "evaluation": evaluation or {}}


def _cand_artifact(root: pathlib.Path, **extra) -> dict:
    """The candidate artifact as fit_final would write it, citing the exact held-out report."""
    held = _heldout()["positions"]
    sha = hashlib.sha256((root / "reports" / "heldout.json").read_bytes()).hexdigest()
    return _ifp_artifact(CAND, config={"provenance": {"heldout_report_sha256": sha}, **extra}, evaluation=
                         {p: {k: deepcopy(held[p][k]) for k in reg.BOUND_HELDOUT_KEYS} for p in reg.POSITIONS})


@pytest.fixture
def env(tmp_path):
    """A model directory with a pinned champion and a fully evidenced candidate; the committed
    manifest with the champion pinned (old) and a proposed promotion with an owner approval (new)."""
    root = tmp_path / "models"
    champ_sha = _write(root, "independent_projections_ifp-1.0.0.json", _ifp_artifact(MODEL_VERSION))
    pro_sha = _write(root, "reports/prospective.json", _prospective())
    held_sha = _write(root, "reports/heldout.json", _heldout())
    cand_sha = _write(root, f"candidates/{CAND}.json", _cand_artifact(root))
    ledger = root / "weekly" / "ledger" / "s2026w08"
    ledger.mkdir(parents=True)
    (ledger / "pregame-run.json").write_text('{"run_id": "frozen"}')

    old = _pre()
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


def test_pre_promotion_manifest_had_ifp_1_as_incumbent():
    m = _pre()
    p = reg.production(m)
    assert p["version"] == MODEL_VERSION == ps.FALLBACK_VERSION == "ifp-1.0.0"
    assert p["state"] == reg.APPROVED and p["approval"]["kind"] == reg.INCUMBENT
    assert reg.entry(m, "ifp-2.0.0")["state"] == reg.SHADOW
    assert [h["event"] for h in m["history"]] == ["registry_initialized"]


def test_committed_production_is_ifp_2_by_owner_approval():
    m = reg.load_manifest()
    p = reg.production(m)
    assert p["version"] == ps.MODEL_VERSION == "ifp-2.0.0"
    assert p["state"] == reg.APPROVED and p["scoring_fingerprint"] == FP and p["rollback_to"] == MODEL_VERSION
    ap = p["approval"]
    assert ap["kind"] == reg.OWNER_APPROVAL and ap["approved_by"] and ap["override"] is True
    assert ap["start_sit_ready"] is False and ap["evidence_sha256"] == {}     # no evidence is claimed
    assert [e["version"] for e in m["models"] if e["state"] == reg.APPROVED] == ["ifp-2.0.0"]
    assert reg.entry(m, MODEL_VERSION)["state"] == reg.RETIRED
    assert [h["event"] for h in m["history"]] == ["registry_initialized", "promotion"]
    # a valid, append-only transition from the pre-promotion manifest
    assert reg.verify_transition(_pre(), m) == []


def test_dry_run_report_on_the_committed_manifest(tmp_path):
    m = reg.load_manifest()
    rep = reg.dry_run_report(m, model_root=tmp_path, live_scoring_fp=FP, consumer_version=ps.MODEL_VERSION)
    assert rep["production"]["version"] == ps.MODEL_VERSION and rep["production"]["consumers_match"]
    assert rep["production"]["artifact_pinned"] is False and rep["production"]["note"]
    assert {c["version"] for c in rep["candidates"]} == {MODEL_VERSION, "ifp-hgb-0.1.0"}
    # the owner override does not pass the evidence gate: the approval cites no report
    gate = reg.evaluate_promotion(_pre(), m, model_root=tmp_path, live_scoring_fp=FP)
    assert gate["activatable"] is False and gate["blocking"]
    assert not any(c["activatable"] or c["eligible_for_review"] or c["activates"] for c in rep["candidates"])


def test_incumbent_record_cannot_be_reused_by_another_model():
    m = _pre()
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
    m = _pre()
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
    assert reg.production(reg.load_manifest())["version"] == ps.MODEL_VERSION


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
    _write(root, f"candidates/{CAND}-newer.json", _cand_artifact(root))
    assert _gate(env)["activatable"]
    _write(root, f"candidates/{CAND}.json", _cand_artifact(root, retrained=True))
    assert not _gate(env)["activatable"]


def test_wrong_scoring_fingerprint_rejected(env):
    root, _, new = env
    assert _results(_gate(env, fp="0000000000000000"))["scoring_fingerprint"] == reg.FAIL
    assert _results(_gate(env, fp=None))["scoring_fingerprint"] == reg.INSUFFICIENT
    # the artifact itself trained under other scoring, though the manifest claims the league's
    bad = deepcopy(new)
    reg.entry(bad, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json",
                                                         {**_cand_artifact(root), "scoring_fingerprint": "other"})
    rep = _gate(env, bad)
    assert not rep["activatable"] and _results(rep)["scoring_fingerprint"] == reg.FAIL
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
        reg.plan_rollback(_pre(), live_scoring_fp=FP, **kw)
    # the committed rollback target (ifp-1.0.0) is registered but its artifact hash is not pinned yet
    with pytest.raises(reg.RegistryError, match=f"cannot roll back to {MODEL_VERSION}"):
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
    assert before["independent"]["run"]["model_version"] == ps.FALLBACK_VERSION == MODEL_VERSION   # fixture run: ifp-1
    assert reg.production(reg.load_manifest())["version"] == ps.MODEL_VERSION


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
    assert out.stdout.strip() == ps.MODEL_VERSION
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
    assert rep["production"]["version"] == ps.MODEL_VERSION and rep["dry_run"] is True
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


@pytest.mark.parametrize("kind,rel,mutate,check", [
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o["subject"].update(challenger_version="ifp-2.0.0"),
     "prospective_report_subject"),
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o["subject"].update(champion_version="ifp-0.9.0"),
     "prospective_report_subject"),
    (reg.PROSPECTIVE, "reports/prospective.json", lambda o: o.pop("subject"), "prospective_report_subject"),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o.update(model_version=MODEL_VERSION), "heldout_report_subject"),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o.update(scoring_fingerprint="other"), "heldout_report_subject"),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o["positions"]["QB"].update(relative_mae_improvement=0.09),
     "artifact_report_binding"),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o["positions"]["RB"]["primary_minus_best_baseline_mae"].update(
        ci95=[-0.9, -0.5]), "artifact_report_binding"),
    (reg.HELDOUT, "reports/heldout.json", lambda o: o["espn_comparison"].update(matched_observations=1400),
     "artifact_report_binding"),
])
def test_report_of_another_model_is_rejected(env, kind, rel, mutate, check):
    """A hash-verified, strong report about a different model or artifact never counts."""
    root, _, new = env
    _repin(new, kind, _rewrite(root, rel, mutate))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)[check] in (reg.FAIL, reg.INSUFFICIENT)


def test_artifact_without_embedded_heldout_result_is_rejected(env):
    root, _, new = env
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", _ifp_artifact(CAND))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["artifact_report_binding"] in (reg.FAIL, reg.INSUFFICIENT)


def _hand_rollback(promoted: dict, target: str) -> dict:
    """A hand-authored rollback manifest restoring `target` (bypassing plan_rollback)."""
    new = deepcopy(promoted)
    reg.entry(new, CAND)["state"] = reg.RETIRED
    t = reg.entry(new, target)
    t["state"] = reg.APPROVED
    t["approval"] = {"kind": reg.ROLLBACK_RESTORE, "approved_by": "o", "approved_at": AT.isoformat()}
    new["production"] = {"family": "ifp", "version": target}
    new["history"].append({"at": AT.isoformat(), "event": "rollback", "from": CAND, "production": target,
                           "by": "o", "reason": "r"})
    return new


def test_hand_authored_rollback_must_target_the_restorable_previous_model(env):
    root, _, promoted = env
    assert reg.verify_transition(promoted, _hand_rollback(promoted, MODEL_VERSION)) == []
    problems = reg.verify_transition(promoted, _hand_rollback(promoted, "ifp-hgb-0.1.0"))   # experimental, unpinned
    assert any("rollback_to" in p for p in problems)
    assert any("never production" in p for p in problems)
    assert any("no pinned artifact" in p for p in problems)
    bad = _hand_rollback(promoted, MODEL_VERSION)
    bad["history"][-1]["event"] = "promotion"
    assert any("rollback` history event" in p for p in reg.verify_transition(promoted, bad))
    unpinned = deepcopy(promoted)
    reg.entry(unpinned, MODEL_VERSION)["artifact"]["sha256"] = None
    assert any("no pinned artifact" in p for p in
               reg.verify_transition(unpinned, _hand_rollback(unpinned, MODEL_VERSION)))


def test_artifact_report_binding_covers_gate_fields(env):
    """An artifact embedding a FAILING bootstrap CI cannot be paired with a report showing a passing one."""
    root, _, new = env
    art = _cand_artifact(root)
    art["evaluation"]["WR"]["primary_minus_best_baseline_mae"] = {"ci95": [-0.2, 0.3]}
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", art)
    rep = _gate(env, new)
    assert not rep["activatable"] and "WR.primary_minus_best_baseline_mae" in str(
        next(c for c in rep["checks"] if c["name"] == "artifact_report_binding")["observed"])
    art = _cand_artifact(root)
    del art["evaluation"]["TE"]["primary_minus_best_baseline_mae"]
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", art)
    assert _results(_gate(env, new))["artifact_report_binding"] == reg.FAIL


@pytest.mark.parametrize("field,value", [("from", "ifp-2.0.0"), ("at", "2027-02-01T00:00:00+00:00"),
                                         ("by", "someone-else"), ("reason", ""), ("from", None)])
def test_promotion_history_event_must_match_the_approval(env, field, value):
    _, old, new = env
    bad = deepcopy(new)
    if value is None:
        bad["history"][-1].pop(field)
    else:
        bad["history"][-1][field] = value
    assert any("final history event" in p for p in reg.verify_transition(old, bad))
    assert not _gate(env, bad)["activatable"]


def test_hand_authored_rollback_event_must_match_its_record(env):
    _, _, promoted = env
    bad = _hand_rollback(promoted, MODEL_VERSION)
    bad["history"][-1]["by"] = "someone-else"
    assert any("rollback` history event" in p for p in reg.verify_transition(promoted, bad))


@pytest.mark.parametrize("value", ["ifp-9.0.0", ["ifp", CAND], None, 7])
def test_malformed_production_value_fails_closed(env, value):
    _, old, new = env
    bad = deepcopy(new)
    bad["production"] = value
    assert any("production must be an object" in p for p in reg.validate_manifest(bad))
    rep = _gate(env, bad)
    assert not rep["activatable"] and rep["blocking"]
    bad = deepcopy(new)
    bad["history"].append("promotion")
    assert any("history event must be an object" in p for p in reg.validate_manifest(bad))


def test_artifact_must_cite_the_exact_heldout_report(env):
    """Field-by-field binding is not enough: the artifact names the sha256 of the report it was fitted
    from, so a repinned report with different per-position ESPN numbers (same aggregate) is rejected."""
    root, _, new = env
    reg.entry(new, CAND)["approval"]["start_sit_ready"] = True
    _repin(new, reg.HELDOUT, _rewrite(root, "reports/heldout.json", lambda o: o["espn_comparison"]["by_position"]
                                      ["QB"].update(n=999, primary={"mae": 1.0})))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["artifact_heldout_report_sha"] == reg.FAIL
    art = _cand_artifact(root)
    del art["config"]
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", art)
    assert _results(_gate(env, new))["artifact_heldout_report_sha"] == reg.INSUFFICIENT


@pytest.mark.parametrize("content", [b"not json", b"[1]", json.dumps({"version": MODEL_VERSION, "scoring_fingerprint": FP}),
                                     json.dumps(_ifp_artifact("ifp-0.1.0")),
                                     json.dumps(_ifp_artifact(MODEL_VERSION, "other"))])
def test_rollback_artifact_must_be_the_target_model(env, content):
    """A pinned hash only proves the bytes; they must also be the rollback target under the league scoring."""
    root, old, new = env
    p = root / "independent_projections_ifp-1.0.0.json"
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    for m in (old, new):
        reg.entry(m, MODEL_VERSION)["artifact"]["sha256"] = sha
    rep = _gate(env)
    assert not rep["activatable"] and _results(rep)["rollback_artifact_integrity"] == reg.FAIL
    with pytest.raises(reg.RegistryError, match="cannot roll back"):
        reg.plan_rollback(new, reason="r", by="o", at=AT, model_root=root, live_scoring_fp=FP)


@pytest.mark.parametrize("version", [["ifp"], {"v": 1}, "", None, 3])
def test_non_string_model_version_fails_closed(env, version):
    _, old, new = env
    bad = deepcopy(new)
    bad["models"][1]["version"] = version
    assert any("non-empty string" in p for p in reg.validate_manifest(bad))
    assert not _gate(env, bad)["activatable"]


@pytest.mark.parametrize("mutate", [
    lambda a: a.pop("models"),
    lambda a: a["models"].pop("TE"),
    lambda a: a.update(schema="other/v0"),
    lambda a: a.update(unexpected_field=1),
    lambda a: a["models"]["RB"].update(coef=[0.5, 0.1]),
    lambda a: a["models"]["WR"].update(stds=[0.0]),
    lambda a: a["models"]["QB"].pop("intercept"),
])
def test_artifact_must_load_with_the_production_loader(env, mutate):
    """A hash-verified JSON object with the right version is not enough: Artifact.from_dict must load it
    and every position needs a well-formed model."""
    root, _, new = env
    art = _cand_artifact(root)
    mutate(art)
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", art)
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["artifact_loads"] == reg.FAIL


def test_non_finite_evidence_rejected(env):
    root, _, new = env
    p = root / "reports" / "heldout.json"
    text = p.read_text().replace('"common_support_n": 900', '"common_support_n": Infinity', 1)
    p.write_text(text)
    _repin(new, reg.HELDOUT, hashlib.sha256(p.read_bytes()).hexdigest())
    rep = _gate(env, new)
    assert not rep["activatable"]
    assert any("non-finite" in c["explanation"] for c in rep["checks"])
    assert reg.heldout_checks({"positions": {pos: {"common_support_n": float("inf"), "relative_mae_improvement": 1.0,
                                                   "primary_minus_best_baseline_mae": {"ci95": [-1, float("-inf")]}}
                                             for pos in reg.POSITIONS}})[0]["result"] == reg.INSUFFICIENT


def test_duplicate_prospective_weeks_rejected(env):
    """One real week repeated eight times must not satisfy the eight-week gate."""
    root, _, new = env

    def dup(o):
        o["pregame"]["weeks"] = [[2026, 1]] * 8
    _repin(new, reg.PROSPECTIVE, _rewrite(root, "reports/prospective.json", dup))
    rep = _gate(env, new)
    assert not rep["activatable"] and _results(rep)["finalized_evaluation"] == reg.FAIL


GOOD_BANDS = {"bands": [{"low": None, "high": 10.0, "q_low": -3.0, "q_high": 4.0},
                        {"low": 10.0, "high": None, "q_low": -6.0, "q_high": 8.0}]}


@pytest.mark.parametrize("intervals,ok", [
    ({"QB": GOOD_BANDS, "WR": GOOD_BANDS}, True),
    ({"QB": 1}, False),
    ({"QB": {"bands": []}}, False),
    ({"QB": {"bands": [{"low": None, "high": None, "q_low": -1.0}]}}, False),
    ({"QB": {"bands": [{"low": None, "high": None, "q_low": 2.0, "q_high": 1.0}]}}, False),
    ({"K": GOOD_BANDS}, False),
    ([1], False),
])
def test_artifact_intervals_must_have_the_production_shape(env, intervals, ok):
    root, _, new = env
    art = _cand_artifact(root)
    art["intervals"] = intervals
    reg.entry(new, CAND)["artifact"]["sha256"] = _write(root, f"candidates/{CAND}.json", art)
    rep = _gate(env, new)
    assert rep["activatable"] is ok
    assert _results(rep)["artifact_loads"] == (reg.PASS if ok else reg.FAIL)


@pytest.mark.parametrize("mutate", [
    lambda e: e["artifact"].update(path=7),
    lambda e: e["validation_reports"][0].update(path=None),
    lambda e: e["validation_reports"][0].update(path=["x"]),
    lambda e: e.update(approval="owner"),
    lambda e: e["approval"].update(approved_by=1),
    lambda e: e["approval"].update(evidence_sha256=["x"]),
    lambda e: e.update(training_cutoff="2026-16"),
    lambda e: e.update(rollback_to=["ifp-1.0.0"]),
    lambda e: e.update(family=None),
    lambda e: e.update(scoring_fingerprint=1),
])
def test_malformed_entry_fields_fail_closed_without_crashing(env, mutate, tmp_path):
    root, old, new = env
    bad = deepcopy(new)
    mutate(reg.entry(bad, CAND))
    assert reg.validate_manifest(bad)
    rep = _gate(env, bad)
    assert not rep["activatable"] and rep["blocking"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(reg.RegistryError):
        reg.load_manifest(p)


def test_evaluators_never_raise_on_malformed_input(env):
    root, old, _ = env
    for junk in ([], "x", {"schema": reg.SCHEMA, "models": "x", "production": {}, "history": [1]}):
        assert reg.evaluate_promotion(old, junk, model_root=root, live_scoring_fp=FP)["activatable"] is False
        assert reg.review_candidate(junk, CAND, model_root=root, live_scoring_fp=FP)["activatable"] is False


def test_prospective_subject_comes_from_the_ledger_runs():
    from app.analytics import weekly_learning as wl

    def run(kind, champ, chal):
        return {"kind": kind, "artifacts": {"champion": {"version": champ}, "challenger": {"version": chal}}}
    one = wl.report_subject([run("pregame", MODEL_VERSION, CAND)] * 3 + [run("retrospective", "x", "y")], "pregame", FP)
    assert (one["champion_version"], one["challenger_version"], one["runs"]) == (MODEL_VERSION, CAND, 3)
    mixed = wl.report_subject([run("pregame", MODEL_VERSION, CAND), run("pregame", MODEL_VERSION, "ifp-2.0.0")],
                              "pregame", FP)
    assert mixed["challenger_version"] is None and mixed["challenger_versions_seen"] == ["ifp-2.0.0", CAND]
    legacy = wl.report_subject([{"kind": "pregame"}], "pregame", FP)
    assert legacy["champion_version"] is None and legacy["challenger_version"] is None
    assert wl.report_subject([], "pregame", FP)["challenger_version"] is None
