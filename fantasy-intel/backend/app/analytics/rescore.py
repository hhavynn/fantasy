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
    shifts instead (+ the difference, never below 0);
  * a K or D/ST run is re-scored from each row's expected statistics (`stat_line.scoring_stats`), exact because both
    scorings are linear in them; a run stored before those were recorded is not re-scored;
  * a scoring a family's resolver does not support gets no runs of that family (consumers show the provider's
    numbers, as for any unsupported scoring).

Every derived run is sealed with its store's own `content_sha256`, keeps its artifact_id (the approved artifact made
it) and says what it was derived from in `evaluation.rescored_from`. Runs whose reference weights are unknown (a run
under an older reference scoring) are skipped. Research tables (projection ranges, performance reports, replays) are
evaluations of the reference scoring: copied only on a fingerprint match.

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
from .league_scoring import COMPONENT_COLUMNS, SkillScoring, points, resolve_skill_scoring

MIX_GAMES = 8
MIX_MIN_GAMES = 3
SHIFT_BELOW = 1.0            # reference mix points under this: additive shift instead of a ratio
RESCORED_FLAG = "re-scored to this league's scoring"
NO_MIX_FLAG = "no box-score mix to re-score from"

SKILL = ("independent_projection_runs", "independent_projection_rows")
KICKER = ("kicker_projection_runs", "kicker_projection_rows")
DST = ("dst_projection_runs", "dst_projection_rows")
# copied only when the league's skill scoring is the reference's
RESEARCH = ("projection_range_quantiles", "projection_performance_reports", "model_replays")
DERIVED_TABLES = frozenset((*SKILL, *KICKER, *DST, *RESEARCH))


def _json(v):
    return json.loads(v) if isinstance(v, (str, bytes, bytearray)) else v


def _ref(t: str) -> str:
    return "ref_" + t


# ----------------------------------------------------------------------------- skill players


def stat_mix(con, through: tuple[int, int]) -> dict[str, dict[str, float]]:
    """gsis_id -> his per-game average of every component column over his last MIX_GAMES regular-season games up to
    `through` = (season, week) inclusive, current and previous season; `__pos__<POS>` -> the position's average mix
    over players with at least MIX_MIN_GAMES games. A player below MIX_MIN_GAMES gets his position's mix."""
    season, week = through
    have = {r["column_name"] for r in fetch_dicts(
        con, "SELECT column_name FROM duckdb_columns() WHERE table_name = 'nfl_weekly_stats'")}
    cols = [c for c in COMPONENT_COLUMNS if c in have]
    if not cols:
        return {}
    avg = ", ".join(f"avg(coalesce({c}, 0)) AS {c}" for c in cols)
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
            m = {c: float(r[c]) for c in cols}
            mix[r["gsis_id"]] = m
            by_pos.setdefault(r["position"], []).append(m)
    for pos, ms in by_pos.items():
        mix["__pos__" + pos] = {c: sum(m[c] for m in ms) / len(ms) for c in cols}
    return mix


def _scale(v, f):
    return None if v is None else round(v * f, 2)


def rescore_skill_rows(rows: Iterable[dict], mix: dict, ref: SkillScoring, target: SkillScoring) -> list[dict]:
    """The rows of one skill run re-scored from `ref` to `target` (module docstring). Rows that are not projected
    (bye, withheld, no estimate) are unchanged."""
    out = []
    for r in rows:
        if r["coverage_status"] != "projected" or r["projected_points"] is None:
            out.append(dict(r))
            continue
        flags = list(_json(r["flags"]) or [])
        m = mix.get(r["gsis_id"]) or mix.get("__pos__" + str(r["position"]))
        a = None if m is None else points(m, ref)
        b = None if m is None else points(m, target)
        if a is None or b is None:
            out.append({**r, "coverage_status": "no_estimate", "projected_points": None, "interval_low": None,
                        "interval_high": None, "expected_points": None, "flags": [*flags, NO_MIX_FLAG]})
            continue
        expl = _json(r["explanation"])
        if a >= SHIFT_BELOW:
            f = b / a
            new = {k: _scale(r[k], f) for k in ("projected_points", "interval_low", "interval_high")}
            expl = None if expl is None else [{**e, "contribution": _scale(e.get("contribution"), f)}
                                               if isinstance(e, dict) else e for e in expl]
        else:
            d = b - a
            new = {k: None if r[k] is None else round(max(r[k] + d, 0.0), 2)
                   for k in ("projected_points", "interval_low", "interval_high")}
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
        pts = round(score(line["scoring_stats"]), 2)
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


def rescore_dst_rows(rows: Iterable[dict], weights: dict[int, float]) -> list[dict] | None:
    return _rescore_expected(rows, lambda s: dst_store.expected_points(weights, s))


# ----------------------------------------------------------------------------- apply


def _copy(con, table: str, where: str = "", params: list | None = None) -> int:
    if not table_exists(con, table):      # created at runtime by its own module (projection ranges): same columns
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM {_ref(table)} WHERE false")
    con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {_ref(table)} {where}", params or [])
    return con.execute(f"SELECT count(*) FROM {_ref(table)} {where}", params or []).fetchone()[0]


def _derive(con, family: tuple[str, str], run: dict, rows: list[dict], fp: str, *, method: str,
            digest, scoring_meta: dict | None = None) -> None:
    runs_t, rows_t = family
    rid = f"{run['run_id']}~{fp.split(':')[-1][-8:]}"
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
        mixes: dict[tuple[int, int], dict] = {}
        for run in _runs(con, SKILL, season):
            if run["scoring_fingerprint"] == target.fingerprint:
                _copy(con, SKILL[0], "WHERE run_id = ?", [run["run_id"]])
                _copy(con, SKILL[1], "WHERE run_id = ?", [run["run_id"]])
                stats["copied"] += 1
            elif ref is not None and ref.supported and run["scoring_fingerprint"] == ref.fingerprint:
                through = (run.get("data_through_season") or season,
                           run.get("data_through_week") or max(int(run["week"]) - 1, 0))
                if through not in mixes:
                    mixes[through] = stat_mix(con, through)
                rows = rescore_skill_rows(_rows(con, SKILL, run["run_id"]), mixes[through], ref, target)
                _derive(con, SKILL, run, rows, target.fingerprint, method="stat-mix ratio", digest=pf.content_sha256)
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

    # research: evaluations of the reference scoring
    same = ref is not None and target.supported and ref.supported and ref.fingerprint == target.fingerprint
    out["research"] = {t: _copy(con, t) if same and table_exists(con, _ref(t)) else 0 for t in RESEARCH}
    return out
