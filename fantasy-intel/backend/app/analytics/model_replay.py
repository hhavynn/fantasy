"""Weekly model replay: the hosted reader (SQL and the standard library only).

The prospective scorecard (performance_report) only grows one week at a time, and only for the model that was live
that week. The replay answers a different, faster question: if TODAY's production model had made every finished week
of the season, using only data from before that week, how close would it have been? It is computed OFFLINE by
scripts/model_replay.py (the same walk-forward refit as scripts/ifp22_backtest.py: for week W the model is refitted
on rows of weeks strictly before W) and stored as ONE write-once row of `model_replays` per distinct snapshot.

Every refresh stores a new snapshot when the model code/config or the finished weeks changed, so after a model change
the page can say, week by week, whether the new model was closer or further than the previous one. The replay is
hindsight: features are rebuilt from today's data (stat corrections, depth snapshots, injury reports as synced
now), and it scores players who played (conditional on appearing). It is never prospective evidence and never
counts toward a promotion or the scorecard's verdicts.

`snapshots` returns the stored rows of a season that verify (hash, schema, structure), newest first; `response` is
the API body: the newest snapshot, the newest earlier snapshot of a DIFFERENT model fingerprint, and the per-week
differences between them (simple subtraction of stored MAEs; nothing is re-estimated here).
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists
from .performance_report import FORBIDDEN_KEYS, _naive_utc, canonical, sha256_text

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
REQUIRED_KEYS = ("schema_version", "season", "generated_at", "model", "prior_version", "data", "weeks", "totals",
                 "population", "notes")


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def _forbidden(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                return f"identifier field {k!r} in a replay"
            bad = _forbidden(v)
            if bad:
                return bad
    elif isinstance(obj, list):
        for v in obj:
            bad = _forbidden(v)
            if bad:
                return bad
    return None


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
    if not payload["model"].get("fingerprint") or not (payload["data"] or {}).get("fingerprint"):
        return "model or data fingerprint missing"
    weeks = payload["weeks"]
    if not isinstance(weeks, list):
        return "weeks must be a list"
    seen = []
    for w in weeks:
        if not isinstance(w, dict) or not isinstance(w.get("week"), int):
            return "a week entry has no week number"
        seen.append(w["week"])
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
    return _forbidden(payload)


def store_replay(con, payload: dict) -> tuple[str, bool]:
    """Insert the snapshot once (offline only). Returns (replay_sha256, created). Refuses a payload that would not
    verify, and stores nothing when the newest snapshot of the season already has the same model and data
    fingerprints (an unchanged model on unchanged weeks is the same replay)."""
    problem = validate(payload)
    if problem:
        raise ValueError(f"refusing to store the replay: {problem}")
    newest = snapshots(con, payload["season"], limit=1)
    if newest and newest[0]["payload"]["model"]["fingerprint"] == payload["model"]["fingerprint"] \
            and newest[0]["payload"]["data"]["fingerprint"] == payload["data"]["fingerprint"]:
        return newest[0]["replay_sha256"], False
    text = canonical(payload)
    sha = sha256_text(text)
    before = con.execute(f"SELECT count(*) FROM {TABLE} WHERE replay_sha256 = ?", [sha]).fetchone()[0]
    con.execute(f"INSERT INTO {TABLE} (replay_sha256, season, schema_version, generated_at, model_fingerprint, "
                f"data_fingerprint, payload) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [sha, payload["season"], payload["schema_version"], _naive_utc(payload["generated_at"]),
                 payload["model"]["fingerprint"], payload["data"]["fingerprint"], text])
    return sha, before == 0


def snapshots(con, season: int, *, limit: int | None = None) -> list[dict]:
    """Verified snapshots of the season, newest first: [{replay_sha256, payload}]. Rows that fail a check are
    skipped (a damaged row never hides an older good one)."""
    if not table_exists(con, TABLE):
        return []
    out = []
    for r in fetch_dicts(con, f"SELECT replay_sha256, schema_version, payload FROM {TABLE} WHERE season = ? "
                              "ORDER BY generated_at DESC, replay_sha256", [season]):
        text = r["payload"]
        if not isinstance(text, str) or sha256_text(text) != r["replay_sha256"]:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if validate(payload, season=season) or r["schema_version"] != payload["schema_version"]:
            continue
        out.append({"replay_sha256": r["replay_sha256"], "payload": payload})
        if limit and len(out) >= limit:
            break
    return out


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


def compare(new: dict, old: dict) -> dict:
    """Per week and group: MAE(new snapshot's model) - MAE(old snapshot's model), on the weeks both replayed. The
    player-weeks can differ slightly (stat corrections, newly synced appearances), so both n are kept."""
    old_weeks = {w["week"]: w for w in old["weeks"]}
    weeks = []
    for w in new["weeks"]:
        o = old_weeks.get(w["week"])
        if o is None:
            continue
        weeks.append({"week": w["week"], "groups": {g: _diff(w["groups"][g], o["groups"][g]) for g in GROUPS}})
    tally = {g: {k: sum(1 for w in weeks if (w["groups"][g] or {}).get("direction") == k)
                 for k in ("better", "worse", "same")} for g in GROUPS}
    return {"against": {"generated_at": old["generated_at"], "model": old["model"]}, "weeks": weeks, "tally": tally}


def vs_prior(snapshot: dict) -> dict:
    """Within one snapshot: MAE(model) - MAE(prior_version) per week and group (identical player-weeks)."""
    weeks = [{"week": w["week"], "groups": {g: _diff(w["groups"][g], w["groups"][g], "model", "prior_version")
                                             for g in GROUPS}} for w in snapshot["weeks"]]
    tally = {g: {k: sum(1 for w in weeks if (w["groups"][g] or {}).get("direction") == k)
                 for k in ("better", "worse", "same")} for g in GROUPS}
    return {"weeks": weeks, "tally": tally}


def response(con, season: int) -> dict:
    """The API body: newest snapshot, the previous model's newest snapshot and their per-week differences."""
    snaps = snapshots(con, season)
    base = {"schema_version": API_SCHEMA_VERSION, "season": season, "same_within": SAME_WITHIN,
            "history": [{"generated_at": s["payload"]["generated_at"], "model": s["payload"]["model"],
                         "weeks": [w["week"] for w in s["payload"]["weeks"]],
                         "mae": s["payload"]["totals"]["ALL"]["mae"]["model"],
                         "n": s["payload"]["totals"]["ALL"]["n"]} for s in snaps[:20]]}
    if not snaps:
        return {**base, "available": False, "latest": None, "previous": None, "vs_previous": None, "vs_prior": None,
                "unavailable_reason": "No replay has been stored for this season yet. It is built offline by the "
                                      "scheduled refresh (scripts/model_replay.py) once a week is fully final."}
    latest = snaps[0]["payload"]
    prev = next((s["payload"] for s in snaps[1:]
                 if s["payload"]["model"]["fingerprint"] != latest["model"]["fingerprint"]), None)
    return {**base, "available": True, "unavailable_reason": None, "latest": latest, "previous": prev,
            "vs_previous": compare(latest, prev) if prev else None, "vs_prior": vs_prior(latest)}
