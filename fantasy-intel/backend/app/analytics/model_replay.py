"""Weekly model replay: the hosted reader (SQL and the standard library only).

The prospective scorecard (performance_report) only grows one week at a time, and only for the model that was live
that week. The replay answers a different, faster question: if TODAY's production model had made every finished week
of the season, using only data from before that week, how close would it have been? It is computed OFFLINE by
scripts/model_replay.py (the same walk-forward refit as scripts/ifp22_backtest.py: for week W the model is refitted
on rows of weeks strictly before W) and stored as ONE write-once row of `model_replays` per distinct snapshot.

Every refresh stores a new snapshot when the model, its inputs or the replayed forecasts changed, so after a model change
the page can say, week by week, whether the new model was closer or further than the previous one. The replay is
hindsight: features are rebuilt from today's data (stat corrections, depth snapshots, injury reports as synced
now), and it scores players who played (conditional on appearing). It is never prospective evidence and never
counts toward a promotion or the scorecard's verdicts.

`response` is the API body: the newest snapshot that verifies, the previous model (the newest earlier snapshot of a
different model fingerprint whose forecasts actually differ on a shared week, so an unrelated code edit never hides
it), and the per-week differences between them (subtraction of stored MAEs; nothing is re-estimated here). Each week
carries an input and an output fingerprint: a week whose inputs changed between the two snapshots is flagged and left
out of the tally and season figures. History comes from index columns; only returned payloads are verified.
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists
from .performance_report import _forbidden, _naive_utc, canonical, sha256_text

TABLE = "model_replays"
SCHEMA_VERSION = "model-replay/v1"
API_SCHEMA_VERSION = "model-replay-api/v1"
POSITIONS = ("QB", "RB", "WR", "TE")
GROUPS = (*POSITIONS, "ALL")
# The three forecasts replayed on the same player-weeks: the production model (as the code is now), the previous
# production version (ifp-2.1.0's frozen spec, refitted the same way) and the recent-average baseline A1.
METRICS = ("model", "prior_version", "recent_avg")
# |MAE difference| below this (points) reads as "about the same": a replay is one season, not a significance test.
SAME_WITHIN = 0.05
HISTORY_ROWS = 20
REQUIRED_KEYS = ("schema_version", "season", "generated_at", "model", "prior_version", "data", "weeks", "totals",
                 "population", "notes")
WEEK_FINGERPRINTS = ("input_fingerprint", "output_fingerprint")


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def _cell_problem(label: str, cell) -> str | None:
    if not isinstance(cell, dict) or not isinstance(cell.get("n"), int) or cell["n"] < 0:
        return f"{label}: missing or bad n"
    mae = cell.get("mae")
    if not isinstance(mae, dict) or set(mae) != set(METRICS):
        return f"{label}: mae must have exactly {', '.join(METRICS)}"
    for m, v in mae.items():
        if v is None and cell["n"] == 0:
            continue
        if not _number(v) or v < 0:
            return f"{label}: mae.{m} is not a non-negative number"
    return None


def validate(payload, *, season: int | None = None) -> str | None:
    """None when the snapshot is servable, else the reason."""
    if not isinstance(payload, dict):
        return "not a JSON object"
    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        return f"missing {', '.join(missing)}"
    if payload["schema_version"] != SCHEMA_VERSION:
        return f"unknown schema {payload['schema_version']!r}"
    if season is not None and payload["season"] != season:
        return "season mismatch"
    for k in ("model", "prior_version"):
        if not isinstance(payload[k], dict) or not payload[k].get("version"):
            return f"{k}.version missing"
    if not payload["model"].get("fingerprint") or not (payload["data"] or {}).get("fingerprint") \
            or not payload["data"].get("output_fingerprint"):
        return "model, data or output fingerprint missing"
    weeks = payload["weeks"]
    if not isinstance(weeks, list):
        return "weeks must be a list"
    seen = []
    for w in weeks:
        if not isinstance(w, dict) or not isinstance(w.get("week"), int):
            return "a week entry has no week number"
        seen.append(w["week"])
        if not all(isinstance(w.get(k), str) and w[k] for k in WEEK_FINGERPRINTS):
            return f"week {w['week']}: input or output fingerprint missing"
        for g in GROUPS:
            bad = _cell_problem(f"week {w['week']} {g}", (w.get("groups") or {}).get(g))
            if bad:
                return bad
        if w["groups"]["ALL"]["n"] != sum(w["groups"][p]["n"] for p in POSITIONS):
            return f"week {w['week']}: ALL n is not the sum of the positions"
    if seen != sorted(set(seen)):
        return "weeks must be unique and ascending"
    for g in GROUPS:
        bad = _cell_problem(f"totals {g}", payload["totals"].get(g) if isinstance(payload["totals"], dict) else None)
        if bad:
            return bad
        if payload["totals"][g]["n"] != sum(w["groups"][g]["n"] for w in weeks):
            return f"totals {g}: n is not the sum of the weeks"
    hit = _forbidden(payload)
    return f"identifier field {hit!r} in a replay" if hit else None


# ----------------------------------------------------------------------------- storage


def _rows(con, season: int) -> list[dict]:
    """Index columns of the season's rows, newest first (no payload: verification is per row, on demand)."""
    if not table_exists(con, TABLE):
        return []
    return fetch_dicts(con, f"SELECT replay_sha256, generated_at, model_fingerprint, model_version, weeks, mae_all, "
                            f"n_all FROM {TABLE} WHERE season = ? ORDER BY generated_at DESC, replay_sha256", [season])


def _load(con, sha: str, season: int) -> dict | None:
    """The row's payload when it verifies (hash, JSON, schema, structure, index columns), else None."""
    r = fetch_dicts(con, f"SELECT schema_version, model_fingerprint, payload FROM {TABLE} WHERE replay_sha256 = ?", [sha])
    if not r:
        return None
    text = r[0]["payload"]
    if not isinstance(text, str) or sha256_text(text) != sha:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if validate(payload, season=season) or r[0]["schema_version"] != payload["schema_version"] \
            or r[0]["model_fingerprint"] != payload["model"]["fingerprint"]:
        return None
    return payload


def snapshots(con, season: int, *, limit: int | None = None) -> list[dict]:
    """Verified snapshots of the season, newest first: [{replay_sha256, payload}]. Rows that fail a check are
    skipped (a damaged row never hides an older good one)."""
    out = []
    for r in _rows(con, season):
        payload = _load(con, r["replay_sha256"], season)
        if payload is not None:
            out.append({"replay_sha256": r["replay_sha256"], "payload": payload})
            if limit and len(out) >= limit:
                break
    return out


def store_replay(con, payload: dict) -> tuple[str, bool]:
    """Insert the snapshot once (offline only). Returns (replay_sha256, created). Refuses a payload that would not
    verify, and stores nothing when the newest snapshot of the season has the same model, data AND output fingerprints
    (same model, same inputs, same forecasts): a change to any of them, even one the model or data fingerprint missed,
    stores a new snapshot. Which snapshot counts as "the previous model" is decided by `previous_model`."""
    problem = validate(payload)
    if problem:
        raise ValueError(f"refusing to store the replay: {problem}")
    newest = snapshots(con, payload["season"], limit=1)
    if newest:
        old = newest[0]["payload"]
        if old["model"]["fingerprint"] == payload["model"]["fingerprint"] \
                and all(old["data"].get(k) == payload["data"][k] for k in ("fingerprint", "output_fingerprint")):
            return newest[0]["replay_sha256"], False
    text = canonical(payload)
    sha = sha256_text(text)
    before = con.execute(f"SELECT count(*) FROM {TABLE} WHERE replay_sha256 = ?", [sha]).fetchone()[0]
    totals = payload["totals"]["ALL"]
    con.execute(f"INSERT INTO {TABLE} (replay_sha256, season, schema_version, generated_at, model_fingerprint, "
                f"data_fingerprint, model_version, weeks, mae_all, n_all, payload) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [sha, payload["season"], payload["schema_version"], _naive_utc(payload["generated_at"]),
                 payload["model"]["fingerprint"], payload["data"]["fingerprint"], payload["model"]["version"],
                 json.dumps([w["week"] for w in payload["weeks"]]), totals["mae"]["model"], totals["n"], text])
    return sha, before == 0


# ----------------------------------------------------------------------------- comparison


def direction(diff: float | None) -> str | None:
    """'better' (lower MAE), 'worse' or 'same' for a MAE difference new - old."""
    if diff is None:
        return None
    return "same" if abs(diff) < SAME_WITHIN else ("better" if diff < 0 else "worse")


def _diff(new: dict, old: dict | None, metric_new: str = "model", metric_old: str = "model") -> dict | None:
    if old is None or not new["n"] or not old["n"]:
        return None
    d = round(new["mae"][metric_new] - old["mae"][metric_old], 4)
    return {"diff": d, "direction": direction(d), "n_new": new["n"], "n_old": old["n"]}


def _pooled(cells: list[dict], metric: str) -> float | None:
    """MAE over several weeks' cells: the n-weighted mean of their MAEs (= MAE over the pooled player-weeks)."""
    n = sum(c["n"] for c in cells if c["n"])
    return round(sum(c["mae"][metric] * c["n"] for c in cells if c["n"]) / n, 4) if n else None


def _season(pairs: list[tuple[dict, dict]], metric_new: str = "model", metric_old: str = "model") -> dict:
    """Per group, season MAE of both sides over the SAME weeks (`pairs`: (new week, old week)) and their difference,
    so the season figures never mix a week only one side replayed."""
    out = {}
    for g in GROUPS:
        new = [n["groups"][g] for n, o in pairs if n["groups"][g]["n"] and o["groups"][g]["n"]]
        old = [o["groups"][g] for n, o in pairs if n["groups"][g]["n"] and o["groups"][g]["n"]]
        a, b = _pooled(new, metric_new), _pooled(old, metric_old)
        d = None if a is None or b is None else round(a - b, 4)
        out[g] = {"mae_new": a, "mae_old": b, "diff": d, "direction": direction(d),
                  "n_new": sum(c["n"] for c in new), "n_old": sum(c["n"] for c in old)}
    return {"weeks": [n["week"] for n, _ in pairs], "groups": out}


def _tally(weeks: list[dict]) -> dict:
    return {g: {k: sum(1 for w in weeks if (w["groups"][g] or {}).get("direction") == k)
                for k in ("better", "worse", "same")} for g in GROUPS}


def compare(new: dict, old: dict) -> dict:
    """Per week and group: MAE(new snapshot's model) - MAE(old snapshot's model), on the weeks both replayed. A week
    whose inputs differ between the two snapshots (stat corrections, newly synced depth or injury data up to that week)
    is listed with inputs_changed: its difference mixes the model change with the data change, so the tally and the
    season figures use only the weeks with identical inputs."""
    old_weeks = {w["week"]: w for w in old["weeks"]}
    pairs = [(w, old_weeks[w["week"]]) for w in new["weeks"] if w["week"] in old_weeks]
    weeks = [{"week": w["week"], "inputs_changed": w["input_fingerprint"] != o["input_fingerprint"],
              "groups": {g: _diff(w["groups"][g], o["groups"][g]) for g in GROUPS}} for w, o in pairs]
    clean = [(w, o) for w, o in pairs if w["input_fingerprint"] == o["input_fingerprint"]]
    return {"against": {"generated_at": old["generated_at"], "model": old["model"]}, "weeks": weeks,
            "tally": _tally([w for w in weeks if not w["inputs_changed"]]),
            "inputs_changed_weeks": [w["week"] for w in weeks if w["inputs_changed"]], "season": _season(clean)}


def vs_prior(snapshot: dict) -> dict:
    """Within one snapshot: MAE(model) - MAE(prior_version) per week and group (identical player-weeks and inputs)."""
    weeks = [{"week": w["week"], "inputs_changed": False,
              "groups": {g: _diff(w["groups"][g], w["groups"][g], "model", "prior_version") for g in GROUPS}}
             for w in snapshot["weeks"]]
    return {"weeks": weeks, "tally": _tally(weeks), "inputs_changed_weeks": [],
            "season": _season([(w, w) for w in snapshot["weeks"]], "model", "prior_version")}


def forecasts_differ(new: dict, old: dict) -> bool:
    """True when the two snapshots' forecasts differ on at least one week both replayed."""
    old_out = {w["week"]: w["output_fingerprint"] for w in old["weeks"]}
    return any(w["week"] in old_out and w["output_fingerprint"] != old_out[w["week"]] for w in new["weeks"])


def previous_model(con, season: int, latest: dict, rows: list[dict]) -> dict | None:
    """The newest older snapshot of a different model fingerprint whose forecasts actually differ from the latest's on
    a shared week. A fingerprint change with identical forecasts (an edit to a module the model does not use that
    way) is not a model change, so it never hides the real previous model. Rows are verified one at a time."""
    for r in rows:
        if r["model_fingerprint"] == latest["model"]["fingerprint"]:
            continue
        payload = _load(con, r["replay_sha256"], season)
        if payload is not None and forecasts_differ(latest, payload):
            return payload
    return None


def _history_row(r: dict) -> dict:
    try:
        weeks = json.loads(r["weeks"]) if r["weeks"] else []
    except ValueError:
        weeks = []
    return {"generated_at": str(r["generated_at"]), "model": {"version": r["model_version"],
                                                              "fingerprint": r["model_fingerprint"]},
            "weeks": weeks, "mae": r["mae_all"], "n": r["n_all"]}


def response(con, season: int) -> dict:
    """The API body: newest verified snapshot, the previous model's snapshot and their per-week differences. History
    comes from the index columns (up to HISTORY_ROWS); only the payloads returned are read and verified."""
    rows = _rows(con, season)
    base = {"schema_version": API_SCHEMA_VERSION, "season": season, "same_within": SAME_WITHIN,
            "history": [_history_row(r) for r in rows[:HISTORY_ROWS]]}
    latest, rest = None, []
    for i, r in enumerate(rows):
        latest = _load(con, r["replay_sha256"], season)
        if latest is not None:
            rest = rows[i + 1:]
            break
    if latest is None:
        return {**base, "available": False, "latest": None, "previous": None, "vs_previous": None, "vs_prior": None,
                "unavailable_reason": "No replay has been stored for this season yet. It is built offline by the "
                                      "scheduled refresh (scripts/model_replay.py) once a week is fully final."}
    prev = previous_model(con, season, latest, rest)
    return {**base, "available": True, "unavailable_reason": None, "latest": latest, "previous": prev,
            "vs_previous": compare(latest, prev) if prev else None, "vs_prior": vs_prior(latest)}
