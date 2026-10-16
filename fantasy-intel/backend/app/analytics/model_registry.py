"""Approved production model registry for the independent projection engine (research only).

A reviewed, committed manifest (model_registry.json next to this file) says which model family and
version is the APPROVED production model, and records every other model with its lifecycle state.
Nothing in the hosted app imports this module, and nothing here changes what a consumer reads:
the projection switch (projection_source.MODEL_VERSION) stays the single runtime source of truth.
Changing production is a separate, explicitly approved code change to the manifest AND to the
consumer wiring; this module only verifies such a change and reports, as a dry run, whether one
would be allowed (docs/independent_projection_engine.md, "Model registry and promotion").

Lifecycle states (STATES):
  experimental_research      a research model (historical backtests only)
  prospective_shadow         forecast weekly in the frozen pregame ledger, never read by consumers
  eligible_for_review        prospective evidence met every gate; waiting for a human decision
  approved_production        the one model consumers may read; needs an explicit approval record
  deprecated_or_rolled_back  retired; its artifact and every ledger it wrote are kept

Fail-closed rules:
  * Only an explicit approval record makes a model production: an owner approval naming who
    approved it, when, and the sha256 of every evidence report it relied on; or a rollback
    record restoring a previously approved model. The incumbent (the production model when the
    registry was created) carries an `incumbent` record and nothing else may use that kind.
  * "Latest artifact" never means approved: an artifact is identified by its path AND the sha256
    of its exact bytes, and a missing, unreadable or changed file is rejected.
  * Every promotion check whose evidence is missing is `insufficient_data`, never a pass. A
    historical (backtest) report can never stand in for prospective confirmation.
  * Transitions are append-only: models are never removed, a pinned artifact hash never changes,
    history only grows, and production moves only with a matching approval or rollback event.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

SCHEMA = "model-registry/v1"
MANIFEST_PATH = Path(__file__).with_name("model_registry.json")
POSITIONS = ("QB", "RB", "WR", "TE")

EXPERIMENTAL, SHADOW, ELIGIBLE, APPROVED, RETIRED = (
    "experimental_research", "prospective_shadow", "eligible_for_review", "approved_production",
    "deprecated_or_rolled_back")
STATES = (EXPERIMENTAL, SHADOW, ELIGIBLE, APPROVED, RETIRED)
INCUMBENT, OWNER_APPROVAL, ROLLBACK_RESTORE = "incumbent", "owner_approval", "rollback_restore"
APPROVAL_KINDS = (INCUMBENT, OWNER_APPROVAL, ROLLBACK_RESTORE)

# Validation report kinds. Only PROSPECTIVE can satisfy the prospective gate; HELDOUT is the
# existing production baseline gate (forecast_backtest.PROMOTION_CRITERIA); HISTORICAL is any
# research backtest or replay and never counts toward a promotion.
PROSPECTIVE, HELDOUT, HISTORICAL = "prospective_shadow_evaluation", "heldout_backtest", "historical_research"
REPORT_KINDS = (PROSPECTIVE, HELDOUT, HISTORICAL)
REQUIRED_REPORTS = (PROSPECTIVE, HELDOUT)
# held-out fields the gate reads; the candidate artifact must embed them and they must match the report
BOUND_HELDOUT_KEYS = ("common_support_n", "relative_mae_improvement", "primary_minus_best_baseline_mae")

PASS, FAIL, INSUFFICIENT, NOT_APPLICABLE = "pass", "fail", "insufficient_data", "not_applicable"
MODEL_FIELDS = ("family", "version", "state", "artifact", "scoring_fingerprint", "training_cutoff", "approval",
                "validation_reports", "rollback_to", "reason")


class RegistryError(ValueError):
    """The manifest, an artifact or an evidence report is missing, malformed or does not verify."""


# ----------------------------------------------------------------------------- reading


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise RegistryError(f"model registry {path} is unreadable ({err.__class__.__name__})") from err
    problems = validate_manifest(m)
    if problems:
        raise RegistryError(f"model registry {path} is invalid: " + "; ".join(problems))
    return m


def _ts(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _sha_ok(v) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def validate_manifest(m) -> list[str]:
    """Structural problems (empty list = valid). Exactly one approved_production model, the
    production pointer names it, and it carries an approval record of an allowed kind."""
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    out = []
    if m.get("schema") != SCHEMA:
        out.append(f"schema {m.get('schema')!r} is not {SCHEMA!r}")
    models = m.get("models")
    if not isinstance(models, list) or not models:
        return out + ["no models"]
    seen = set()
    for e in models:
        if not isinstance(e, dict):
            out.append("a model entry is not an object")
            continue
        v = e.get("version")
        if not isinstance(v, str) or not v.strip():
            out.append(f"a model version must be a non-empty string, not {v!r}")
            continue
        missing = [f for f in MODEL_FIELDS if f not in e]
        if missing:
            out.append(f"{v}: missing {', '.join(missing)}")
        if v in seen:
            out.append(f"{v}: duplicate version")
        seen.add(v)
        if e.get("state") not in STATES:
            out.append(f"{v}: unknown state {e.get('state')!r}")
        out += [f"{v}: {p}" for p in _entry_type_problems(e)]
        if not isinstance(e.get("reason"), str) or not e.get("reason", "").strip():
            out.append(f"{v}: a human-readable reason is required")
    approved = [e for e in models if isinstance(e, dict) and e.get("state") == APPROVED]
    prod = m.get("production")
    if not isinstance(prod, dict):
        out.append("production must be an object {family, version}")
        prod = {}
    if len(approved) != 1:
        out.append(f"{len(approved)} approved_production models (exactly one required)")
    elif (prod.get("family"), prod.get("version")) != (approved[0].get("family"), approved[0].get("version")):
        out.append(f"production pointer {prod} does not name the approved_production model "
                   f"{approved[0].get('version')}")
    else:
        ap = approved[0].get("approval")
        ap = ap if isinstance(ap, dict) else {}
        if ap.get("kind") not in APPROVAL_KINDS:
            out.append(f"{approved[0].get('version')}: approved_production without an approval record")
        elif ap["kind"] == INCUMBENT and not _is_initial_production(m, approved[0]["version"]):
            out.append(f"{approved[0].get('version')}: an `incumbent` record is only valid for the registry's "
                       "initial production model")
        elif ap["kind"] != INCUMBENT and not (ap.get("approved_by") and _ts(ap.get("approved_at"))):
            out.append(f"{approved[0].get('version')}: approval needs approved_by and a timezone-aware approved_at")
    hist = m.get("history")
    if not isinstance(hist, list) or not hist:
        out.append("history must be a non-empty list")
    elif not all(isinstance(h, dict) for h in hist):
        out.append("every history event must be an object")
    elif not all(isinstance(h.get("event"), str) and (h.get("production") is None or isinstance(h["production"], str))
                 for h in hist):
        out.append("every history event needs a string event and a string or null production")
    return out


def _ref_problems(ref, what: str, path_required: bool) -> list[str]:
    if not isinstance(ref, dict) or "path" not in ref or "sha256" not in ref:
        return [f"{what} must be {{path, sha256}}"]
    out = []
    path = ref["path"]
    if not ((isinstance(path, str) and path.strip()) or (path is None and not path_required)):
        out.append(f"{what} path must be a non-empty string{'' if path_required else ' or null'}")
    if ref["sha256"] is not None and not _sha_ok(ref["sha256"]):
        out.append(f"{what} sha256 is not a full lowercase hex digest")
    if path is None and ref["sha256"] is not None:
        out.append(f"{what} has a sha256 but no path")
    return out


def _entry_type_problems(e: dict) -> list[str]:
    """Every field has the type the evaluator relies on, so malformed input is a validation
    problem, never a crash."""
    out = []
    if not (isinstance(e.get("family"), str) and e["family"].strip()):
        out.append("family must be a non-empty string")
    out += _ref_problems(e.get("artifact"), "artifact", path_required=False)
    if not isinstance(e.get("scoring_fingerprint"), str):
        out.append("scoring_fingerprint must be a string")
    cut = e.get("training_cutoff")
    if cut is not None and not (isinstance(cut, dict) and all(type(cut.get(k)) is int for k in ("season", "week"))):
        out.append("training_cutoff must be null or {season: int, week: int}")
    ap = e.get("approval")
    if ap is not None and not (isinstance(ap, dict) and ap.get("kind") in APPROVAL_KINDS):
        out.append(f"approval must be null or an object with kind in {APPROVAL_KINDS}")
    elif isinstance(ap, dict):
        for k in ("approved_by", "approved_at"):
            if ap.get(k) is not None and not isinstance(ap[k], str):
                out.append(f"approval {k} must be a string")
        if "evidence_sha256" in ap and not isinstance(ap["evidence_sha256"], dict):
            out.append("approval evidence_sha256 must be an object")
    reps = e.get("validation_reports")
    if not isinstance(reps, list):
        out.append("validation_reports must be a list")
    else:
        for r in reps:
            if not isinstance(r, dict) or r.get("kind") not in REPORT_KINDS:
                out.append(f"a validation report needs kind in {REPORT_KINDS}")
            else:
                out += _ref_problems(r, f"{r['kind']} report", path_required=True)
    if e.get("rollback_to") is not None and not isinstance(e["rollback_to"], str):
        out.append("rollback_to must be null or a version string")
    if "retired" in e and not isinstance(e["retired"], dict):
        out.append("retired must be an object")
    return out


def _is_initial_production(m: dict, version: str) -> bool:
    hist = m.get("history") or []
    return bool(hist) and isinstance(hist[0], dict) and hist[0].get("event") == "registry_initialized" \
        and hist[0].get("production") == version


def entry(m: dict, version: str) -> dict | None:
    return next((e for e in m["models"] if e.get("version") == version), None)


def production(m: dict) -> dict:
    """The approved production model. Raises when the manifest does not name exactly one."""
    problems = validate_manifest(m)
    if problems:
        raise RegistryError("; ".join(problems))
    return entry(m, m["production"]["version"])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _crit(name: str, result: str, explanation: str, observed=None, required=None) -> dict:
    return {"name": name, "result": result, "observed": observed, "required": required, "explanation": explanation}


def _resolve(root: Path, rel: str) -> Path | None:
    """`rel` under `root` only (no absolute paths, no escaping the model directory)."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    root = Path(root).resolve()
    p = (root / rel).resolve()
    return p if p == root or root in p.parents else None


def verify_file(root: Path, ref: dict, what: str) -> tuple[dict, bytes | None]:
    """(criterion, file bytes if verified). The digest covers the exact bytes on disk."""
    sha = (ref or {}).get("sha256")
    if not _sha_ok(sha):
        return _crit(f"{what}_integrity", INSUFFICIENT, f"{what} has no pinned sha256; an unpinned file is never "
                     "trusted (pin it in a reviewed manifest change)", None, "sha256 of the exact bytes"), None
    p = _resolve(root, (ref or {}).get("path"))
    if p is None:
        return _crit(f"{what}_integrity", FAIL, f"{what} path {ref.get('path')!r} is not a relative path inside the "
                     "model directory"), None
    try:
        data = p.read_bytes()
    except OSError as err:
        return _crit(f"{what}_integrity", FAIL, f"{what} {ref['path']} is missing or unreadable "
                     f"({err.__class__.__name__})", None, sha), None
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        return _crit(f"{what}_integrity", FAIL, f"{what} {ref['path']} was changed or replaced: sha256 {got[:16]}... "
                     f"!= pinned {sha[:16]}...", got, sha), None
    return _crit(f"{what}_integrity", PASS, f"{what} {ref['path']} matches its pinned sha256", got, sha), data


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def strict_json(data: bytes):
    """JSON without NaN/Infinity (Python's parser accepts them; evidence must be finite)."""
    return json.loads(data, parse_constant=_reject_constant)


def load_family_artifact(family: str, data: bytes) -> dict:
    """Deserialize verified bytes with the family's PRODUCTION loader and check that every
    position has a well-formed model; returns the artifact as a dict. Raises RegistryError."""
    try:
        raw = strict_json(data)
    except ValueError as err:
        raise RegistryError(f"the artifact is not valid finite JSON ({err})") from err
    if family != "ifp":
        raise RegistryError(f"no production loader for model family {family!r}")
    from .independent_projections import Artifact

    try:
        art = Artifact.from_dict(raw)
    except (TypeError, ValueError, KeyError, AttributeError) as err:
        raise RegistryError(f"the production loader rejects the artifact ({err.__class__.__name__}: {err})") from err
    for pos in POSITIONS:
        m = art.models.get(pos)
        if m is None:
            raise RegistryError(f"the artifact has no {pos} model")
        n = len(m.columns)
        if not n or not (len(m.means) == len(m.stds) == len(m.coef) == n) or m.position != pos:
            raise RegistryError(f"the artifact's {pos} model is malformed")
        if not all(_num(v) for v in [*m.means, *m.stds, *m.coef, m.intercept, m.lam]) or any(v <= 0 for v in m.stds):
            raise RegistryError(f"the artifact's {pos} model has non-finite or invalid parameters")
    problem = _intervals_problem(art.intervals)
    if problem:
        raise RegistryError(f"the artifact's intervals are malformed: {problem}")
    return raw


def _intervals_problem(intervals) -> str | None:
    """Published 80% intervals must have the shape interval_for reads, with finite numbers, and
    interval_for must return finite bounds across the prediction range."""
    from .independent_projections import interval_for

    if not isinstance(intervals, dict):
        return "not an object"
    for pos, spec in intervals.items():
        if pos not in POSITIONS:
            return f"unknown position {pos!r}"
        bands = spec.get("bands") if isinstance(spec, dict) else None
        if not isinstance(bands, list) or not bands:
            return f"{pos} has no bands"
        for b in bands:
            if not isinstance(b, dict) or not all(_num(b.get(k)) for k in ("q_low", "q_high")) \
                    or not all(b.get(k) is None or _num(b.get(k)) for k in ("low", "high")) or "low" not in b \
                    or "high" not in b or b["q_low"] > b["q_high"]:
                return f"{pos} has a malformed band {b!r}"
        try:
            for pred in [x / 2 for x in range(0, 121)]:
                iv = interval_for(intervals, pos, pred)
                if iv is not None and not all(_num(v) for v in iv):
                    return f"{pos} interval at {pred} is not finite"
        except (TypeError, KeyError, AttributeError, ValueError) as err:
            return f"{pos}: interval_for fails ({err.__class__.__name__})"
    return None


def artifact_identity_problem(data: bytes, version: str, live_fp: str | None, family: str = "ifp") -> str | None:
    """None when verified artifact bytes load with the production loader and name `version` under
    the league's scoring; otherwise why not (a hash pins bytes, not what they contain)."""
    try:
        art = load_family_artifact(family, data)
    except RegistryError as err:
        return str(err)
    if art.get("version") != version:
        return f"the artifact names version {art.get('version')!r}, not {version!r}"
    if live_fp is None or art.get("scoring_fingerprint") != live_fp:
        return f"the artifact was trained under scoring {art.get('scoring_fingerprint')!r}, the league scores {live_fp}"
    return None


def _json_bytes(data: bytes, what: str):
    try:
        return strict_json(data)
    except ValueError as err:
        raise RegistryError(f"{what} is not valid finite JSON ({err})") from err


# ----------------------------------------------------------------------------- promotion checks


def _artifact_checks(e: dict, root: Path, live_fp: str | None) -> tuple[list[dict], dict | None]:
    """(checks, the verified artifact JSON or None)."""
    c, data = verify_file(root, e.get("artifact") or {}, "artifact")
    out, art = [c], None
    fp = e.get("scoring_fingerprint")
    if live_fp is None:
        out.append(_crit("scoring_fingerprint", INSUFFICIENT, "the live league's scoring fingerprint is unknown or "
                         "unsupported", fp, None))
    elif fp != live_fp:
        out.append(_crit("scoring_fingerprint", FAIL, f"registered under scoring {fp}, the league scores {live_fp}",
                         fp, live_fp))
    elif data is None:
        out.append(_crit("scoring_fingerprint", INSUFFICIENT, "the artifact's own scoring fingerprint cannot be read "
                         "from an unverified file", fp, live_fp))
    else:
        try:
            art = load_family_artifact(e.get("family"), data)
        except RegistryError as err:
            out.append(_crit("artifact_loads", FAIL, str(err)))
        else:
            out.append(_crit("artifact_loads", PASS, "the production loader accepts the artifact"))
        if art is not None and not isinstance(art, dict):
            art = None
            out.append(_crit("scoring_fingerprint", FAIL, "artifact is not an object", fp, live_fp))
        if art is not None:
            inner = art.get("scoring_fingerprint") if isinstance(art, dict) else None
            ver = art.get("version") if isinstance(art, dict) else None
            ok = inner == live_fp and ver == e.get("version")
            out.append(_crit("scoring_fingerprint", PASS if ok else FAIL,
                             "manifest, artifact and league scoring agree" if ok else
                             f"artifact says version {ver!r} under scoring {inner!r}; the manifest says "
                             f"{e.get('version')!r} under {fp!r} and the league scores {live_fp}",
                             {"manifest": fp, "artifact": inner, "artifact_version": ver}, live_fp))
    cut = e.get("training_cutoff")
    out.append(_crit("training_cutoff", PASS if isinstance(cut, dict) and cut.get("season") and cut.get("week")
                     else INSUFFICIENT, "training cutoff recorded" if isinstance(cut, dict) and cut.get("season")
                     and cut.get("week") else "no training cutoff {season, week} recorded", cut, "{season, week}"))
    return out, art


def report_identity_checks(e: dict, replaces: str, pros: dict | None, held: dict | None,
                           art: dict | None, held_sha: str | None = None) -> list[dict]:
    """Evidence must be about THIS candidate: a hash-verified report of another model (or another
    scoring) never counts. The prospective report's `subject` names the challenger, the champion it
    was compared with and the scoring; the held-out report names its model_version and scoring, and
    the candidate artifact must embed that held-out result (fit_final copies it), binding the
    artifact to the report it was fitted from."""
    v, fp, out = e.get("version"), e.get("scoring_fingerprint"), []
    if pros is not None:
        sub = pros.get("subject") if isinstance(pros.get("subject"), dict) else None
        if sub is None:
            out.append(_crit("prospective_report_subject", INSUFFICIENT, "the prospective report does not name the "
                             "model it evaluates (`subject`), so it cannot be attributed to this candidate"))
        else:
            got = {k: sub.get(k) for k in ("challenger_version", "champion_version", "scoring_fingerprint")}
            want = {"challenger_version": v, "champion_version": replaces, "scoring_fingerprint": fp}
            out.append(_crit("prospective_report_subject", PASS if got == want else FAIL,
                             "the report evaluates this candidate against the model it replaces" if got == want else
                             f"the report evaluates {got}, not {want}", got, want))
    if held is not None:
        got = {"model_version": held.get("model_version"), "scoring_fingerprint": held.get("scoring_fingerprint")}
        want = {"model_version": v, "scoring_fingerprint": fp}
        out.append(_crit("heldout_report_subject", PASS if got == want else FAIL,
                         "the held-out report evaluates this candidate" if got == want else
                         f"the held-out report evaluates {got}, not {want}", got, want))
        cfg = art.get("config") if isinstance(art, dict) else None
        cited = ((cfg or {}).get("provenance") or {}).get("heldout_report_sha256") if isinstance(cfg, dict) else None
        out.append(_crit("artifact_heldout_report_sha", PASS if held_sha and cited == held_sha else
                         (INSUFFICIENT if cited is None else FAIL),
                         "the artifact records the sha256 of exactly this held-out report" if held_sha and cited ==
                         held_sha else ("the artifact does not record which held-out report it was fitted from "
                                        "(config.provenance.heldout_report_sha256)" if cited is None else
                                        "the artifact was fitted from a different held-out report"),
                         cited, held_sha))
        ev = art.get("evaluation") if isinstance(art, dict) else None
        if not isinstance(ev, dict):
            out.append(_crit("artifact_report_binding", INSUFFICIENT, "the artifact does not embed the held-out "
                             "result it was fitted from (or did not verify)"))
        else:
            pos = held.get("positions") if isinstance(held.get("positions"), dict) else {}
            diff = []
            for p in POSITIONS:
                a, r = ev.get(p), pos.get(p)
                if not isinstance(a, dict) or not isinstance(r, dict) or any(k not in a for k in BOUND_HELDOUT_KEYS):
                    diff.append(f"{p} (missing)")
                    continue
                # every embedded field (fit_final copies them verbatim), the gate-critical ones required
                diff += [f"{p}.{k}" for k in sorted(a) if a[k] != r.get(k)]
            espn = (held.get("espn_comparison") or {}).get("matched_observations")
            if ev.get("espn_matched_observations", 0) != (espn or 0):
                diff.append("espn_matched_observations")
            out.append(_crit("artifact_report_binding", FAIL if diff else PASS,
                             f"the artifact's embedded held-out result differs from the report: {diff}" if diff
                             else "the artifact embeds this held-out report's result", diff or None))
    return out


def _report(e: dict, root: Path, kind: str) -> tuple[list[dict], dict | None, str | None]:
    refs = [r for r in e.get("validation_reports") or [] if r.get("kind") == kind]
    if not refs:
        hist = [r for r in e.get("validation_reports") or [] if r.get("kind") == HISTORICAL]
        why = f"no {kind} report is referenced"
        if kind == PROSPECTIVE and hist:
            why += f" ({len(hist)} historical research report(s) cannot replace prospective confirmation)"
        return [_crit(f"{kind}_report", INSUFFICIENT, why)], None, None
    if len(refs) > 1:
        return [_crit(f"{kind}_report", FAIL, f"{len(refs)} {kind} reports are referenced; exactly one must be "
                      "named so the approval cites one immutable result")], None, None
    c, data = verify_file(root, refs[0], f"{kind}_report")
    if data is None:
        return [c], None, None
    try:
        rep = _json_bytes(data, f"{kind} report")
    except RegistryError as err:
        return [c, _crit(f"{kind}_report", FAIL, str(err))], None, None
    if not isinstance(rep, dict):
        return [c, _crit(f"{kind}_report", FAIL, "report is not an object")], None, None
    return [c], rep, refs[0]["sha256"]


def prospective_checks(report: dict | None) -> list[dict]:
    """The weekly_learning evaluate report (`{"pregame": ...}`), recomputed per position from its
    metrics with weekly_learning.shadow_criteria (the stored shadow_status is not trusted)."""
    if report is None:
        return [_crit("prospective_evidence", INSUFFICIENT, "no verified prospective report")]
    from .weekly_learning import shadow_criteria

    pre = report.get("pregame")
    if not isinstance(pre, dict):
        return [_crit("prospective_evidence", FAIL, "the report has no `pregame` evaluation (retrospective replays are "
                      "never evidence)")]
    out = []
    kind_ok = pre.get("kind") == "pregame"
    out.append(_crit("pregame_provenance", PASS if kind_ok else FAIL,
                     "frozen pregame ledger rows (latest run before each player's kickoff)" if kind_ok
                     else f"evidence kind {pre.get('kind')!r} is not pregame", pre.get("kind"), "pregame"))
    weeks, fin = pre.get("weeks"), pre.get("finality")
    if not isinstance(weeks, list) or not isinstance(fin, dict):
        out.append(_crit("finalized_evaluation", INSUFFICIENT, "the report lists no finalized weeks"))
    elif not all(isinstance(x, list) and len(x) == 2 and all(type(i) is int for i in x) for x in weeks):
        out.append(_crit("finalized_evaluation", FAIL, "counted weeks are not [season, week] pairs"))
    else:
        dup = len({tuple(x) for x in weeks}) != len(weeks)
        bad = [f"{s}-{w:02d}" for s, w in weeks if not (fin.get(f"{s}-{w:02d}") or {}).get("fully_final")]
        prelim = set(map(tuple, (pre.get("preliminary") or {}).get("weeks") or []))
        overlap = [f"{s}-{w:02d}" for s, w in weeks if (s, w) in prelim]
        if dup:
            bad.append("duplicate weeks")
        ok = bool(weeks) and not bad and not overlap
        out.append(_crit("finalized_evaluation", PASS if ok else (INSUFFICIENT if not weeks else FAIL),
                         "every counted week is fully final" if ok else
                         ("no fully final week" if not weeks else
                          f"counted weeks that are not fully final: {sorted(set(bad + overlap))}"),
                         len(weeks), "only fully final weeks"))
    for pos in POSITIONS:
        if not (pre.get("positions") or {}).get(pos):
            out.append(_crit(f"{pos}:position_report", INSUFFICIENT, f"no {pos} comparison in the report"))
        for c in shadow_criteria(pre, pos):
            out.append({**c, "name": f"{pos}:{c['name']}"})
    return out


def heldout_checks(report: dict | None) -> list[dict]:
    """The existing production baseline gate (forecast_backtest.PROMOTION_CRITERIA) per position."""
    if report is None:
        return [_crit("production_baseline", INSUFFICIENT, "no verified held-out report")]
    from .forecast_backtest import PROMOTION_CRITERIA as P

    out = []
    for pos in POSITIONS:
        r = (report.get("positions") or {}).get(pos)
        if not isinstance(r, dict):
            out.append(_crit(f"{pos}:production_baseline", INSUFFICIENT, f"no held-out {pos} result"))
            continue
        n, rel = r.get("common_support_n"), r.get("relative_mae_improvement")
        ci = (r.get("primary_minus_best_baseline_mae") or {}).get("ci95")
        if not (_num(n) and _num(rel) and isinstance(ci, list) and len(ci) == 2 and _num(ci[1])):
            out.append(_crit(f"{pos}:production_baseline", INSUFFICIENT, f"held-out {pos} n, relative MAE "
                             "improvement or bootstrap CI is missing", {"n": n, "rel": rel, "ci95": ci}))
            continue
        ok = (n >= P["heldout_min_observations_per_position"]
              and rel >= P["min_relative_mae_improvement_vs_best_trailing_baseline"] and ci[1] < 0)
        out.append(_crit(f"{pos}:production_baseline", PASS if ok else FAIL,
                         f"held-out n {n}, {rel:.1%} better than the best trailing baseline, CI {ci}",
                         {"n": n, "relative_mae_improvement": rel, "ci95": ci},
                         f"n >= {P['heldout_min_observations_per_position']}, >= "
                         f"{P['min_relative_mae_improvement_vs_best_trailing_baseline']:.0%}, CI upper < 0"))
    return out


def espn_checks(report: dict | None, required: bool) -> list[dict]:
    """PROMOTION_CRITERIA espn_rule; required only when the approval marks positions Start/Sit ready."""
    if not required:
        return [_crit("espn_rule", NOT_APPLICABLE, "the approval does not claim Start/Sit readiness, so consumers keep "
                      "the 'not validated against ESPN' label")]
    if report is None:
        return [_crit("espn_rule", INSUFFICIENT, "no verified held-out report with an ESPN comparison")]
    from .forecast_backtest import PROMOTION_CRITERIA as P

    by = (report.get("espn_comparison") or {}).get("by_position") or {}
    out = []
    for pos in POSITIONS:
        r = by.get(pos) or {}
        n = r.get("n") or 0
        mp, me = (r.get("primary") or {}).get("mae"), (r.get("espn") or {}).get("mae")
        if not (_num(n) and _num(mp) and _num(me)) or n < P["espn_min_matched_per_position"]:
            out.append(_crit(f"{pos}:espn_rule", INSUFFICIENT, f"{n} matched pregame ESPN observations (< "
                             f"{P['espn_min_matched_per_position']} or MAE missing)", n))
        else:
            out.append(_crit(f"{pos}:espn_rule", PASS if mp <= me else FAIL, f"MAE {mp} vs ESPN {me} on {n}",
                             {"mae": mp, "espn_mae": me, "n": n}, "MAE <= ESPN"))
    return out


def approval_checks(e: dict, evidence_sha: dict[str, str | None]) -> list[dict]:
    """An explicit owner approval naming who, when, and the exact evidence it relied on."""
    ap = e.get("approval")
    if e.get("state") != APPROVED:
        return [_crit("lifecycle_state", FAIL, f"state is {e.get('state')!r}; only an approved_production model may be "
                      "read by consumers (a dry run cannot change it)", e.get("state"), APPROVED)]
    out = [_crit("lifecycle_state", PASS, "approved_production", APPROVED, APPROVED)]
    if not isinstance(ap, dict) or ap.get("kind") != OWNER_APPROVAL:
        out.append(_crit("human_approval", FAIL, "no explicit owner approval record (an incumbent or rollback record "
                         "never authorizes a new promotion)", (ap or {}).get("kind") if isinstance(ap, dict) else None,
                         OWNER_APPROVAL))
        return out
    problems = []
    if not (isinstance(ap.get("approved_by"), str) and ap["approved_by"].strip()):
        problems.append("approved_by is missing")
    if _ts(ap.get("approved_at")) is None:
        problems.append("approved_at is missing or not a timezone-aware ISO timestamp")
    cited = ap.get("evidence_sha256") if isinstance(ap.get("evidence_sha256"), dict) else {}
    for kind in REQUIRED_REPORTS:
        if not evidence_sha.get(kind) or cited.get(kind) != evidence_sha[kind]:
            problems.append(f"the approval does not cite the verified {kind} report")
    out.append(_crit("human_approval", FAIL if problems else PASS,
                     "; ".join(problems) if problems else f"approved by {ap['approved_by']} at {ap['approved_at']}",
                     {k: ap.get(k) for k in ("approved_by", "approved_at")}, OWNER_APPROVAL))
    return out


def rollback_reference_checks(m: dict, e: dict, root: Path, live_fp: str | None, replaces: str) -> list[dict]:
    """The model a promotion replaces must stay restorable: rollback_to names it, and its artifact
    is pinned, present and scored like the league (a rollback could not restore it otherwise)."""
    target = e.get("rollback_to")
    prev = entry(m, target) if target else None
    if target != replaces or prev is None:
        return [_crit("rollback_reference", FAIL, f"rollback_to {target!r} must name the model being replaced "
                      f"({replaces})", target, replaces)]
    c, data = verify_file(root, prev.get("artifact") or {}, "rollback_artifact")
    if data is not None:
        why = artifact_identity_problem(data, target, live_fp, prev.get("family"))
        if why:
            c = _crit("rollback_artifact_integrity", FAIL, why)
    fp_ok = live_fp is not None and prev.get("scoring_fingerprint") == live_fp
    return [_crit("rollback_reference", PASS, f"replaces {replaces}, which becomes the rollback target", target,
                  replaces), c,
            _crit("rollback_scoring", PASS if fp_ok else FAIL,
                  "the rollback target is scored like the league" if fp_ok else
                  f"rollback target scored {prev.get('scoring_fingerprint')}, league {live_fp}: a rollback could not "
                  "restore it", prev.get("scoring_fingerprint"), live_fp)]


def _evidence(m: dict, e: dict, root: Path, live_fp: str | None, replaces: str) -> tuple[list[dict], dict]:
    """Every evidence check for candidate `e` (artifact, scoring, prospective, held-out, ESPN,
    rollback reference). Returns (checks, {report kind: verified sha256})."""
    checks = [_crit("registered", PASS, f"{e['version']} ({e.get('family')}) is registered", e.get("state"))]
    art_c, art = _artifact_checks(e, root, live_fp)
    checks += art_c
    shas = {}
    pros_c, pros, shas[PROSPECTIVE] = _report(e, root, PROSPECTIVE)
    held_c, held, shas[HELDOUT] = _report(e, root, HELDOUT)
    ap = e.get("approval") if isinstance(e.get("approval"), dict) else {}
    checks += pros_c + _guarded("prospective_evidence", prospective_checks, pros)
    checks += held_c + _guarded("production_baseline", heldout_checks, held)
    checks += _guarded("espn_rule", espn_checks, held, bool(ap.get("start_sit_ready")))
    checks += _guarded("report_identity", report_identity_checks, e, replaces, pros, held, art, shas[HELDOUT])
    checks += rollback_reference_checks(m, e, root, live_fp, replaces)
    return checks, shas


def _guarded(name: str, fn, *args) -> list[dict]:
    """A report with an unexpected shape is a failed check, never a crash or a pass."""
    try:
        return fn(*args)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as err:
        return [_crit(name, FAIL, f"the report is malformed ({err.__class__.__name__}: {err})")]


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _blocking(checks: list[dict]) -> list[str]:
    return [f"{c['name']}: {c['explanation']}" for c in checks if c["result"] not in (PASS, NOT_APPLICABLE)]


_MALFORMED = (TypeError, AttributeError, KeyError, ValueError, IndexError)


def _malformed(version, err: Exception) -> dict:
    why = f"malformed input ({err.__class__.__name__}: {err})"
    return {"version": version, "dry_run": True, "activates": False, "activatable": False,
            "eligible_for_review": False, "checks": [_crit("input", FAIL, why)], "blocking": [why]}


def review_candidate(m: dict, version: str, *, model_root: Path, live_scoring_fp: str | None) -> dict:
    """Fail-closed wrapper: unexpected input shapes become a failed check, never a crash."""
    try:
        return _review_candidate(m, version, model_root=model_root, live_scoring_fp=live_scoring_fp)
    except _MALFORMED as err:
        return _malformed(version, err)


def evaluate_promotion(old: dict, new: dict, *, model_root: Path, live_scoring_fp: str | None) -> dict:
    """Fail-closed wrapper of _evaluate_promotion (see there)."""
    try:
        return _evaluate_promotion(old, new, model_root=model_root, live_scoring_fp=live_scoring_fp)
    except _MALFORMED as err:
        return _malformed(None, err)


def _review_candidate(m: dict, version: str, *, model_root: Path, live_scoring_fp: str | None) -> dict:
    """Dry run on the CURRENT manifest: would `version` be eligible for human review, and what
    blocks it? A model that is not production can never be activatable here: activation needs a
    proposed manifest with an owner approval (evaluate_promotion)."""
    problems = validate_manifest(m)
    e = entry(m, version) if not problems else None
    base = {"version": version, "dry_run": True, "activates": False, "activatable": False}
    if problems or e is None:
        why = "; ".join(problems) if problems else f"{version} is not registered"
        return {**base, "state": None, "eligible_for_review": False, "checks": [_crit("registered", FAIL, why)],
                "blocking": [why]}
    cur = m["production"]["version"]
    if version == cur:
        return {**base, "state": e["state"], "eligible_for_review": False, "blocking": [],
                "checks": [_crit("already_production", NOT_APPLICABLE, f"{version} is the approved production model")]}
    checks, _ = _evidence(m, e, Path(model_root), live_scoring_fp, cur)
    review = _blocking(checks)
    checks.append(_crit("lifecycle_state", FAIL, f"state is {e['state']!r}, not approved_production: consumers never "
                        "read it; only a reviewed manifest change with an owner approval can", e["state"], APPROVED))
    return {**base, "state": e["state"], "eligible_for_review": not review, "checks": checks,
            "blocking": _blocking(checks)}


def _evaluate_promotion(old: dict, new: dict, *, model_root: Path, live_scoring_fp: str | None) -> dict:
    """Gate for a FUTURE promotion: replacing manifest `old` (committed) by `new` (proposed).
    `activatable` only when the transition is valid, the new production model passed every
    evidence check against the model it replaces, and an owner approval cites the exact evidence.
    A dry run: nothing is written and no consumer changes (`activates` is always False)."""
    base = {"dry_run": True, "activates": False}
    problems = verify_transition(old, new)
    if problems:
        return {**base, "version": None, "activatable": False, "checks": [_crit("transition", FAIL, "; ".join(problems))],
                "blocking": problems}
    cur, cand = old["production"]["version"], new["production"]["version"]
    if cand == cur:
        return {**base, "version": cand, "activatable": False, "blocking": ["production does not change"],
                "checks": [_crit("transition", FAIL, "production does not change")]}
    e = entry(new, cand)
    moves = [h for h in new["history"][len(old["history"]):] if isinstance(h, dict)
             and h.get("event") in ("promotion", "rollback")]
    last = moves[-1] if moves else None
    if not isinstance(last, dict) or last.get("event") != "promotion":
        return {**base, "version": cand, "activatable": False, "blocking": ["the last history event is not a promotion"],
                "checks": [_crit("transition", FAIL, "the last history event is not a promotion")]}
    checks = [_crit("transition", PASS, f"{cur} -> {cand} keeps every model, pinned hash and history entry")]
    ev, shas = _evidence(new, e, Path(model_root), live_scoring_fp, cur)
    checks += ev + approval_checks(e, shas)
    blocking = _blocking(checks)
    return {**base, "version": cand, "replaces": cur, "activatable": not blocking, "checks": checks,
            "blocking": blocking}


def production_status(m: dict, *, model_root: Path, live_scoring_fp: str | None, consumer_version: str,
                      consumer_artifact_sha256: str | None = None, consumer_pinned_version: str | None = None) -> dict:
    """The approved production model, whether the runtime switch reads exactly it (version, and the artifact pin:
    the single-sha pin production_pin.APPROVED_ARTIFACT_SHA256 must equal the registry's artifact sha256 of
    `consumer_pinned_version`, the version it belongs to; weekly ifp-2 artifacts are approved in the database),
    and its rollback target."""
    p = production(m)
    c, _ = verify_file(Path(model_root), p["artifact"], "artifact")
    pinned = _sha_ok(p["artifact"].get("sha256"))
    pin_entry = (entry(m, consumer_pinned_version) if consumer_pinned_version else p) or {"artifact": {}}
    per_run = p["artifact"].get("path") is None
    if pinned:
        note = None
    elif per_run:
        note = (f"{p['version']} has no single artifact: each weekly artifact is approved in the database "
                "(approved_artifacts, by the sha256 of its file, which runs record as artifact_id); there is nothing "
                "to pin here")
    else:
        note = ("the production artifact hash is not pinned yet: pin it (pin-artifact, a reviewed manifest change) "
                "so a rollback can restore its exact bytes")
    target = entry(m, p.get("rollback_to")) if p.get("rollback_to") else None
    rb = None if target is None else {
        "version": target["version"], "artifact_pinned": _sha_ok((target.get("artifact") or {}).get("sha256")),
        "artifact_check": verify_file(Path(model_root), target.get("artifact") or {}, "rollback_artifact")[0],
        "training_cutoff": target.get("training_cutoff")}
    return {"family": p["family"], "version": p["version"], "approval_kind": p["approval"]["kind"],
            "override": bool(p["approval"].get("override")),
            "consumer_version": consumer_version, "consumers_match": consumer_version == p["version"],
            "scoring_matches_league": live_scoring_fp is not None and p["scoring_fingerprint"] == live_scoring_fp,
            "artifact_pinned": pinned, "artifact_per_run": per_run and not pinned,
            "consumer_artifact_sha256": consumer_artifact_sha256, "consumer_pinned_version": consumer_pinned_version,
            "consumer_pin_matches": consumer_artifact_sha256 == pin_entry["artifact"].get("sha256"),
            "artifact_check": c, "rollback_target": rb, "note": note}


def dry_run_report(m: dict, *, model_root: Path, live_scoring_fp: str | None, consumer_version: str,
                   now: datetime | None = None, consumer_artifact_sha256: str | None = None,
                   consumer_pinned_version: str | None = None) -> dict:
    """Production status plus a promotion evaluation of every other registered model. Read-only."""
    now = now or datetime.now(timezone.utc)
    return {"schema": "model-registry-dry-run/v1", "created_at": now.isoformat(), "dry_run": True,
            "note": "Read-only. Nothing here promotes, activates or rolls back a model; production changes only by a "
                    "separate, explicitly approved change to model_registry.json and the consumer wiring.",
            "production": production_status(m, model_root=model_root, live_scoring_fp=live_scoring_fp,
                                             consumer_version=consumer_version,
                                             consumer_artifact_sha256=consumer_artifact_sha256,
                                             consumer_pinned_version=consumer_pinned_version),
            "candidates": [review_candidate(m, e["version"], model_root=model_root, live_scoring_fp=live_scoring_fp)
                           for e in m["models"] if e["version"] != m["production"]["version"]]}


# ----------------------------------------------------------------------------- rollback (planned, never applied)


def plan_rollback(m: dict, *, reason: str, by: str, at: datetime, model_root: Path,
                  live_scoring_fp: str | None) -> dict:
    """The manifest a rollback WOULD produce: the current production model is retired and its
    rollback_to model (a previously approved, retired model) is restored. Pure: returns a new
    manifest and touches no file, artifact or ledger. Refuses when the target's artifact does not
    verify or was trained under a scoring the league no longer uses."""
    if not (isinstance(reason, str) and reason.strip()) or not (isinstance(by, str) and by.strip()):
        raise RegistryError("a rollback needs a reason and who requested it")
    if at.tzinfo is None:
        raise RegistryError("rollback time must be timezone-aware")
    cur = production(m)
    target = entry(m, cur.get("rollback_to")) if cur.get("rollback_to") else None
    if target is None:
        raise RegistryError(f"{cur['version']} has no registered rollback target")
    if target.get("state") != RETIRED or not _was_approved(m, target["version"]):
        raise RegistryError(f"rollback target {target['version']} was never an approved production model")
    c, data = verify_file(Path(model_root), target.get("artifact") or {}, "rollback_artifact")
    if c["result"] != PASS:
        raise RegistryError(f"cannot roll back to {target['version']}: {c['explanation']}")
    why = artifact_identity_problem(data, target["version"], live_scoring_fp, target.get("family"))
    if why:
        raise RegistryError(f"cannot roll back to {target['version']}: {why}")
    if live_scoring_fp is None or target.get("scoring_fingerprint") != live_scoring_fp:
        raise RegistryError(f"cannot roll back to {target['version']}: it was trained under scoring "
                            f"{target.get('scoring_fingerprint')}, the league scores {live_scoring_fp}")
    new = deepcopy(m)
    when = at.isoformat()
    old_e, new_e = entry(new, cur["version"]), entry(new, target["version"])
    old_e["state"] = RETIRED
    old_e["retired"] = {"at": when, "by": by, "reason": reason, "kind": "rolled_back"}
    new_e["state"] = APPROVED
    new_e["approval"] = {"kind": ROLLBACK_RESTORE, "approved_by": by, "approved_at": when, "reason": reason,
                         "restores": target["version"], "previous_approval": target.get("approval")}
    new_e.pop("retired", None)
    new_e["rollback_to"] = None
    new["production"] = {"family": new_e["family"], "version": new_e["version"]}
    new["history"].append({"at": when, "event": "rollback", "from": cur["version"], "production": target["version"],
                           "by": by, "reason": reason})
    problems = verify_transition(m, new)
    if problems:
        raise RegistryError("planned rollback is not a valid transition: " + "; ".join(problems))
    return new


def _was_approved(m: dict, version: str) -> bool:
    return any(h.get("production") == version for h in m.get("history") or [] if isinstance(h, dict))


def verify_transition(old: dict, new: dict) -> list[str]:
    """Problems with replacing manifest `old` by `new` (empty = allowed): both valid; no model
    removed; a pinned artifact or report hash never changes; history is append-only; and a
    production change is matched by a new history event and an approval or rollback record."""
    out = [f"new: {p}" for p in validate_manifest(new)] + [f"old: {p}" for p in validate_manifest(old)]
    if out:
        return out
    for e in old["models"]:
        n = entry(new, e["version"])
        if n is None:
            out.append(f"{e['version']} was removed (models and their artifacts are never deleted)")
            continue
        if e["artifact"].get("sha256") and n["artifact"] != e["artifact"]:
            out.append(f"{e['version']}: pinned artifact reference changed")
        if (e["artifact"].get("sha256") is None and n["artifact"].get("path") != e["artifact"].get("path")):
            out.append(f"{e['version']}: artifact path changed")
        if e["scoring_fingerprint"] != n["scoring_fingerprint"]:
            out.append(f"{e['version']}: scoring fingerprint changed")
        if e.get("training_cutoff") is not None and n.get("training_cutoff") != e["training_cutoff"]:
            out.append(f"{e['version']}: recorded training cutoff changed")
        pinned = {(r["kind"], r["path"]): r["sha256"] for r in e["validation_reports"] if r.get("sha256")}
        now = {(r["kind"], r["path"]): r["sha256"] for r in n["validation_reports"]}
        if any(now.get(k) != v for k, v in pinned.items()):
            out.append(f"{e['version']}: a pinned validation report was changed or dropped")
    oh, nh = old["history"], new["history"]
    if nh[:len(oh)] != oh:
        out.append("history was rewritten (it is append-only)")
    if old["production"] != new["production"]:
        added = nh[len(oh):]
        p = entry(new, new["production"]["version"])
        ev = [h for h in added if isinstance(h, dict) and h.get("production") == p["version"]
              and h.get("event") in ("promotion", "rollback")]
        if not ev:
            out.append("production changed without a promotion or rollback history event")
        kind = (p.get("approval") or {}).get("kind")
        # the last production-changing event (a later bookkeeping event such as an artifact pin does not count)
        moves = [h for h in added if isinstance(h, dict) and h.get("event") in ("promotion", "rollback")]
        last = moves[-1] if moves else {}
        if kind not in (OWNER_APPROVAL, ROLLBACK_RESTORE):
            out.append(f"production changed to {p['version']} without an owner approval or rollback record")
        elif kind == OWNER_APPROVAL:
            ap = p["approval"]
            want = {"event": "promotion", "from": old["production"]["version"], "production": p["version"],
                    "at": ap.get("approved_at"), "by": ap.get("approved_by")}
            got = {k: last.get(k) for k in want}
            if got != want or not (isinstance(last.get("reason"), str) and last["reason"].strip()):
                out.append(f"the final history event must be a promotion {want} with a reason; got {got}")
        elif kind == ROLLBACK_RESTORE:
            old_prod = entry(old, old["production"]["version"])
            if old_prod.get("rollback_to") != p["version"]:
                out.append(f"rollback target {p['version']} is not {old_prod['version']}'s rollback_to "
                           f"({old_prod.get('rollback_to')!r})")
            if not _was_approved(old, p["version"]):
                out.append(f"rollback target {p['version']} was never production")
            if not _sha_ok((p.get("artifact") or {}).get("sha256")):
                out.append(f"rollback target {p['version']} has no pinned artifact to restore")
            ap = p["approval"]
            want = {"event": "rollback", "from": old_prod["version"], "production": p["version"],
                    "at": ap.get("approved_at"), "by": ap.get("approved_by")}
            if {k: last.get(k) for k in want} != want or not (isinstance(last.get("reason"), str)
                                                              and last["reason"].strip()):
                out.append(f"a rollback must be recorded by a final `rollback` history event {want} with a reason")
        prev = entry(new, old["production"]["version"])
        if prev["state"] != RETIRED:
            out.append(f"previous production {prev['version']} must be retired, not {prev['state']}")
    return out


# ----------------------------------------------------------------------------- artifact pinning (proposed, reviewed)

PIN_EVENT = "artifact_pinned"


def training_cutoff_of(art: dict) -> dict | None:
    """{season, week} of the last week the artifact trained on, read from the artifact itself: the end of every
    position model's train_window (YYYYWW). None when the positions disagree or the window is missing."""
    ends = set()
    for m in (art.get("models") or {}).values():
        win = m.get("train_window") if isinstance(m, dict) else None
        if not (isinstance(win, list) and len(win) == 2 and type(win[1]) is int):
            return None
        ends.add(divmod(win[1], 100))
    if len(ends) != 1:
        return None
    season, week = ends.pop()
    return {"season": season, "week": week} if 1 <= week <= 22 else None


def _heldout_binding_problem(art: dict, report: dict) -> str | None:
    """None when the held-out report is the one embedded in the artifact: same model and scoring, and every field
    the gate reads equal per position (the artifact does not record the report's sha, see Known limitations)."""
    if report.get("model_version") != art.get("version"):
        return f"report model_version {report.get('model_version')!r} is not {art.get('version')!r}"
    if report.get("scoring_fingerprint") != art.get("scoring_fingerprint"):
        return "report and artifact scoring fingerprints differ"
    for pos in POSITIONS:
        got = ((report.get("positions") or {}).get(pos) or {})
        emb = ((art.get("evaluation") or {}).get(pos) or {})
        bad = [k for k in BOUND_HELDOUT_KEYS if k not in got or got.get(k) != emb.get(k)]
        if bad:
            return f"{pos}: report {', '.join(bad)} differ from the artifact's embedded held-out result"
    return None


def pin_artifact(m: dict, version: str, *, model_root: Path, live_scoring_fp: str | None,
                 recorded_runs: list[dict], by: str, at: datetime, reason: str) -> tuple[dict, dict]:
    """The manifest that pins `version`'s artifact to the exact bytes production served, plus a report.
    Pure: returns a new manifest and touches no file; applying it is a reviewed change of model_registry.json.

    Refuses unless: the entry's artifact path is set and its sha256 is still null (a pin never changes); the
    file loads with the production loader and names `version` under the league's scoring; and the NEWEST
    eligible production run of `version` recorded exactly this file's sha256, so the pin names the bytes users
    were shown, not merely the file that happens to be on disk now. `recorded_runs` ([{run_id, created_at,
    artifact_id, scoring_fingerprint, verified}], see recorded_runs_for_pin) are chosen the way consumers choose
    runs: only runs under the live league scoring (`live_scoring_fp`) whose stored rows verified against their
    content hash (`verified` True) are evidence. A newer run under another scoring, or a damaged one, can neither
    block the pin nor stand in as its evidence; the report counts what was ignored. The training cutoff is read
    from the artifact; an already recorded, different cutoff refuses. A held-out report referenced with a null
    sha256 is pinned too when it binds to the artifact (_heldout_binding_problem); otherwise it stays unpinned
    and the report says why. Appends an `artifact_pinned` history event (production does not change)."""
    if not (isinstance(reason, str) and reason.strip()) or not (isinstance(by, str) and by.strip()):
        raise RegistryError("a pin needs a reason and who requested it")
    if at.tzinfo is None:
        raise RegistryError("pin time must be timezone-aware")
    problems = validate_manifest(m)
    if problems:
        raise RegistryError("; ".join(problems))
    e = entry(m, version)
    if e is None:
        raise RegistryError(f"{version} is not registered")
    ref = e.get("artifact") or {}
    if ref.get("sha256") is not None:
        raise RegistryError(f"{version}'s artifact is already pinned ({ref['sha256'][:16]}...); a pin never changes")
    path = _resolve(Path(model_root), ref.get("path"))
    if path is None:
        raise RegistryError(f"{version} has no artifact path inside the model directory to pin (a per-run "
                            "artifact model is approved per artifact in the database: approved_artifacts, "
                            "`project --register-artifact manual_pin`)")
    try:
        data = path.read_bytes()
    except OSError as err:
        raise RegistryError(f"{version} artifact {ref['path']} is missing or unreadable ({err.__class__.__name__}); "
                            "its identity cannot be pinned and must not be reconstructed") from err
    sha = hashlib.sha256(data).hexdigest()
    why = artifact_identity_problem(data, version, live_scoring_fp, e.get("family"))
    if why:
        raise RegistryError(f"cannot pin {version}: {why}")
    ignored = {"other_scoring": 0, "not_verified": 0, "no_artifact_id": 0}
    eligible = []
    for r in recorded_runs:
        if r.get("scoring_fingerprint") != live_scoring_fp:
            ignored["other_scoring"] += 1
        elif r.get("verified") is not True:
            ignored["not_verified"] += 1
        elif not _sha_ok(r.get("artifact_id")):
            ignored["no_artifact_id"] += 1
        else:
            eligible.append(r)
    recorded = sorted(eligible, key=lambda r: (str(r.get("created_at")), str(r.get("run_id"))))
    if not recorded:
        raise RegistryError(f"cannot pin {version}: no stored production run under the league's scoring with verified "
                            f"rows recorded an artifact sha256 (ignored: {ignored}), so nothing proves these bytes are "
                            "what production served")
    newest = recorded[-1]
    if newest["artifact_id"] != sha:
        raise RegistryError(f"cannot pin {version}: the newest production run {newest['run_id']} recorded artifact "
                            f"{newest['artifact_id'][:16]}..., but {ref['path']} hashes to {sha[:16]}... (the file "
                            "was replaced; the served artifact is not on disk)")
    art = load_family_artifact(e.get("family"), data)
    cutoff = training_cutoff_of(art)
    if cutoff is None:
        raise RegistryError(f"cannot pin {version}: the artifact's training cutoff cannot be read from its models")
    if e.get("training_cutoff") is not None and e["training_cutoff"] != cutoff:
        raise RegistryError(f"cannot pin {version}: recorded training cutoff {e['training_cutoff']} != the "
                            f"artifact's {cutoff}")
    new = deepcopy(m)
    ne = entry(new, version)
    when = at.isoformat()
    ne["artifact"] = {"path": ref["path"], "sha256": sha}
    ne["training_cutoff"] = cutoff
    reports = []
    for r in ne["validation_reports"]:
        if r.get("kind") != HELDOUT or r.get("sha256") is not None:
            continue
        rp = _resolve(Path(model_root), r.get("path"))
        try:
            rep = _json_bytes(rp.read_bytes(), "held-out report") if rp is not None else None
        except (OSError, RegistryError) as err:
            reports.append({"path": r.get("path"), "pinned": False, "reason": f"unreadable ({err})"})
            continue
        bind = "the path is not inside the model directory" if rep is None else (
            "report is not an object" if not isinstance(rep, dict) else _heldout_binding_problem(art, rep))
        if bind:
            reports.append({"path": r["path"], "pinned": False, "reason": bind})
            continue
        r["sha256"] = sha256_file(rp)
        reports.append({"path": r["path"], "pinned": True, "sha256": r["sha256"],
                        "reason": "binds to the artifact's embedded held-out result field by field"})
    others = sorted({r["artifact_id"] for r in recorded if r["artifact_id"] != sha})
    ne["pin"] = {"at": when, "by": by, "reason": reason, "artifact_sha256": sha,
                 "training_cutoff_source": "end of every position model's train_window in the artifact",
                 "served_by_runs": [r["run_id"] for r in recorded if r["artifact_id"] == sha],
                 "other_recorded_artifacts": others}
    new["history"].append({"at": when, "event": PIN_EVENT, "production": m["production"]["version"],
                           "version": version, "artifact_sha256": sha, "by": by, "reason": reason})
    bad = verify_transition(m, new)
    if bad:
        raise RegistryError("the pin is not a valid transition: " + "; ".join(bad))
    from . import production_pin

    consumer = {"file": "backend/app/analytics/production_pin.py", "APPROVED_ARTIFACT_SHA256": sha,
                "applies_to": production_pin.PINNED_MODEL_VERSION,
                "currently": production_pin.APPROVED_ARTIFACT_SHA256,
                "matches": production_pin.APPROVED_ARTIFACT_SHA256 == sha}
    return new, {"dry_run": True, "applies": False, "version": version, "artifact": ne["artifact"],
                 "training_cutoff": cutoff, "served_by_runs": ne["pin"]["served_by_runs"],
                 "other_recorded_artifacts": others, "ignored_runs": ignored, "reports": reports,
                 "consumer_pin": consumer if version == production_pin.PINNED_MODEL_VERSION else None,
                 "note": "proposal only: copy it into model_registry.json AND set production_pin.APPROVED_ARTIFACT_SHA256 "
                         "to the same sha in one reviewed change (the consumer copy of the registry pin, tested equal)"}


def recorded_runs_for_pin(con, version: str, season: int, scoring_fp: str | None) -> list[dict]:
    """Pin evidence for pin_artifact, read-only: every stored run of `version` for the live `season`, with its
    scoring fingerprint and whether its stored rows verify against its content hash
    (production_forecasts.verified_runs; the artifact gate is NOT applied, because the pin being established is
    that gate). Runs of other scorings are returned too, so pin_artifact can report them as ignored."""
    from . import production_forecasts as pf
    from ..db.connection import fetch_dicts, table_exists

    if not table_exists(con, pf.RUNS):
        return []
    out = []
    weeks = [r["week"] for r in fetch_dicts(con, f"SELECT DISTINCT week FROM {pf.RUNS} WHERE season = ? AND "
                                                 "model_version = ? ORDER BY week", [season, version])]
    if not table_exists(con, pf.ROWS):      # legacy rows cannot be verified: no run is evidence
        return [{**r, "verified": False} for r in fetch_dicts(
            con, f"SELECT run_id, created_at, artifact_id, scoring_fingerprint FROM {pf.RUNS} "
                 "WHERE season = ? AND model_version = ?", [season, version])]
    for w in weeks:
        for run, _, problem in pf.verified_runs(con, season, w, model_version=version, artifact_id=None):
            out.append({"run_id": run["run_id"], "created_at": run["created_at"], "artifact_id": run.get("artifact_id"),
                        "scoring_fingerprint": run.get("scoring_fingerprint"), "verified": problem is None})
    return out


# ----------------------------------------------------------------------------- rollback dry run (read-only)

# The participation artifact `project` loads next to the ifp artifact (participation.PARTICIPATION_VERSION).
PARTICIPATION_FILE = "participation_ipm-1.0.0.json"


def _served(sel: dict) -> dict:
    run = sel.get("run")
    if run is None:
        return {"run_id": None, "model_version": None, "fallback": False, "reason": sel.get("unavailable_reason"),
                "skipped": sel.get("skipped") or []}
    return {"run_id": run["run_id"], "model_version": run["model_version"], "artifact_id": run.get("artifact_id"),
            "fallback": sel.get("fallback", False), "skipped": sel.get("skipped") or []}


def rollback_dry_run(m: dict, *, model_root: Path, live_scoring_fp: str | None, con=None,
                     season: int | None = None, weeks=(), now: datetime | None = None) -> dict:
    """What a rollback of the current production model would restore, what is missing, and how the run every
    consumer sees would change per week. Read-only: no manifest, artifact, ledger or database is written
    (`con` is only read), and `applies` is always False."""
    from . import projection_source as ps

    now = now or datetime.now(timezone.utc)
    cur = production(m)
    target = entry(m, cur.get("rollback_to")) if cur.get("rollback_to") else None
    out = {"schema": "rollback-dry-run/v1", "created_at": now.isoformat(), "dry_run": True, "applies": False,
           "from": cur["version"], "to": target["version"] if target else None, "restorable": False,
           "checks": [], "restorable_items": [], "missing": [], "consumer_change": [], "steps_to_apply": []}
    if target is None:
        out["missing"].append(f"{cur['version']} has no registered rollback target")
        return out
    root = Path(model_root)
    c, data = verify_file(root, target.get("artifact") or {}, "rollback_artifact")
    out["checks"].append(c)
    if data is not None:
        why = artifact_identity_problem(data, target["version"], live_scoring_fp, target.get("family"))
        out["checks"].append(_crit("rollback_artifact_loads", FAIL if why else PASS,
                                   why or "the production loader accepts it; version and league scoring match"))
        if not why:
            out["restorable_items"].append({"item": f"{target['version']} artifact", "path": target["artifact"]["path"],
                                            "sha256": target["artifact"]["sha256"],
                                            "training_cutoff": target.get("training_cutoff")})
    else:
        out["missing"].append(f"{target['version']} artifact: {c['explanation']}")
    for r in target.get("validation_reports") or []:
        rc, _ = verify_file(root, r, f"{r.get('kind')}_report")
        out["checks"].append(rc)
        if rc["result"] == PASS:
            out["restorable_items"].append({"item": f"{r.get('kind')} report", "path": r.get("path"),
                                            "sha256": r.get("sha256")})
        else:
            out["missing"].append(f"{r.get('kind')} report {r.get('path')}: {rc['explanation']}")
    part = root / PARTICIPATION_FILE
    out["missing"].append(
        "participation artifact ipm-1.0.0 (turns conditional points into the expected points consumers see) is not "
        "registered or pinned, and runs record only its version, so a rollback cannot prove it restores the same "
        f"participation bytes ({'present on disk, sha256 ' + sha256_file(part) if part.exists() else 'not on disk'})")
    try:
        plan_rollback(m, reason="rollback dry run", by="dry-run", at=now, model_root=root,
                      live_scoring_fp=live_scoring_fp)
    except RegistryError as err:
        out["checks"].append(_crit("plan_rollback", FAIL, str(err)))
        out["missing"].append(f"plan_rollback refuses: {err}")
    else:
        out["checks"].append(_crit("plan_rollback", PASS, f"a valid {cur['version']} -> {target['version']} "
                                   "rollback manifest can be planned"))
    out["restorable"] = all(ch["result"] == PASS for ch in out["checks"] if ch["name"] in
                            ("rollback_artifact_integrity", "rollback_artifact_loads", "plan_rollback"))
    if con is not None and season is not None:
        for w in sorted({int(x) for x in weeks}):
            before = ps.select_run(con, season, w)
            after = ps.select_run(con, season, w, versions=(target["version"],))
            change = {"week": w, "before": _served(before), "after": _served(after)}
            if after["run"] is None:
                change["action"] = (f"no servable {target['version']} run: after the rollback consumers would show "
                                    f"ESPN for week {w} until `backtest_independent_projections.py project --week {w} "
                                    f"--model {target['version']}` stores one from the pinned artifact")
            out["consumer_change"].append(change)
    out["steps_to_apply"] = [
        "NOT done here. A rollback is an explicitly authorized, reviewed change:",
        "1. `scripts/model_registry.py plan-rollback --reason ... --by ... --out proposal.json` (write-once proposal)",
        f"2. in one PR: copy the proposal into model_registry.json AND change projection_source.MODEL_VERSION to "
        f"{target['version']} (and its fallback, GOVERNANCE, NOTE), the production_pin constants (which version is "
        "pinned and which is registered weekly), and the `project` default model",
        f"3. `project --model {target['version']} --week W` for each week consumers need, from the pinned artifact",
        "4. stored runs and ledgers stay immutable: nothing is deleted or rewritten"]
    return out


def write_once(path: Path, obj) -> Path:
    """Write JSON to a NEW file atomically (temp + fsync + link); an existing file is never replaced."""
    path = Path(path)
    if path.resolve() == MANIFEST_PATH.resolve():
        raise RegistryError("the committed registry is changed only by a reviewed code change")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=1, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError as err:
            raise RegistryError(f"{path} already exists; proposals and reports are write-once") from err
    finally:
        os.unlink(tmp)
    return path


def write_manifest(m: dict, path: Path) -> Path:
    """A proposed manifest, for review. Applying it to the committed registry is a reviewed code
    change, not something this does."""
    problems = validate_manifest(m)
    if problems:
        raise RegistryError("refusing to write an invalid manifest: " + "; ".join(problems))
    return write_once(path, m)


__all__ = ["SCHEMA", "MANIFEST_PATH", "STATES", "load_manifest", "validate_manifest", "production", "entry",
           "review_candidate", "evaluate_promotion", "production_status", "dry_run_report", "plan_rollback", "verify_transition",
           "pin_artifact", "recorded_runs_for_pin", "training_cutoff_of", "rollback_dry_run",
           "write_manifest", "write_once", "sha256_file", "RegistryError"]
