"""A league's Ours, kicker and D/ST runs, derived from the reference runs in the global file (multi-league Phase 4).

The models train on the laptop in the reference scoring (the laptop's league). `app/db/league_split.py` ships their
sealed runs to every league build as `ref_*` tables, with the reference scoring items. `apply_reference(con, season)`
turns them into the league's own `independent_projection_*`, `kicker_projection_*` and `dst_projection_*` rows, the
tables every consumer reads:

  * a run whose scoring fingerprint is the league's is copied verbatim (same run_id, rows and content_sha256): the
    reference league gets exactly the laptop's runs;
  * a skill run under another supported scoring is RE-SCORED, not retrained: every projected row is scaled by
    f = mix_points(league) / mix_points(reference), where the mix is the player's average box score over his last 8
    regular-season games up to the run's data cutoff (his position's average mix when he has fewer than 3 games).
    The same f scales the interval and the explanation's contributions. A reference mix worth less than 1 point
    shifts instead (+ the difference, never below 0). A box-score component missing (NULL) in any of the player's
    games is unknown in his mix, never 0; when either scoring weights it the row is not estimated
    (`insufficient_data`, MISSING_COMPONENT_FLAG) and consumers fall back to the provider's number, labelled as such.
    A position's mix averages each component over the players who have it;
  * a K or D/ST run is re-scored from each row's expected statistics (`stat_line.scoring_stats`), exact because both
    scorings are linear in them; a run stored before those were recorded, or whose rows lack a statistic the league
    scores, is not re-scored;
  * a scoring a family's resolver does not support gets no runs of that family (consumers show the provider's
    numbers, as for any unsupported scoring).

Every derived run is sealed with its store's own `content_sha256`, keeps its artifact_id (the approved artifact made
it) and says what it was derived from in `evaluation.rescored_from`. Runs whose reference weights are unknown (a run
under an older reference scoring) are skipped. Projection ranges are calibrated in the reference scoring: copied only
on a fingerprint match. Owner research (performance reports, replays) is the laptop league's own and not shipped.

SQL + stdlib only (the runner and the API share it; no numpy).
"""

from __future__ import annotations

import json
from typing import Iterable

from ..db.connection import fetch_dicts, insert_rows, table_exists
from . import dst_store, kicker_store
from . import production_forecasts as pf
from .kicker_scoring import expected_points as kicker_points
from .kicker_scoring import league_items, resolve_kicker_scoring
from .league_scoring import MIX_COLUMNS, SkillScoring, points, resolve_skill_scoring

MIX_GAMES = 8
MIX_MIN_GAMES = 3
SHIFT_BELOW = 1.0            # reference mix points under this: additive shift instead of a ratio
RESCORED_FLAG = "re-scored to this league's scoring"
NO_MIX_FLAG = "no box-score mix to re-score from"
MISSING_COMPONENT_FLAG = "a scored box-score component is missing from the mix"
SHIFTED_FLAG = "explanation contributions are in the reference scoring"

SKILL = ("independent_projection_runs", "independent_projection_rows")
# a skill run's full model inputs (production_forecasts "forecast inputs"): copied with a copied run; a re-scored run
# keeps the reference inputs and records its per-row factor (or shift), so its points still rebuild from them
SKILL_INPUTS = (pf.MODELS, pf.INPUTS)
KICKER = ("kicker_projection_runs", "kicker_projection_rows")
DST = ("dst_projection_runs", "dst_projection_rows")
# calibrated in the reference scoring: copied only when the league's skill scoring is the reference's
RANGES = ("projection_range_quantiles",)
DERIVED_TABLES = frozenset((*SKILL, *SKILL_INPUTS, *KICKER, *DST, *RANGES))


def _json(v):
    return json.loads(v) if isinstance(v, (str, bytes, bytearray)) else v


def _ref(t: str) -> str:
    return "ref_" + t


# ----------------------------------------------------------------------------- skill players


def stat_mix(con, through: tuple[int, int]) -> dict[str, dict[str, float | None]]:
    """gsis_id -> his per-game average of every component column over his last MIX_GAMES regular-season games up to
    `through` = (season, week) inclusive, current and previous season; `__pos__<POS>` -> the position's average mix
    over players with at least MIX_MIN_GAMES games. A player below MIX_MIN_GAMES gets his position's mix.

    Missing is never zero: a component that is NULL in any of the player's games is None in his mix (unknown), and a
    position's component is the average over the players who have it (None when none does)."""
    season, week = through
    have = {r["column_name"] for r in fetch_dicts(
        con, "SELECT column_name FROM duckdb_columns() WHERE table_name = 'nfl_weekly_stats'")}
    cols = [c for c in MIX_COLUMNS if c in have]
    if not cols:
        return {}
    # count(c) counts the games where c is known: the average is used only when every game in the window has it
    avg = ", ".join(f"CASE WHEN count({c}) = count(*) THEN avg({c}) END AS {c}" for c in cols)
    rows = fetch_dicts(con, f"""
        WITH g AS (
            SELECT player_id AS gsis_id, CASE WHEN position = 'FB' THEN 'RB' ELSE position END AS position,
                   {", ".join(cols)},
                   row_number() OVER (PARTITION BY player_id ORDER BY season DESC, week DESC) AS rn
            FROM nfl_weekly_stats
            WHERE season_type = 'REG' AND season IN (?, ?) AND season * 100 + week <= ?
              AND position IN ('QB', 'RB', 'WR', 'TE', 'FB'))
        SELECT gsis_id, any_value(position) AS position, count(*) AS games, {avg}
        FROM g WHERE rn <= ? GROUP BY gsis_id""", [season, season - 1, season * 100 + week, MIX_GAMES])
    mix: dict[str, dict[str, float]] = {}
    by_pos: dict[str, list[dict]] = {}
    for r in rows:
        if r["games"] >= MIX_MIN_GAMES:
            m = {c: None if r[c] is None else float(r[c]) for c in cols}
            mix[r["gsis_id"]] = m
            by_pos.setdefault(r["position"], []).append(m)
    for pos, ms in by_pos.items():
        known = {c: [m[c] for m in ms if m[c] is not None] for c in cols}
        mix["__pos__" + pos] = {c: sum(v) / len(v) if v else None for c, v in known.items()}
    return mix


def _scale(v, f):
    return None if v is None else round(v * f, 2)


def rescore_skill_rows(rows: Iterable[dict], mix: dict, ref: SkillScoring, target: SkillScoring,
                       factors: dict | None = None) -> list[dict]:
    """The rows of one skill run re-scored from `ref` to `target` (module docstring). Rows that are not projected
    (bye, withheld, no estimate) are unchanged. A row whose mix lacks a component either scoring weights is not
    estimated (MISSING_COMPONENT_FLAG); a row without any mix gets NO_MIX_FLAG. `factors`, when given, receives
    {espn_player_id: {"scale": f} | {"shift": d}} for every re-scored row."""
    out = []
    for r in rows:
        if r["coverage_status"] != "projected" or r["projected_points"] is None:
            out.append(dict(r))
            continue
        flags = list(_json(r["flags"]) or [])
        m = mix.get(r["gsis_id"]) or mix.get("__pos__" + str(r["position"]))
        a = None if m is None else points(m, ref, r["position"])
        b = None if m is None else points(m, target, r["position"])
        if a is None or b is None:
            out.append({**r, "coverage_status": "insufficient_data", "projected_points": None, "interval_low": None,
                        "interval_high": None, "expected_points": None,
                        "flags": [*flags, NO_MIX_FLAG if m is None else MISSING_COMPONENT_FLAG]})
            continue
        expl = _json(r["explanation"])
        if a >= SHIFT_BELOW:
            f = b / a
            new = {k: _scale(r[k], f) for k in ("projected_points", "interval_low", "interval_high")}
            if f < 0 and new["interval_low"] is not None and new["interval_high"] is not None:
                # a negative factor (a scoring whose typical game is worth negative points) flips the range
                new["interval_low"], new["interval_high"] = new["interval_high"], new["interval_low"]
            expl = None if expl is None else [{**e, "contribution": _scale(e.get("contribution"), f)}
                                               if isinstance(e, dict) else e for e in expl]
            if factors is not None:
                factors[int(r["espn_player_id"])] = {"scale": f}
        else:
            d = b - a
            new = {k: None if r[k] is None else round(max(r[k] + d, 0.0), 2)
                   for k in ("projected_points", "interval_low", "interval_high")}
            flags.append(SHIFTED_FLAG)
            if factors is not None:
                factors[int(r["espn_player_id"])] = {"shift": d}
        p_play = r.get("p_play")
        out.append({**r, **new, "explanation": expl, "flags": [*flags, RESCORED_FLAG],
                    "expected_points": None if p_play is None else round(new["projected_points"] * p_play, 2)})
    return out


# ----------------------------------------------------------------------------- K and D/ST


def _rescore_expected(rows: Iterable[dict], score) -> list[dict] | None:
    """Rows re-scored by `score(scoring_stats) -> points`; None when a projected row has no scoring_stats."""
    out = []
    for r in rows:
        if r["coverage_status"] != "projected" or r["projected_points"] is None:
            out.append(dict(r))
            continue
        line = _json(r["stat_line"]) or {}
        if "scoring_stats" not in line:
            return None
        pts = score(line["scoring_stats"])
        if pts is None:
            return None
        pts = round(pts, 2)
        old = r["projected_points"]
        shift = {k: None if r.get(k) is None else round(max(r[k] + pts - old, 0.0), 2)
                 for k in ("interval_low", "interval_high")}
        p_play = r.get("p_play")
        exp = (round(pts * p_play, 2) if p_play is not None
               else pts if r.get("expected_points") is not None else None)
        out.append({**r, **shift, "projected_points": pts, "expected_points": exp,
                    "flags": [*(_json(r["flags"]) or []), RESCORED_FLAG]})
    return out


def rescore_kicker_rows(rows: Iterable[dict], target) -> list[dict] | None:
    return _rescore_expected(rows, lambda s: kicker_points(target, s))


def _dst_points(weights: dict[int, float], scoring_stats: dict) -> float | None:
    """None when the row lacks the expectation of a statistic the league scores (a run stored before that statistic
    was recorded, e.g. another provider's points-allowed tiers): missing is never zero."""
    if any(scoring_stats.get(str(sid), scoring_stats.get(sid)) is None for sid in weights):
        return None
    return dst_store.expected_points(weights, scoring_stats)


def rescore_dst_rows(rows: Iterable[dict], weights: dict[int, float]) -> list[dict] | None:
    return _rescore_expected(rows, lambda s: _dst_points(weights, s))


# ----------------------------------------------------------------------------- apply


def _copy(con, table: str, where: str = "", params: list | None = None) -> int:
    if not table_exists(con, table):      # created at runtime by its own module (projection ranges): same columns
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM {_ref(table)} WHERE false")
    return con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {_ref(table)} {where}", params or []).fetchone()[0]


def _derived_id(run_id: str, fp: str) -> str:
    return f"{run_id}~{fp.split(':')[-1][-8:]}"


def _derive(con, family: tuple[str, str], run: dict, rows: list[dict], fp: str, *, method: str,
            digest, scoring_meta: dict | None = None) -> str:
    runs_t, rows_t = family
    rid = _derived_id(run["run_id"], fp)
    meta = {**run, "run_id": rid, "scoring_fingerprint": fp,
            "evaluation": {**(_json(run.get("evaluation")) or {}),
                           "rescored_from": {"run_id": run["run_id"], "scoring_fingerprint": run["scoring_fingerprint"],
                                             "method": method}}}
    if scoring_meta is not None:
        meta["scoring"] = scoring_meta
    stamped = [{**r, "run_id": rid} for r in rows]
    meta["content_sha256"] = digest(meta, stamped)
    meta["row_count"] = len(stamped)
    insert_rows(con, runs_t, [meta])
    insert_rows(con, rows_t, stamped)
    return rid


def _derive_inputs(con, ref_run_id: str, rid: str, factors: dict, fp: str) -> int:
    """The reference run's input rows under the derived run id, each with its row's re-score factor or shift (rows
    that were not re-scored keep none). Values and models are the reference's, untouched; a row stored as changes
    against its cycle's base names that base's derived run (re-scored in the same pass)."""
    if not table_exists(con, _ref(pf.INPUTS)):
        return 0
    out = []
    for r in fetch_dicts(con, f"SELECT * FROM {_ref(pf.INPUTS)} WHERE run_id = ?", [ref_run_id]):
        fx = factors.get(int(r["espn_player_id"]))
        if fx is None or r.get("scale") is not None or r.get("shift") is not None:
            continue
        out.append({**{k: r[k] for k in pf.INPUT_COLUMNS if k not in ("run_id", "inputs_digest", "scale", "shift")},
                    "base_run_id": None if r.get("base_run_id") is None else _derived_id(r["base_run_id"], fp),
                    "scale": fx.get("scale"), "shift": fx.get("shift")})
    return pf.write_inputs(con, rid, out)


def _runs(con, family, season: int) -> list[dict]:
    return fetch_dicts(con, f"SELECT * FROM {_ref(family[0])} WHERE season = ? ORDER BY created_at, run_id", [season])


def _rows(con, family, run_id: str) -> list[dict]:
    return fetch_dicts(con, f"SELECT * FROM {_ref(family[1])} WHERE run_id = ?", [run_id])


def apply_reference(con, season: int) -> dict:
    """Replace the league's derived tables with its runs from the global file's references (module docstring).
    Returns counts per family. A working copy without references (an older global file) keeps empty tables."""
    for t in DERIVED_TABLES:
        if table_exists(con, t):
            con.execute(f"DELETE FROM {t}")
    if not table_exists(con, "ref_league_scoring"):
        return {"skipped": "no reference runs in the global file"}
    ref_row = fetch_dicts(con, "SELECT scoring_items FROM ref_league_scoring WHERE season = ?", [season])
    items = league_items(con, season)
    out: dict = {}

    # skill players
    stats = {"copied": 0, "rescored": 0, "skipped": 0}
    target = resolve_skill_scoring(items)
    ref = resolve_skill_scoring(_json(ref_row[0]["scoring_items"])) if ref_row else None
    if table_exists(con, _ref(SKILL[0])) and target.supported:
        if table_exists(con, _ref(pf.MODELS)):
            _copy(con, pf.MODELS)
        has_inputs = table_exists(con, _ref(pf.INPUTS))
        mixes: dict[tuple[int, int], dict] = {}
        for run in _runs(con, SKILL, season):
            if run["scoring_fingerprint"] == target.fingerprint:
                _copy(con, SKILL[0], "WHERE run_id = ?", [run["run_id"]])
                _copy(con, SKILL[1], "WHERE run_id = ?", [run["run_id"]])
                if has_inputs:
                    _copy(con, pf.INPUTS, "WHERE run_id = ?", [run["run_id"]])
                stats["copied"] += 1
            elif ref is not None and ref.supported and run["scoring_fingerprint"] == ref.fingerprint:
                through = (run.get("data_through_season") or season,
                           run.get("data_through_week") or max(int(run["week"]) - 1, 0))
                if through not in mixes:
                    mixes[through] = stat_mix(con, through)
                factors: dict = {}
                rows = rescore_skill_rows(_rows(con, SKILL, run["run_id"]), mixes[through], ref, target, factors)
                rid = _derive(con, SKILL, run, rows, target.fingerprint, method="stat-mix ratio",
                              digest=pf.content_sha256)
                _derive_inputs(con, run["run_id"], rid, factors, target.fingerprint)
                stats["rescored"] += 1
            else:
                stats["skipped"] += 1
    out["skill"] = stats

    # kickers
    stats = {"copied": 0, "rescored": 0, "skipped": 0}
    ks = resolve_kicker_scoring(items) if items else None
    if table_exists(con, _ref(KICKER[0])) and ks is not None and ks.supported:
        for run in _runs(con, KICKER, season):
            if run["scoring_fingerprint"] == ks.fingerprint:
                _copy(con, KICKER[0], "WHERE run_id = ?", [run["run_id"]])
                _copy(con, KICKER[1], "WHERE run_id = ?", [run["run_id"]])
                stats["copied"] += 1
                continue
            rows = rescore_kicker_rows(_rows(con, KICKER, run["run_id"]), ks)
            if rows is None:
                stats["skipped"] += 1
                continue
            _derive(con, KICKER, run, rows, ks.fingerprint, method="expected statistics",
                    digest=kicker_store.content_sha256, scoring_meta={**ks.describe(), "reconciliation": None})
            stats["rescored"] += 1
    out["kicker"] = stats

    # D/ST
    stats = {"copied": 0, "rescored": 0, "skipped": 0}
    weights, _ = dst_store.scoring_weights(items)
    fp, _ = dst_store.scoring_fingerprint(items)
    if table_exists(con, _ref(DST[0])) and weights is not None:
        for run in _runs(con, DST, season):
            if run["scoring_fingerprint"] == fp:
                _copy(con, DST[0], "WHERE run_id = ?", [run["run_id"]])
                _copy(con, DST[1], "WHERE run_id = ?", [run["run_id"]])
                stats["copied"] += 1
                continue
            rows = rescore_dst_rows(_rows(con, DST, run["run_id"]), weights)
            if rows is None:
                stats["skipped"] += 1
                continue
            _derive(con, DST, run, rows, fp, method="expected statistics", digest=dst_store.content_sha256,
                    scoring_meta=dst_store.describe_scoring(items))
            stats["rescored"] += 1
    out["dst"] = stats

    # projection ranges: calibrated in the reference scoring
    same = ref is not None and target.supported and ref.supported and ref.fingerprint == target.fingerprint
    out["ranges"] = {t: _copy(con, t) if same and table_exists(con, _ref(t)) else 0 for t in RANGES}
    return out
