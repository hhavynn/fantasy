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

Full inputs (contract `projection-attribution/v2`, additive). A run stored with its forecast inputs
(production_forecasts "forecast inputs") keeps each projected row's whole design row and the model it went through.
When BOTH compared rows have inputs that verify (digests match and they rebuild the stored point), the per-game change
is split exactly, even across a retrain or a re-score. Write each point as y = s * L + t with L = a + sum_i w_i * v_i
before the floor (w = coef / std, a = intercept - sum_i w_i * mean_i; s and t the league re-score factor or shift, 1 and
0 otherwise; a QB start/no-start mixture is L at the start share when neither scenario is floored). With the two-factor
midpoint (Shapley) convention, exact and independent of which forecast comes first:

  input i       p1 * s_mid * w_mid_i * (v1_i - v0_i)        a column only one model has changes its model term only
  model         p1 * s_mid * ((a1 - a0) + sum_i v_mid_i * (w1_i - w0_i))
  re-scoring    p1 * (L_mid * (s1 - s0) + (t1 - t0))

where x_mid is the mean of the two forecasts' values. An input unknown in either forecast (imputed with the training
average) is not a measurement: its term is residual ("imputed"). A floored row is not split. The parts plus the
stored-value rounding add up to p1 * (x1 - x0). Without verified inputs on both sides the week is split exactly as
under v1, and a block that used no inputs is byte-identical to v1.
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
CONTRACT_V2 = "projection-attribution/v2"          # a block that split at least one week with the full stored inputs
SMALL_PART = 0.05            # full-input parts under this many points are shown together as one line
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
METHOD_V2 = ("Both forecasts stored their full model inputs and model, so the per-game change is split exactly with "
             "the two-factor midpoint (Shapley) convention: each input's change is weighted by the average of the two "
             "models' weights, and the model's own change (retrained coefficients and intercept) is taken at the "
             "average of the two forecasts' inputs. Swapping the two forecasts negates every part; nothing is assigned "
             "by the order they were made in. Inputs unknown when a forecast was made stay in the residual.")
AVAILABILITY_UNSPLIT = ("The chance-to-play model or method also differs between the two forecasts, so that part is "
                        "shown as a model change and not split.")
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
    "imputed": "Inputs unknown when a forecast was made (filled with the training average): not measured changes",
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


# Stored markers of a step applied on top of the participation model's P(plays): the frozen QB calibration map
# (context_model.calibrate_rows, horizon 0 only), the availability news (the live run store), and a median
# borrowed from the run when the player had no estimate of his own. A change in any of them is a change of method.
PARTICIPATION_MODIFIERS = (("calibration", "calibration"), ("availability_news", "availability news"),
                           ("base_p_play_source", "borrowed estimate"))


def participation_method(run: dict, row: dict) -> str | None:
    """The method behind a row's chance to play: the participation model (row-level version, else the run's) plus
    every stored modifier. Two rows compare as measured availability only when this is equal."""
    q = _json(row.get("participation"))
    q = q if isinstance(q, dict) else {}
    base = str(q["model_version"]) if q.get("model_version") else run.get("participation_version")
    mods = [f"{label} {q[key]}" if key != "base_p_play_source" else label
            for key, label in PARTICIPATION_MODIFIERS if q.get(key)]
    if base is None and not mods:
        return None
    return " + ".join([base or "unknown model", *mods])


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
        self.full_weeks: list[int] = []     # weeks split with the full stored inputs (v2)

    def part(self, key: str, base: dict, points: float, week: int) -> dict:
        p = self.parts.setdefault(key, {**base, "points": 0.0, "weeks": [], "_before": [], "_after": []})
        # one part over weeks split in different ways: exact only if every week was, folded only if every week was full
        p["exact"] = bool(p.get("exact", True)) and bool(base.get("exact", True))
        if p.get("_full") and not base.get("_full"):
            p["_full"] = False
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

    def recorded_values(self, col: str, before: tuple, after: tuple, week: int) -> None:
        r = self.inputs.setdefault(col, {"feature": col, "label": feature_label(col), "unit": feature_unit(col),
                                         "weeks": [], "_before": [], "_after": []})
        r["weeks"].append(week)
        r["_before"].append(before)
        r["_after"].append(after)


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


def _side_values(model: dict, rebuilt: dict, imputed: list) -> dict[str, tuple[float, float, float, bool]]:
    """{column: (raw weight, value, training mean, imputed)} of one verified forecast."""
    return {str(c): (w, v, float(m), bool(i))
            for c, w, v, m, i in zip(model["columns"], rebuilt["w"], rebuilt["values"], model["means"], imputed)}


def _full_split(acc: _Acc, w: int, p1: float, per_game: float, vb: dict, va: dict) -> None:
    """The per-game change of one week split with both forecasts' full stored inputs (module docstring, v2).

    An input unknown in either forecast is kept in its centred form w * (v - mean) on both sides (0 where it was
    imputed: the training mean), and the whole change of that term is residual ("imputed"): neither an input part nor,
    through the model constant's w * mean, a model part."""
    rb, ra = vb["rebuilt"], va["rebuilt"]
    acc.full_weeks.append(w)
    if rb["floored"] or ra["floored"]:
        acc.resid("floored", per_game, w)
        return
    s0, s1, t0, t1 = rb["scale"], ra["scale"], rb["shift"], ra["shift"]
    l0, l1 = rb["linear"], ra["linear"]
    s_mid, l_mid = (s0 + s1) / 2, (l0 + l1) / 2
    c0 = _side_values(vb["model"], rb, vb["inputs"]["imputed"])
    c1 = _side_values(va["model"], ra, va["inputs"]["imputed"])
    same_model = vb["model"].get("model_sha256") == va["model"].get("model_sha256")
    unknown = {c for c in set(c0) | set(c1) if (c0.get(c) or (0, 0, 0, False))[3] or (c1.get(c) or (0, 0, 0, False))[3]}
    a0 = rb["a"] + sum(c0[c][0] * c0[c][2] for c in unknown if c in c0)       # the constant without those columns'
    a1 = ra["a"] + sum(c1[c][0] * c1[c][2] for c in unknown if c in c1)       # w * mean: they are centred below
    model_term = a1 - a0
    imputed = 0.0
    for col in sorted(set(c0) | set(c1)):
        if col in unknown:
            t_0 = c0[col][0] * (c0[col][1] - c0[col][2]) if col in c0 else 0.0
            t_1 = c1[col][0] * (c1[col][1] - c1[col][2]) if col in c1 else 0.0
            imputed += p1 * s_mid * (t_1 - t_0)
            acc.recorded_values(col, (None, IMPUTED) if col in c0 and c0[col][3] else
                                ((c0[col][1], None) if col in c0 else (None, NOT_STORED)),
                                (None, IMPUTED) if col in c1 and c1[col][3] else
                                ((c1[col][1], None) if col in c1 else (None, NOT_STORED)), w)
            continue
        w0, v0, _, _ = c0.get(col, (0.0, None, 0.0, False))
        w1, v1, _, _ = c1.get(col, (0.0, None, 0.0, False))
        v0 = v1 if v0 is None else v0            # a column one model lacks: no input change, a model change only
        v1 = v0 if v1 is None else v1
        model_term += (v0 + v1) / 2 * (w1 - w0)
        if col not in c0 or col not in c1:
            continue
        p = acc.part(f"input:{col}", {"key": f"input:{col}", "kind": "input", "feature": col,
                                      "label": feature_label(col), "unit": feature_unit(col), "exact": True,
                                      "_full": True}, p1 * s_mid * (w0 + w1) / 2 * (v1 - v0), w)
        p["_before"].append((v0, None))
        p["_after"].append((v1, None))
    if not same_model and abs(model_term) > 1e-12:
        acc.part("model:coefficients", {"key": "model:coefficients", "kind": "model", "exact": True, "measured": True,
                                        "label": "Model retrained (its weights and intercept)", "unit": "points"},
                 p1 * s_mid * model_term, w)
    rescoring = p1 * (l_mid * (s1 - s0) + (t1 - t0))
    if abs(rescoring) >= 1e-9:
        acc.part("model:rescoring", {"key": "model:rescoring", "kind": "model", "exact": True, "measured": True,
                                     "label": "League re-scoring factor changed", "unit": "points"}, rescoring, w)
    if imputed:
        acc.resid("imputed", imputed, w)
    y0, y1 = s0 * l0 + t0, s1 * l1 + t1
    acc.resid("rounding", per_game - p1 * (y1 - y0), w)
    acc.tolerance += TOLERANCE_PER_NUMBER * p1 * (2 + abs(s0) * (s0 != 1.0) + abs(s1) * (s1 != 1.0))


def _week(acc: _Acc, w: int, wk: dict, position: str, sides: dict, first_kickoff: datetime | None,
          full: tuple | None = None) -> None:
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
    pv0, pv1 = participation_method(b_run, b_row), participation_method(a_run, a_row)
    if pv0 != pv1:
        reason = f"availability method {pv0 or 'unknown'} → {pv1 or 'unknown'}"
        acc.model_reasons.setdefault(reason, []).append(w)
        acc.part("model:availability", {"key": "model:availability", "kind": "model", "exact": True,
                                        "label": "Availability model changed", "unit": "points"}, avail, w)
    else:
        p = acc.part("availability", {"key": "availability", "kind": "availability", "exact": True,
                                      "label": "Chance he plays", "unit": "share"}, avail, w)
        p["_before"].append((p0, None))
        p["_after"].append((p1, None))
    if full is not None:
        _full_split(acc, w, p1, per_game, *full)
        return
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
              first_kickoffs: dict, inputs: dict | None = None) -> dict:
    """The attribution block of one Fantasy Intel mover. `before_runs` / `after_runs`: {week: run meta};
    `rows`: {(run_id, espn_player_id): row}; `first_kickoffs`: {week: aware datetime}; `inputs`: the verified forecast
    inputs {(run_id, espn_player_id): {model, inputs, rebuilt}} (production_forecasts.verified_inputs), None or {} =
    none. Pure: same input, same output."""
    pid, position = int(mover["espn_player_id"]), mover.get("position") or ""
    acc = _Acc()
    weeks = {int(w["week"]): w for w in mover.get("weeks") or []}
    for w in sorted(weeks):
        if w not in window:
            continue
        b_run, a_run = before_runs.get(w), after_runs.get(w)
        sides = {"before": (b_run, rows.get((b_run or {}).get("run_id"), {}).get(pid) if b_run else None),
                 "after": (a_run, rows.get((a_run or {}).get("run_id"), {}).get(pid) if a_run else None)}
        vb = (inputs or {}).get(((b_run or {}).get("run_id"), pid))
        va = (inputs or {}).get(((a_run or {}).get("run_id"), pid))
        _week(acc, w, weeks[w], position, sides, first_kickoffs.get(w), (vb, va) if vb and va else None)
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


def _fold_small(parts: dict[str, dict]) -> dict[str, dict]:
    """Full-input parts under SMALL_PART points shown as one line (their exact sum), so a model with dozens of inputs
    stays readable; nothing is dropped."""
    # an input that did not move contributes nothing and is not a line at all
    parts = {k: p for k, p in parts.items() if not (p.get("_full") and abs(p["points"]) < 1e-9)}
    small = [p for p in parts.values() if p.get("_full") and abs(p["points"]) < SMALL_PART]
    if len(small) < 2:
        return parts
    out = {k: p for k, p in parts.items() if not (p.get("_full") and abs(p["points"]) < SMALL_PART)}
    out["inputs:small"] = {"key": "inputs:small", "kind": "input", "exact": True,
                           "label": f"{len(small)} other inputs, each under {SMALL_PART:.2f} points",
                           "unit": "points", "points": sum(p["points"] for p in small),
                           "weeks": sorted({w for p in small for w in p["weeks"]}),
                           "features": sorted(p["feature"] for p in small),
                           "feature_labels": [feature_label(f) for f in sorted(p["feature"] for p in small)]}
    return out


def _finish(acc: _Acc, mover: dict, before_runs: dict, after_runs: dict) -> dict:
    total = round(float(mover.get("delta") or 0.0), 2)
    full = bool(acc.full_weeks)
    raw = _fold_small(acc.parts) if full else acc.parts
    parts = sorted((_public_part(p) for p in raw.values()), key=lambda p: (-abs(p["points"]), p["key"]))
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
    model_changed = any(p["kind"] == "model" and not p.get("measured") for p in parts)
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
    method = "model_changed" if model_changed else ("approximate" if acc.approximate else "exact_linear")
    note = METHOD_MODEL_CHANGED if model_changed else METHOD
    # a full-input block whose only unsplit model part is the chance to play still split the points model exactly
    points_unsplit = any(p["kind"] == "model" and not p.get("measured") and p["key"] != "model:availability"
                         for p in parts)
    if full and not points_unsplit:
        method = "approximate" if acc.approximate else "exact_midpoint"
        note = METHOD_V2 + (" " + AVAILABILITY_UNSPLIT if model_changed else "")
    block = {
        "contract": CONTRACT_V2 if full else CONTRACT, "status": status, "source_label": OURS_LABEL,
        "method": method, "method_note": note,
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
    if full:
        block["full_inputs"] = {"weeks": sorted(set(acc.full_weeks)), "convention": "two-factor midpoint (Shapley)"}
    return block


def unavailable(reason: str, label: str) -> dict:
    return {"contract": CONTRACT, "status": "unavailable", "source_label": label, "method": None, "method_note": None,
            "approximation_note": None, "total": None, "parts": [], "residual": None, "reconciles": None,
            "tolerance": None, "inputs": [], "missing_inputs": {"before": [], "after": []},
            "model": {"changed": False, "reasons": []}, "as_of": None, "reason": reason}


# ----------------------------------------------------------------------------- reading and attaching


def _run_ids(snap: dict | None) -> dict[int, str]:
    ids = (snap or {}).get("run_ids") or {}
    return {int(w): str(r) for w, r in ids.items()} if isinstance(ids, dict) else {}


def _load(con, run_ids: list[str], pids: list[int]) -> tuple[dict, dict, dict]:
    """(runs {run_id: meta}, rows {run_id: {pid: row}}, verified inputs {(run_id, pid): ...}): four queries, whatever
    the number of movers."""
    if not run_ids or not table_exists(con, pf.RUNS) or not table_exists(con, pf.ROWS):
        return {}, {}, {}
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
    return runs, rows, pf.verified_inputs(con, run_ids, pids, rows, runs)


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
        runs, rows, inputs = _load(con, ids, pids)
        before_runs = {w: runs[r] for w, r in before_ids.items() if w in window and r in runs}
        after_runs = {w: runs[r] for w, r in after_ids.items() if w in window and r in runs}
        first = pf.first_kickoffs(con, season)
        blocks = {pid: None for pid in pids}
        for k, i in targets:
            m = result[k][i]
            pid = int(m["espn_player_id"])
            if blocks[pid] is None:
                blocks[pid] = decompose(m, window, before_runs, after_runs, rows, first, inputs)
    # `players` shares its dicts with the two lists: copy before setting, so the other entries keep None
    shared = {id(m) for m in result.get("players") or []} | {id(m) for m in result.get("risers") or []} \
        | {id(m) for m in result.get("fallers") or []}
    for k, i in targets:
        m = dict(result[k][i]) if id(result[k][i]) in shared else result[k][i]
        result[k][i] = m
        m["attribution"] = (blocks.get(int(m["espn_player_id"])) if result.get("source") == INDEPENDENT
                            else unavailable(ESPN_REASON, ESPN_LABEL))
    return result
