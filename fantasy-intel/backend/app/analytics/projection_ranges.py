"""Floor / ceiling ranges around the served Ours projection (ifpd-0.1.0; the fantasy point-distribution report in
docs/research, sections 14 and 18).

A range is the 10th / 50th / 90th percentile of a player's points if he plays, read from the frozen point-distribution
artifact the frozen pregame distribution ledger pins: the scaled local-outcome method R@300,
centred on the projection, so the projection stays the distribution's mean. 80% of outcomes land between the floor and
the ceiling. This module is the serving side only; it never fits anything.

Storage. `scripts/projection_ranges.py load` copies the pinned artifact's residual-quantile table (one row per position
and grid mean, at the three served levels plus the row's residual mean) into TABLE, append-only by pin. The MotherDuck
publish copies every table, so the hosted API reads it like any other. numpy-free: the hosted API has no research
packages, and app code never imports the research module (tested). `quantiles` reproduces the research module's
artifact_quantiles exactly at the served levels (tested against it).

Which ranges are served (POSITION_POLICY, from the report's own confirmation and replay numbers):
  RB, WR, TE  served: conditional ranges validated on the 2023-24 confirmation run (80% coverage 0.80-0.81, P10/P90
              rates 0.09-0.10) and held on the 2025-26 replay (coverage 0.79-0.81).
  QB          not served: its floor ran high in the 2025-26 replay (12.2% of outcomes below P10, 14.1% among
              starters), so section 14 keeps it back until prospective weeks confirm the lower tail.
  K, D/ST     no distribution model.

A range is attached only to a served run's next-week forecast (forecast horizon 0, the horizon the ranges were
validated at), to a row with a projection (coverage_status "projected"), and only when the artifact was built under the
run's league scoring.
"""

from __future__ import annotations

from bisect import bisect_right

from ..db.connection import fetch_dicts, table_exists, utcnow
from . import production_forecasts as pf
from . import projection_source as ps

TABLE = "projection_range_quantiles"
DISTRIBUTION_VERSION = "ifpd-0.1.0"
ARTIFACT_SCHEMA = "point-distribution-artifact/v1"
LEVELS = {"floor": 0.1, "median": 0.5, "ceiling": 0.9}
COVERAGE = 0.8
SERVED_POSITIONS = ("RB", "WR", "TE")
POSITION_POLICY = {
    "QB": {"served": False, "reason": "QB floors ran high in the 2025-26 replay (12.2% of outcomes below the floor, "
                                      "14.1% among starters); QB ranges return once prospective weeks confirm the lower tail"},
    "RB": {"served": True, "reason": None},
    "WR": {"served": True, "reason": None},
    "TE": {"served": True, "reason": None},
    "K": {"served": False, "reason": "no point-distribution model for kickers"},
    "D/ST": {"served": False, "reason": "no point-distribution model for team defenses"},
}
# The measured 80% coverage behind each served position (the fantasy point-distribution report in docs/research,
# sections 6 and 18): the frozen 2023-24 confirmation run (rows, coverage) and the exploratory 2025-26 replay. Stated
# next to a range; never recomputed here. The report's file name is not spelled out: app code never names the
# research module, not even in text (guarded by its own test).
VALIDATION_SOURCE = "the fantasy point-distribution report (docs/research), sections 6 and 18"
VALIDATION = {
    "RB": {"confirmation": {"window": "2023-24", "rows": 3398, "coverage": 0.813},
           "replay": {"window": "2025-26", "kind": "exploratory", "coverage": 0.810}},
    "WR": {"confirmation": {"window": "2023-24", "rows": 5409, "coverage": 0.813},
           "replay": {"window": "2025-26", "kind": "exploratory", "coverage": 0.807}},
    "TE": {"confirmation": {"window": "2023-24", "rows": 3208, "coverage": 0.801},
           "replay": {"window": "2025-26", "kind": "exploratory", "coverage": 0.791}},
}
DEFINITION = ("floor = 10th percentile, median = 50th, ceiling = 90th percentile of his points if he plays: 80% of "
              "outcomes land between floor and ceiling. The projection is the distribution's mean.")
_COLUMNS = ("pin_id", "content_hash", "artifact_sha256", "version", "scoring_fingerprint", "trained_through_key",
            "pinned_at", "loaded_at", "position", "method", "status", "grid_index", "mean", "r10", "r50", "r90",
            "r_mean")


def _mean_weights(taus: list[float]) -> list[float]:
    """The research module's _mean_weights: mean(Q) = values @ w for a piecewise-linear Q with constant tails."""
    n = len(taus)
    if n == 1:
        return [1.0]
    w = [0.0] * n
    for i in range(n - 1):
        g = (taus[i + 1] - taus[i]) / 2
        w[i] += g
        w[i + 1] += g
    w[0] += taus[0]
    w[-1] += 1 - taus[-1]
    return w


def _knot(taus: list[float], tau: float) -> int:
    for i, t in enumerate(taus):
        if abs(t - tau) < 1e-9:
            return i
    raise ValueError(f"the artifact has no quantile knot at {tau}")


def table_rows(artifact: dict, pin: dict, *, loaded_at=None) -> list[dict]:
    """TABLE rows for one pinned artifact: every position with a mean-grid table (its residual quantiles at the
    served levels, plus each grid row's residual mean so serving re-centres exactly as artifact_quantiles does)."""
    if artifact.get("schema") != ARTIFACT_SCHEMA or artifact.get("version") != DISTRIBUTION_VERSION:
        raise ValueError(f"not a {DISTRIBUTION_VERSION} point-distribution artifact")
    if artifact.get("content_hash") != pin.get("content_hash"):
        raise ValueError("the artifact is not the pinned one (content hash differs)")
    taus = [float(t) for t in artifact["taus"]]
    weights = _mean_weights(taus)
    idx = {k: _knot(taus, t) for k, t in LEVELS.items()}
    loaded_at = loaded_at or utcnow()
    out = []
    for pos, entry in sorted(artifact["positions"].items()):
        tab = entry.get("table")
        if not tab or tab.get("kind") != "mean_grid":
            continue
        for g, (m, rq) in enumerate(zip(tab["mean_grid"], tab["residual_quantiles"])):
            out.append({"pin_id": int(pin["pin_id"]), "content_hash": artifact["content_hash"],
                        "artifact_sha256": pin.get("artifact_sha256"), "version": artifact["version"],
                        "scoring_fingerprint": artifact["scoring_fingerprint"],
                        "trained_through_key": int(artifact["trained_through_key"]), "pinned_at": pin.get("pinned_at"),
                        "loaded_at": loaded_at, "position": pos, "method": entry.get("method"),
                        "status": entry.get("status"), "grid_index": g, "mean": float(m),
                        "r10": float(rq[idx["floor"]]), "r50": float(rq[idx["median"]]),
                        "r90": float(rq[idx["ceiling"]]),
                        "r_mean": sum(float(r) * w for r, w in zip(rq, weights))})
    return out


def store(con, artifact: dict, pin: dict) -> int:
    """Append the pinned artifact's rows to TABLE once (keyed by pin id and content hash). Returns rows written."""
    con.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        pin_id INTEGER, content_hash VARCHAR, artifact_sha256 VARCHAR, version VARCHAR, scoring_fingerprint VARCHAR,
        trained_through_key INTEGER, pinned_at VARCHAR, loaded_at TIMESTAMP, position VARCHAR, method VARCHAR,
        status VARCHAR, grid_index INTEGER, mean DOUBLE, r10 DOUBLE, r50 DOUBLE, r90 DOUBLE, r_mean DOUBLE)""")
    if con.execute(f"SELECT count(*) FROM {TABLE} WHERE pin_id = ? AND content_hash = ?",
                   [int(pin["pin_id"]), artifact["content_hash"]]).fetchone()[0]:
        return 0
    rows = table_rows(artifact, pin)
    if rows:
        con.executemany(f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
                        [[r[c] for c in _COLUMNS] for r in rows])
    return len(rows)


def load(con) -> dict | None:
    """The newest pin's tables: {pin_id, content_hash, scoring_fingerprint, trained_through_key, positions {pos:
    {method, status, grid, r10, r50, r90, r_mean}}}, or None when nothing is loaded."""
    if not table_exists(con, TABLE):
        return None
    rows = fetch_dicts(con, f"""SELECT * FROM {TABLE} WHERE pin_id = (SELECT max(pin_id) FROM {TABLE})
                                ORDER BY position, grid_index""")
    if not rows:
        return None
    head = rows[0]
    out = {k: head[k] for k in ("pin_id", "content_hash", "artifact_sha256", "version", "scoring_fingerprint",
                                "trained_through_key")}
    positions: dict[str, dict] = {}
    for r in rows:
        p = positions.setdefault(r["position"], {"method": r["method"], "status": r["status"], "grid": [],
                                                 "r10": [], "r50": [], "r90": [], "r_mean": []})
        p["grid"].append(r["mean"])
        for k in ("r10", "r50", "r90", "r_mean"):
            p[k].append(r[k])
    out["positions"] = positions
    return out


def quantiles(tables: dict, position: str, mean: float) -> dict | None:
    """{floor, median, ceiling} for one projection (points if he plays), or None when the position has no table.
    Interpolates the residual rows linearly in the mean, clamped to the grid, re-centred so the mean is exact."""
    t = (tables.get("positions") or {}).get(position)
    if not t or len(t["grid"]) < 2:
        return None
    grid = t["grid"]
    g = min(max(float(mean), grid[0]), grid[-1])
    j = min(max(bisect_right(grid, g), 1), len(grid) - 1)
    w = (g - grid[j - 1]) / (grid[j] - grid[j - 1])

    def at(col: str) -> float:
        return t[col][j - 1] + w * (t[col][j] - t[col][j - 1])

    shift = at("r_mean")
    floor, median, ceiling = (float(mean) + at(c) - shift for c in ("r10", "r50", "r90"))
    median, ceiling = max(median, floor), max(ceiling, median, floor)
    return {"floor": round(floor, 2), "median": round(median, 2), "ceiling": round(ceiling, 2)}


def servable(tables: dict | None, position: str | None) -> tuple[bool, str | None]:
    """(served, reason when not) for a position under the loaded tables."""
    policy = POSITION_POLICY.get(position or "")
    if policy is None:
        return False, f"no point-distribution model for {position}"
    if not policy["served"]:
        return False, policy["reason"]
    t = ((tables or {}).get("positions") or {}).get(position)
    if t is None:
        return False, f"the pinned distribution has no {position} table"
    if t["status"] != "validated":
        return False, f"the pinned {position} distribution is {t['status']}"
    return True, None


def run_problem(tables: dict | None, run: dict | None) -> str | None:
    """Why no row of this served run gets a range (None when rows can)."""
    if run is None:
        return "no Ours projection run is served for this week"
    if tables is None:
        return "no pinned point distribution is loaded"
    if pf.horizon_of(run) != 0:
        return (f"ranges cover the next-week forecast; this week is a rest-of-season projection made "
                f"{pf.horizon_of(run)} week(s) ahead")
    if run.get("scoring_fingerprint") != tables["scoring_fingerprint"]:
        return "the pinned distribution was built under different league scoring"
    return None


def row_range(tables: dict, row: dict) -> dict | None:
    """The range for one served row (a dict with position, coverage_status, projected_points), or None."""
    ok, _ = servable(tables, row.get("position"))
    mean = row.get("projected_points")
    if not ok or row.get("coverage_status") != "projected" or mean is None:
        return None
    return quantiles(tables, row["position"], float(mean))


def describe(tables: dict | None, problem: str | None) -> dict:
    """The response block saying what the ranges are and which positions carry one."""
    positions = {}
    for pos in POSITION_POLICY:
        ok, why = servable(tables, pos)
        positions[pos] = {"served": ok and problem is None, "reason": problem or why}
    return {"available": tables is not None and problem is None, "reason": problem,
            "version": DISTRIBUTION_VERSION, "coverage": COVERAGE, "levels": LEVELS, "conditional_on": "appears",
            "definition": DEFINITION, "positions": positions,
            "artifact": None if tables is None else {k: tables[k] for k in ("pin_id", "content_hash", "trained_through_key")}}


def for_week(con, season: int, week: int) -> dict:
    """Ranges for the run every consumer view serves for the week (projection_source.select_run):
    {available, reason, run, ranges {espn_player_id: {position, projection, floor, median, ceiling}}, ...describe}."""
    sel = ps.select_run(con, season, week)
    run, tables = sel["run"], load(con)
    problem = run_problem(tables, run) if run is not None else (sel["unavailable_reason"] or run_problem(tables, None))
    ranges = {}
    if problem is None:
        for r in sel["rows"]:
            rng = row_range(tables, r)
            if rng is not None:
                ranges[int(r["espn_player_id"])] = {"position": r["position"], "projection": r["projected_points"],
                                                    **rng}
    return {**describe(tables, problem), "season": season, "week": week,
            "run": None if run is None else {"run_id": run["run_id"], "model_version": run["model_version"],
                                             "forecast_horizon": pf.horizon_of(run)},
            "ranges": ranges}
