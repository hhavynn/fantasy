"""Why a stored Fantasy Intel forecast changed: the move split into measured parts that add up to it.

Contract `projection-attribution/v1`, one block per mover of the projection-movers page (`attach`). Read-only, SQL +
stdlib only (the hosted API has no numpy/polars), and nothing here trains, rebuilds a feature or writes.

What the production model computes (ifp-2.2.0 and its predecessors, trained and run offline):

  points if he plays  x = max(intercept + sum_i coef_i * (value_i - mean_i) / std_i, 0)   (ridge, per position; QB
                      mixes a start and a no-start prediction by the chance he starts)
  expected points     e = p * x, p = the participation model's chance he plays

Each stored forecast row keeps p, x, e (2 decimals), its coverage status with the stored reasons, its missing inputs
and `explanation`: the four largest per-input contributions coef_i * (value_i - mean_i) / std_i (2 decimals, only
those of at least 0.05 points), each with the input's value (null when it was unknown and imputed). The full input
vector, the intercept and the coefficients are NOT stored, and nothing is reconstructed here.

The split of one compared week (both forecasts are the movers' verified runs):

  status        one forecast projected him and the other did not (bye, withheld as unavailable): the whole change,
                with the reason stored on the row. Measured.
  availability  (p1 - p0) * x0: the chance he plays moved. Exact (the movers' split).
  per-game      p1 * (x1 - x0), split further only when both rows come from the SAME model artifact (same model version
                and artifact id, neither re-scored to another scoring): the model is then linear in its inputs before
                the floor, so for every input stored in both rows p1 * (c1 - c0) is exactly that input's share. Inputs
                stored in only one row, or in neither, are the residual. A row floored at 0 is not split.
  model         a different model version, a retrained artifact, a re-score to the league's scoring (its factor is
                recomputed per forecast) or an unknown artifact on either side: the per-game change is reported as
                "model changed", never attributed to inputs. The input values both rows stored are still listed,
                without points. A change of the participation model or method moves the availability part to "model
                changed" too.
  residual      everything not measured above, with its reasons, including the rounding of the stored 2-decimal
                values. The parts and the residual add up to the mover's change exactly (to the cent shown).

QB rows are labelled approximate: the stored contributions describe the start/no-start blend and are exact only when
neither scenario is floored at 0, which the stored row cannot confirm.

As-of guarantee: only the two stored runs are read. A week is decomposed only when both runs were created strictly
before its first kickoff and before the player's own team kickoff when stored, and, where a run records its data
cutoff, that cutoff is before the compared week and before the run's own cutoff week. Otherwise the week's change is
residual ("as-of check failed"). No outcome of any compared week is read.

ESPN snapshots publish one total per week and no inputs: their movers get `status = "unavailable"` with the reason,
labelled as ESPN's numbers, never as Fantasy Intel's.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterable

from ..db.connection import fetch_dicts, table_columns, table_exists
from . import production_forecasts as pf
from .projection_backtest import _utc

CONTRACT = "projection-attribution/v1"
INDEPENDENT = "independent"
ZERO_STATUSES = frozenset({"bye", "withheld_unavailable"})       # projection_source.ZERO_STATUSES (tested)
OURS_LABEL = "Fantasy Intel model"
ESPN_LABEL = "ESPN"
TOLERANCE_PER_NUMBER = 0.005     # every stored number is rounded to 2 decimals
STATUS_TEXT = MappingProxyType({"projected": "projected", "withheld_unavailable": "withheld (unavailable)",
                                "bye": "bye week"})

# The model's input names in words: a stdlib copy of the point model's LABELS (tests/test_projection_attribution.py).
LABELS = MappingProxyType({
    "xfp_recent": "expected fantasy points (nflverse xFP) per game", "pass_att_pg": "pass attempts per game",
    "carries_pg": "carries per game", "targets_pg": "targets per game", "rush_share": "share of team rushes",
    "target_share": "target share", "snap_pct": "offensive snap share", "rz_opps_pg": "red-zone opportunities per game",
    "gl_carries_pg": "goal-line carries per game", "air_yards_pg": "air yards per game",
    "games_recent": "games in recent window", "pts_recent": "recent points per game (shrunk)",
    "pts_l3": "points per game, last 3", "pts_season": "season points per game (shrunk)",
    "pts_prev_season": "previous-season points per game", "pts_std": "recent scoring volatility",
    "pts_minus_xfp": "points above nflverse xFP per game", "snap_trend": "change in snap share last game",
    "opp_trend": "change in targets+carries last game", "weeks_since_last": "weeks since last game",
    "games_season": "games played this season", "team_changed": "changed NFL team",
    "team_pts_pg": "team points per game vs league", "opp_pos_allowed": "opponent points allowed to position vs league",
    "opp_pass_epa": "opponent pass EPA/play allowed vs league", "opp_rush_epa": "opponent rush EPA/play allowed vs league",
    "is_home": "home game", "xfp_missing": "no xFP coverage", "snap_missing": "no snap data", "rz_missing": "no red-zone data",
    "ho_exp_xfpl": "expected opportunity value with this week's available teammates (xFP per game)",
    "ho_shift_pg": "expected touches vs his recent average (carries + targets per game)",
    "s_start": "chance he starts at quarterback when he plays", "qb_self_epa": "his passing efficiency (EPA per dropback)",
    "qb_epa_delta": "expected starting QB's passing efficiency vs the QBs he has played with",
    "qb_cpoe_delta": "expected starting QB's accuracy vs the QBs he has played with",
    "qb_changed": "a different starting QB is expected",
    "tm_tgt_gain_pg": "targets opened up by unavailable teammates (per game)",
    "tm_car_gain_pg": "carries opened up by unavailable teammates (per game)",
    "tm_tgt_vacated": "share of team targets held by unavailable teammates",
    "tm_tgt_returning": "share of team targets held by returning teammates",
    "tm_car_vacated": "share of team carries held by unavailable teammates",
    "tm_car_returning": "share of team carries held by returning teammates",
    "dc_tier": "depth chart tier (1 = starter)",
    "aw_role_d": "workload role (expected points per game, follows a sustained drop)",
})
SHARE_INPUTS = frozenset({"rush_share", "target_share", "snap_pct", "snap_trend", "s_start", "tm_tgt_vacated",
                          "tm_tgt_returning", "tm_car_vacated", "tm_car_returning"})
FLAG_INPUTS = frozenset({"is_home", "team_changed", "qb_changed", "xfp_missing", "snap_missing", "rz_missing"})
MISSING_SUFFIX = "__missing"

IMPUTED = "unknown when the forecast was made (filled with the training average)"
NOT_STORED = "not among the inputs this forecast stored"
QB_NOTE = ("Quarterback forecasts blend a start and a no-start prediction by the chance he starts. The stored input "
           "shares describe that blend and are exact only when neither prediction is floored at 0, which the stored "
           "forecast cannot confirm, so they are approximate.")
METHOD = ("Both forecasts come from the same model artifact. Before its floor at 0 the model adds one term per input, "
          "so the change in each input's stored term, times the latest chance he plays, is that input's exact share of "
          "the per-game change. Each forecast stores only its largest input terms; everything else is the residual.")
METHOD_MODEL_CHANGED = ("The model differs between the two forecasts (retrained, another version or re-scored), so the "
                        "per-game change is shown as a model change and not split between inputs. The inputs both "
                        "forecasts stored are listed with their values only.")
ESPN_REASON = ("ESPN publishes one projected total per week and none of the inputs behind it, so its changes cannot be "
               "split into parts.")
RESIDUAL = MappingProxyType({
    "unstored": "Inputs the forecasts did not store (only the largest input terms are kept)",
    "one_sided": "Inputs stored in only one of the two forecasts",
    "no_inputs": "A forecast stored no input terms",
    "floored": "A forecast was floored at 0 points, so its input terms do not add up to it",
    "missing_row": "A stored forecast row or its chance to play is missing",
    "as_of": "As-of check failed: a forecast may have seen the week's games",
    "rounding": "Rounding of the stored 2-decimal values",
})


def _json(v):
    if isinstance(v, (str, bytes, bytearray)):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def feature_label(col: str) -> str:
    if col.endswith(MISSING_SUFFIX):
        base = col[: -len(MISSING_SUFFIX)]
        return f"{LABELS.get(base, base.replace('_', ' '))}: missing-data indicator"
    return LABELS.get(col, col.replace("_", " "))


def feature_unit(col: str) -> str:
    if col.endswith(MISSING_SUFFIX) or col in FLAG_INPUTS:
        return "flag"
    return "share" if col in SHARE_INPUTS else "number"


def _value(item: dict) -> tuple[float | None, str | None]:
    """(value shown, missing reason). A missing-data indicator is 1 when its input was unknown, else 0."""
    col, v = item.get("feature") or "", _num(item.get("value"))
    if col.endswith(MISSING_SUFFIX):
        return (1.0 if item.get("value") is None else 0.0), None
    return (None, IMPUTED) if v is None else (v, None)


def _explanation(row: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in _json((row or {}).get("explanation")) or []:
        if isinstance(e, dict) and e.get("feature") and _num(e.get("contribution")) is not None:
            out[str(e["feature"])] = e
    return out


def _rescored(run: dict) -> bool:
    ev = _json(run.get("evaluation"))
    return isinstance(ev, dict) and isinstance(ev.get("rescored_from"), dict)


def model_changes(before: dict, after: dict) -> list[str]:
    """Why the two runs' point models cannot be compared input by input ([] = same artifact)."""
    out = []
    if before.get("model_version") != after.get("model_version"):
        out.append(f"model version {before.get('model_version') or 'unknown'} → {after.get('model_version') or 'unknown'}")
    if before.get("artifact_id") is None or after.get("artifact_id") is None:
        out.append("model artifact not recorded for a forecast, so the same model cannot be confirmed")
    elif before["artifact_id"] != after["artifact_id"]:
        out.append("model retrained between the two forecasts (different artifact)")
    if _rescored(before) or _rescored(after):
        out.append("re-scored to this league's scoring with a factor recomputed for each forecast")
    return out


def _participation_version(run: dict, row: dict) -> str | None:
    q = _json(row.get("participation"))
    if isinstance(q, dict) and q.get("model_version"):
        return str(q["model_version"])
    return run.get("participation_version")


def as_of_problem(run: dict, row: dict | None, week: int, first_kickoff: datetime | None) -> str | None:
    """None when the run provably predates week `week`'s games (module docstring), else why not."""
    created = _utc(run.get("created_at"))
    if created is None:
        return "forecast creation time unknown"
    if first_kickoff is None:
        return f"week {week} kickoff time unknown"
    if created >= first_kickoff:
        return f"forecast made after week {week}'s first kickoff"
    own = _utc((row or {}).get("kickoff"))
    if own is not None and created >= own:
        return "forecast made after the player's own kickoff"
    season, through = run.get("season"), run.get("data_through_week")
    if through is not None and run.get("data_through_season") is not None:
        key = int(run["data_through_season"]) * 100 + int(through)
        cutoff = run.get("projected_from_week")
        cutoff = int(run["week"] if cutoff is None else cutoff)
        if season is not None and key >= int(season) * 100 + min(cutoff, week):
            return f"forecast inputs run through week {through}, not before week {min(cutoff, week)}"
    return None


# ----------------------------------------------------------------------------- one mover


class _Acc:
    """Accumulates the parts of one mover across the window weeks."""

    def __init__(self):
        self.parts: dict[str, dict] = {}
        self.residual: dict[str, dict] = {}
        self.inputs: dict[str, dict] = {}
        self.missing = {"before": {}, "after": {}}
        self.model_reasons: dict[str, list[int]] = {}
        self.tolerance = 0.0
        self.refused: list[dict] = []
        self.approximate = False

    def part(self, key: str, base: dict, points: float, week: int) -> dict:
        p = self.parts.setdefault(key, {**base, "points": 0.0, "weeks": [], "_before": [], "_after": []})
        p["points"] += points
        if week not in p["weeks"]:
            p["weeks"].append(week)
        return p

    def resid(self, reason: str, points: float, week: int) -> None:
        r = self.residual.setdefault(reason, {"key": reason, "label": RESIDUAL[reason], "points": 0.0, "weeks": []})
        r["points"] += points
        if week not in r["weeks"]:
            r["weeks"].append(week)

    def miss(self, side: str, texts: Iterable, week: int) -> None:
        for t in texts or []:
            if isinstance(t, str) and t:
                self.missing[side].setdefault(t, []).append(week)

    def recorded(self, col: str, eb: dict | None, ea: dict | None, week: int) -> None:
        r = self.inputs.setdefault(col, {"feature": col, "label": feature_label(col), "unit": feature_unit(col),
                                         "weeks": [], "_before": [], "_after": []})
        r["weeks"].append(week)
        for side, e in (("_before", eb), ("_after", ea)):
            r[side].append(_value(e) if e is not None else (None, NOT_STORED))


def _collapse(values: list[tuple]) -> tuple[float | None, str | None, bool]:
    """(value, missing reason, varies) of one side over the weeks: one value when every week agrees."""
    distinct = sorted({(None if v is None else round(v, 4), why) for v, why in values}, key=str)
    if len(distinct) == 1:
        return distinct[0][0], distinct[0][1], False
    return None, None, True


def _public_part(p: dict) -> dict:
    out = {k: v for k, v in p.items() if not k.startswith("_")}
    out["points"] = round(p["points"], 2)
    out["weeks"] = sorted(p["weeks"])
    if p.get("_before") or p.get("_after"):
        bv, bwhy, bvar = _collapse(p["_before"])
        av, awhy, avar = _collapse(p["_after"])
        out.update(before=bv, after=av, before_missing=bwhy, after_missing=awhy, varies=bvar or avar)
    return out


def _week(acc: _Acc, w: int, wk: dict, position: str, sides: dict, first_kickoff: datetime | None) -> None:
    """Split one window week's change into `acc` (module docstring)."""
    d = (_num(wk.get("after")) or 0.0) - (_num(wk.get("before")) or 0.0)
    (b_run, b_row), (a_run, a_row) = sides["before"], sides["after"]
    if b_run is None or a_run is None or b_row is None or a_row is None:
        acc.resid("missing_row", d, w)
        return
    for side, run, row in (("earlier", b_run, b_row), ("latest", a_run, a_row)):
        why = as_of_problem(run, row, w, first_kickoff)
        if why is not None:
            acc.refused.append({"week": w, "forecast": side, "reason": why})
            acc.resid("as_of", d, w)
            return
    sb, sa = b_row.get("coverage_status"), a_row.get("coverage_status")
    zb, za = sb in ZERO_STATUSES, sa in ZERO_STATUSES
    # a projected row's missing inputs; a withheld row's stored reasons are cited on its status part instead
    if not zb:
        acc.miss("before", _json(b_row.get("missing_inputs")), w)
    if not za:
        acc.miss("after", _json(a_row.get("missing_inputs")), w)
    if zb and za:
        acc.resid("rounding", d, w)
        return
    if zb or za:
        key = f"status:{sb}>{sa}"
        p = acc.part(key, {"key": key, "kind": "status", "exact": True,
                           "label": f"Status {STATUS_TEXT.get(sb, str(sb).replace('_', ' '))} → "
                                    f"{STATUS_TEXT.get(sa, str(sa).replace('_', ' '))}",
                           "events": []}, d, w)
        for side, row in (("earlier", b_row), ("latest", a_row)):
            if row.get("coverage_status") in ZERO_STATUSES:
                for t in _json(row.get("missing_inputs")) or []:
                    ev = {"forecast": side, "text": t, "source": "stored with the forecast"}
                    if isinstance(t, str) and ev not in p["events"]:
                        p["events"].append(ev)
        return
    p0, x0, p1, x1 = (_num(b_row.get("p_play")), _num(b_row.get("projected_points")), _num(a_row.get("p_play")),
                      _num(a_row.get("projected_points")))
    if None in (p0, x0, p1, x1):
        acc.resid("missing_row", d, w)
        return
    avail, per_game = (p1 - p0) * x0, p1 * (x1 - x0)
    acc.resid("rounding", d - avail - per_game, w)
    acc.tolerance += 2 * TOLERANCE_PER_NUMBER
    pv0, pv1 = _participation_version(b_run, b_row), _participation_version(a_run, a_row)
    if pv0 != pv1:
        reason = f"availability model {pv0 or 'unknown'} → {pv1 or 'unknown'}"
        acc.model_reasons.setdefault(reason, []).append(w)
        acc.part("model:availability", {"key": "model:availability", "kind": "model", "exact": True,
                                        "label": "Availability model changed", "unit": "points"}, avail, w)
    else:
        p = acc.part("availability", {"key": "availability", "kind": "availability", "exact": True,
                                      "label": "Chance he plays", "unit": "share"}, avail, w)
        p["_before"].append((p0, None))
        p["_after"].append((p1, None))
    eb, ea = _explanation(b_row), _explanation(a_row)
    changes = model_changes(b_run, a_run)
    if changes:
        for r in changes:
            acc.model_reasons.setdefault(r, []).append(w)
        acc.part("model:points", {"key": "model:points", "kind": "model", "exact": True,
                                  "label": "Model changed", "unit": "points"}, per_game, w)
        for col in sorted(set(eb) | set(ea)):
            acc.recorded(col, eb.get(col), ea.get(col), w)
        return
    if x0 <= 0 or x1 <= 0:
        acc.resid("floored", per_game, w)
        return
    if not eb or not ea:
        acc.resid("no_inputs", per_game, w)
        return
    explained = 0.0
    for col in sorted(set(eb) & set(ea)):
        c = p1 * (float(ea[col]["contribution"]) - float(eb[col]["contribution"]))
        explained += c
        acc.tolerance += 2 * TOLERANCE_PER_NUMBER * p1
        p = acc.part(f"input:{col}", {"key": f"input:{col}", "kind": "input", "feature": col,
                                      "label": feature_label(col), "unit": feature_unit(col),
                                      "exact": position != "QB"}, c, w)
        p["_before"].append(_value(eb[col]))
        p["_after"].append(_value(ea[col]))
        acc.recorded(col, eb[col], ea[col], w)
    one_sided = sorted(set(eb) ^ set(ea))
    for col in one_sided:
        acc.recorded(col, eb.get(col), ea.get(col), w)
    if position == "QB":
        acc.approximate = True
    acc.resid("one_sided" if one_sided else "unstored", per_game - explained, w)


def decompose(mover: dict, window: list[int], before_runs: dict, after_runs: dict, rows: dict,
              first_kickoffs: dict) -> dict:
    """The attribution block of one Fantasy Intel mover. `before_runs` / `after_runs`: {week: run meta};
    `rows`: {(run_id, espn_player_id): row}; `first_kickoffs`: {week: aware datetime}. Pure: same input, same output."""
    pid, position = int(mover["espn_player_id"]), mover.get("position") or ""
    acc = _Acc()
    weeks = {int(w["week"]): w for w in mover.get("weeks") or []}
    for w in sorted(weeks):
        if w not in window:
            continue
        b_run, a_run = before_runs.get(w), after_runs.get(w)
        sides = {"before": (b_run, rows.get((b_run or {}).get("run_id"), {}).get(pid) if b_run else None),
                 "after": (a_run, rows.get((a_run or {}).get("run_id"), {}).get(pid) if a_run else None)}
        _week(acc, w, weeks[w], position, sides, first_kickoffs.get(w))
    return _finish(acc, mover, before_runs, after_runs)


def _side_meta(runs: dict, window: Iterable[int]) -> dict:
    used = [runs[w] for w in sorted(window) if w in runs]
    created = [c for c in (_utc(r.get("created_at")) for r in used) if c is not None]
    through = sorted({(r.get("data_through_season"), r.get("data_through_week")) for r in used
                      if r.get("data_through_week") is not None and r.get("data_through_season") is not None})
    cutoffs = sorted({int(r["week"] if r.get("projected_from_week") is None else r["projected_from_week"]) for r in used})
    return {"created_at": _iso(max(created)) if created else None,
            "data_through": None if not through else {"season": through[-1][0], "week": through[-1][1]},
            "cutoff_week": cutoffs[-1] if cutoffs else None,
            "model_versions": sorted({str(r.get("model_version")) for r in used if r.get("model_version")}),
            "artifact_ids": sorted({str(r.get("artifact_id")) for r in used if r.get("artifact_id")}),
            "run_ids": list(dict.fromkeys(str(r["run_id"]) for r in used))}


def _finish(acc: _Acc, mover: dict, before_runs: dict, after_runs: dict) -> dict:
    total = round(float(mover.get("delta") or 0.0), 2)
    parts = sorted((_public_part(p) for p in acc.parts.values()), key=lambda p: (-abs(p["points"]), p["key"]))
    shown = round(sum(p["points"] for p in parts), 2)
    residual_points = round(total - shown, 2)
    reasons = sorted(({**r, "points": round(r["points"], 2), "weeks": sorted(r["weeks"])}
                      for r in acc.residual.values()), key=lambda r: (-abs(r["points"]), r["key"]))
    # the rounding line absorbs the cents the per-part rounding leaves, so the reasons add up to the residual shown
    gap = round(residual_points - sum(r["points"] for r in reasons), 2)
    if abs(gap) >= 0.005:
        rnd = next((r for r in reasons if r["key"] == "rounding"), None)
        if rnd is None:
            rnd = {"key": "rounding", "label": RESIDUAL["rounding"], "points": 0.0, "weeks": []}
            reasons.append(rnd)
        rnd["points"] = round(rnd["points"] + gap, 2)
    reasons = [r for r in reasons if abs(r["points"]) >= 0.005 or r["key"] in ("as_of", "missing_row", "no_inputs")]
    tolerance = round(acc.tolerance + TOLERANCE_PER_NUMBER * 2, 2)
    model_changed = any(p["kind"] == "model" for p in parts)
    if not parts and abs(residual_points) >= 0.005:
        status = "not_decomposed"
    elif model_changed:
        status = "model_changed"
    elif abs(residual_points) <= tolerance:
        status = "explained"
    else:
        status = "partial"
    window = sorted(set(before_runs) | set(after_runs))
    inputs = sorted((_public_part({**r, "points": 0.0}) for r in acc.inputs.values()),
                    key=lambda r: (r["label"], r["feature"]))
    for r in inputs:
        r.pop("points", None)
    return {
        "contract": CONTRACT, "status": status, "source_label": OURS_LABEL,
        "method": "model_changed" if model_changed else ("approximate" if acc.approximate else "exact_linear"),
        "method_note": METHOD_MODEL_CHANGED if model_changed else METHOD,
        "approximation_note": QB_NOTE if acc.approximate else None,
        "total": total, "parts": parts,
        "residual": {"points": residual_points, "reasons": reasons},
        "reconciles": abs(shown + residual_points - total) < 0.005, "tolerance": tolerance,
        "inputs": inputs,
        "missing_inputs": {side: [{"text": t, "weeks": sorted(set(ws))} for t, ws in sorted(m.items())]
                           for side, m in acc.missing.items()},
        "model": {"changed": model_changed,
                  "reasons": [{"text": t, "weeks": sorted(set(ws))} for t, ws in sorted(acc.model_reasons.items())]},
        "as_of": {"before": _side_meta(before_runs, window), "after": _side_meta(after_runs, window),
                  "refused": sorted(acc.refused, key=lambda r: (r["week"], r["forecast"]))},
        "reason": None,
    }


def unavailable(reason: str, label: str) -> dict:
    return {"contract": CONTRACT, "status": "unavailable", "source_label": label, "method": None, "method_note": None,
            "approximation_note": None, "total": None, "parts": [], "residual": None, "reconciles": None,
            "tolerance": None, "inputs": [], "missing_inputs": {"before": [], "after": []},
            "model": {"changed": False, "reasons": []}, "as_of": None, "reason": reason}


# ----------------------------------------------------------------------------- reading and attaching


def _run_ids(snap: dict | None) -> dict[int, str]:
    ids = (snap or {}).get("run_ids") or {}
    return {int(w): str(r) for w, r in ids.items()} if isinstance(ids, dict) else {}


def _load(con, run_ids: list[str], pids: list[int]) -> tuple[dict, dict]:
    """(runs {run_id: meta}, rows {run_id: {pid: row}}): two queries, whatever the number of movers."""
    if not run_ids or not table_exists(con, pf.RUNS) or not table_exists(con, pf.ROWS):
        return {}, {}
    runs = {r["run_id"]: r for r in fetch_dicts(con, f"SELECT * FROM {pf.RUNS} WHERE run_id IN (SELECT unnest(?))",
                                                [run_ids])}
    have = table_columns(con, pf.ROWS)
    cols = [c for c in ("run_id", "espn_player_id", "position", "coverage_status", "p_play", "projected_points",
                        "expected_points", "explanation", "missing_inputs", "participation", "kickoff") if c in have]
    rows: dict[str, dict[int, dict]] = {}
    if pids:
        for r in fetch_dicts(con, f"SELECT {', '.join(cols)} FROM {pf.ROWS} WHERE run_id IN (SELECT unnest(?)) "
                                  "AND espn_player_id IN (SELECT unnest(?))", [run_ids, pids]):
            rows.setdefault(r["run_id"], {})[int(r["espn_player_id"])] = r
    return runs, rows


def _targets(result: dict, player: int | None) -> list[tuple[str, int]]:
    """(list name, index) of every mover that gets a block: the risers, the fallers and the requested player."""
    out = [(key, i) for key in ("risers", "fallers") for i, _ in enumerate(result.get(key) or [])]
    if player is not None:
        out += [(key, i) for key in ("risers", "fallers", "players") for i, m in enumerate(result.get(key) or [])
                if m.get("espn_player_id") == player and (key, i) not in out]
    return out


def attach(con, season: int, result: dict, player: int | None = None) -> dict:
    """Set `attribution` on every listed riser and faller and on the requested `player` wherever he appears (module
    docstring). Other movers keep None. An unavailable result is returned unchanged."""
    if not result.get("available"):
        return result
    targets = _targets(result, player)
    if not targets:
        return result
    window = [int(w) for w in (result.get("window") or {}).get("weeks") or []]
    if result.get("source") != INDEPENDENT:
        blocks = {}
    else:
        before_ids, after_ids = _run_ids(result.get("baseline")), _run_ids(result.get("current"))
        ids = sorted({r for w, r in before_ids.items() if w in window} | {r for w, r in after_ids.items() if w in window})
        pids = sorted({int(result[k][i]["espn_player_id"]) for k, i in targets})
        runs, rows = _load(con, ids, pids)
        before_runs = {w: runs[r] for w, r in before_ids.items() if w in window and r in runs}
        after_runs = {w: runs[r] for w, r in after_ids.items() if w in window and r in runs}
        first = pf.first_kickoffs(con, season)
        blocks = {pid: None for pid in pids}
        for k, i in targets:
            m = result[k][i]
            pid = int(m["espn_player_id"])
            if blocks[pid] is None:
                blocks[pid] = decompose(m, window, before_runs, after_runs, rows, first)
    # `players` shares its dicts with the two lists: copy before setting, so the other entries keep None
    shared = {id(m) for m in result.get("players") or []} | {id(m) for m in result.get("risers") or []} \
        | {id(m) for m in result.get("fallers") or []}
    for k, i in targets:
        m = dict(result[k][i]) if id(result[k][i]) in shared else result[k][i]
        result[k][i] = m
        m["attribution"] = (blocks.get(int(m["espn_player_id"])) if result.get("source") == INDEPENDENT
                            else unavailable(ESPN_REASON, ESPN_LABEL))
    return result
