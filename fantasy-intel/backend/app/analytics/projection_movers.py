"""Weekly projection movers: whose outlook changed between two stored forecasts, and by how much.

SQL + stdlib only (no polars/numpy, the hosted API has none) and read-only: nothing here trains, syncs or writes.
`movement(...)` answers GET /api/projections/movers (contract `projection-movers/v1`, docs/projection_movers.md).

A *snapshot* is one stored forecast of the remaining weeks. Movement is only ever computed between two snapshots
that were actually stored; a missing one is never reconstructed.

  ESPN (default)  one snapshot per `snapshot_date` of espn_projection_history. taken_at = the latest synced_at of that
                  date; cycle_week = the first regular-season week whose first kickoff is after taken_at.
  ours (opt-in)   one snapshot per cutoff week C (projected_from_week; the run's own week when NULL or absent) of the
                  production model (projection_source.MODEL_VERSION) under the league's CURRENT scoring fingerprint.
                  Per target week T the newest run of that cutoff that (a) was created strictly before T's first
                  kickoff (unknown kickoff = not eligible), (b) is not rejected by production_forecasts.artifact_gate
                  and (c) passes production_forecasts.integrity_problem. A rejected or failed run is listed in
                  `skipped` and never used (the next older run of the same cutoff is tried). A cutoff that only has
                  runs of the previous model (FALLBACK_VERSION) is not a snapshot: movement across model versions is
                  never shown. Runs under another scoring fingerprint are never compared (`excluded_runs`).

current = the newest snapshot. baseline defaults to the newest snapshot of an earlier cycle week ("last week's
outlook"), else the newest older one ("earlier this week"); an explicit baseline must be strictly older.

The WINDOW is the one set of target weeks both snapshots forecast, from the current cycle week to the last week
either covers, whose first kickoff is known and after the latest snapshot (so neither forecast saw any of those
games). Every other week is listed in `window.excluded_weeks` with its reason. A player is COMPARABLE only when every
window week has a value in BOTH snapshots (complete or null, like forward_ours); otherwise he is counted by reason in
`summary.not_comparable_reasons`, never given a partial sum. Values: ESPN's row; ours = expected_points when the row
is "projected", 0.0 for a bye or a withheld-unavailable row, otherwise missing. A missing ESPN row on a schedule bye
counts 0.0 and is flagged `bye`. No outcome (actual points, box scores) of any week is read.

For ours the change splits exactly into availability (p_play) and per-game (points if he plays): with p and x of the
earlier (0) and later (1) forecast of a week, delta = (p1-p0)*x0 + p1*(x1-x0). A zero-status week has p = 0 and takes
the other side's x. ESPN publishes no components, so they are null there.

Reads are batched (a constant number of queries in the number of players); ours reads the rows of a snapshot only
when it is loaded (the current and the chosen baseline), not to list the baselines. The listed `taken_at` of a
baseline that is not loaded comes from run metadata, before its integrity check.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..db.connection import fetch_dicts, table_exists
from . import production_forecasts as pf
from . import projection_source as ps
from .legacy_league import latest_league
from .projection_backtest import _utc
from .projections import load_schedule

CONTRACT = "projection-movers/v1"
VIEWS = ("ros", "week")
POSITIONS = ps.POSITIONS
THRESHOLDS = {"min_delta": 0.5, "relevant_per_week": 3.0, "pct_floor": 1.0, "flat": 0.05}
MAX_LIMIT, MAX_PLAYERS = 50, 300
ESPN_LABEL = "ESPN projections"
OURS_LABEL = f"Fantasy Intel projections ({ps.MODEL_VERSION})"
ESPN_SCORING_NOTE = ("ESPN does not store the league scoring with each snapshot, so a scoring change between the two "
                     "snapshots could not be told apart from a change in the projections.")
OURS_SCORING_NOTE = ("Both forecasts were made under the league's current scoring (same scoring fingerprint); runs made "
                     "under another scoring are never compared.")
LATE = "kicked off before the latest snapshot"
COVERAGE = {"abstain_no_history": "no NFL history", "uncertain_id_mapping": "uncertain player ID",
            "unsupported_position": "position not modelled", "unsupported_scoring": "scoring not supported",
            "no_model": "no model"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _label(day: date, cycle_week: int | None) -> str:
    return f"{_day(day)} (before week {cycle_week})" if cycle_week is not None else _day(day)


def _cycle_week(first: dict[int, datetime], taken_at: datetime) -> int | None:
    later = sorted(w for w, ko in first.items() if ko > taken_at)
    return later[0] if later else None


def _public(snap: dict) -> dict:
    return {"id": snap["id"], "label": snap["label"], "taken_at": _iso(snap["taken_at"]),
            "cycle_week": snap["cycle_week"], "model_version": snap.get("model_version"),
            "run_ids": snap.get("run_ids")}


def _listed(snap: dict) -> dict:
    return {k: v for k, v in _public(snap).items() if k in ("id", "label", "taken_at", "cycle_week")}


# ----------------------------------------------------------------------------- ESPN snapshots


def _espn_snapshots(con, season: int) -> list[dict]:
    """Newest-first ESPN snapshots: {source, id, label, taken_at, cycle_week, weeks, date}."""
    if not table_exists(con, "espn_projection_history"):
        return []
    first = pf.first_kickoffs(con, season)
    out = []
    for r in fetch_dicts(con, """SELECT snapshot_date, max(synced_at) AS taken_at,
                                        list(DISTINCT scoring_period) FILTER (WHERE projected_points IS NOT NULL) AS weeks
                                 FROM espn_projection_history WHERE season = ?
                                 GROUP BY snapshot_date ORDER BY snapshot_date DESC""", [season]):
        taken = _utc(r["taken_at"])
        if taken is None or not r["weeks"]:     # no capture time or no projection: not a snapshot
            continue
        d = r["snapshot_date"]
        cycle = _cycle_week(first, taken)
        out.append({"source": "espn", "id": d.isoformat(), "label": _label(d, cycle), "taken_at": taken,
                    "cycle_week": cycle, "weeks": sorted(int(w) for w in r["weeks"]), "date": d})
    return out


def _load_espn(con, season: int, snaps: list[dict]) -> list[dict]:
    """The loaded sides of `snaps` (one rows query): values[pid][week] = cell."""
    rows = fetch_dicts(con, """SELECT snapshot_date, espn_player_id, scoring_period, projected_points
                               FROM espn_projection_history
                               WHERE season = ? AND snapshot_date IN (SELECT unnest(?)) AND projected_points IS NOT NULL""",
                       [season, [s["date"] for s in snaps]])
    sides = {s["id"]: {**s, "weeks": set(s["weeks"]), "values": {}, "positions": {}, "horizons": {}} for s in snaps}
    for r in rows:
        side = sides[r["snapshot_date"].isoformat()]
        side["values"].setdefault(int(r["espn_player_id"]), {})[int(r["scoring_period"])] = {
            "value": float(r["projected_points"]), "status": "projected"}
    return [sides[s["id"]] for s in snaps]


# ----------------------------------------------------------------------------- ours: snapshots

def _ours_index(con, season: int) -> dict:
    """Every cutoff's candidate runs, without reading rows: {reason (why nothing can be shown), snaps newest first,
    fallback_cutoffs, excluded_runs, rejected {cutoff: [{run_id, week, reason}]}, late (count)}."""
    idx = {"reason": None, "snaps": [], "fallback_cutoffs": set(), "excluded_runs": [], "rejected": {}}
    fp, why = ps._scoring(con, season)
    if fp is None:
        return {**idx, "reason": f"No Fantasy Intel movers: {why}."}
    if not table_exists(con, pf.RUNS):
        return {**idx, "reason": "No Fantasy Intel forecast is stored in this database yet."}
    if not table_exists(con, pf.ROWS):
        return {**idx, "reason": "No Fantasy Intel movers: the database is not migrated to schema v8, so no stored "
                                 "run can be integrity-checked."}
    first, gate = pf.first_kickoffs(con, season), pf.artifact_gate(con, ps.MODEL_VERSION)
    cands: dict[int, dict[int, list[dict]]] = {}
    for run in fetch_dicts(con, f"SELECT * FROM {pf.RUNS} WHERE season = ? "
                                "ORDER BY created_at DESC NULLS LAST, run_id DESC", [season]):
        cutoff = run.get("projected_from_week")
        cutoff = int(run["week"] if cutoff is None else cutoff)
        week, version = int(run["week"]), run["model_version"]
        entry = {"run_id": run["run_id"], "week": week}
        if version == ps.FALLBACK_VERSION and run["scoring_fingerprint"] == fp:
            idx["fallback_cutoffs"].add(cutoff)
        if version != ps.MODEL_VERSION:
            continue
        if run["scoring_fingerprint"] != fp:
            idx["excluded_runs"].append({**entry, "reason": "made under different league scoring"})
            continue
        created, kickoff = _utc(run.get("created_at")), first.get(week)
        status, note = gate(run)
        if created is None:
            reason = "run creation time unknown, so it cannot be shown to be made before kickoff"
        elif kickoff is None:
            reason = f"kickoff time of week {week} unknown, so the run cannot be shown to be made before kickoff"
        elif created >= kickoff:
            reason = f"made after week {week}'s first kickoff"
        elif status == "rejected":
            reason = note
        else:
            cands.setdefault(cutoff, {}).setdefault(week, []).append(
                {**run, "created": created, "artifact_check": status, "artifact_note": note,
                 "horizon": pf.horizon_of(run), "horizon_note": pf.horizon_note(run)})
            continue
        idx["rejected"].setdefault(cutoff, []).append({**entry, "reason": reason})
    for cutoff in sorted(cands, reverse=True):
        weeks = cands[cutoff]
        taken = max(lst[0]["created"] for lst in weeks.values())
        snap = {"source": "ours", "id": f"w{cutoff}", "cutoff": cutoff, "cycle_week": cutoff, "taken_at": taken,
                "weeks": sorted(weeks), "model_version": ps.MODEL_VERSION, "_cands": weeks,
                "run_ids": {str(w): lst[0]["run_id"] for w, lst in sorted(weeks.items())}}
        snap["label"] = _label(taken.date(), cutoff)
        idx["snaps"].append(snap)
    return idx


def _load_ours(con, snap: dict) -> tuple[dict, list[dict]]:
    """(loaded side, skipped): per target week the newest candidate whose rows verify. Rows of the first candidate of
    every week come in one query; a further candidate is read only when the one before it failed."""
    cands = snap["_cands"]
    firsts = sorted({lst[0]["run_id"] for lst in cands.values()})
    pre: dict[str, list[dict]] = {rid: [] for rid in firsts}
    for r in fetch_dicts(con, f"SELECT * FROM {pf.ROWS} WHERE run_id IN (SELECT unnest(?))", [firsts]):
        pre[r["run_id"]].append(r)
    selected, skipped = {}, []
    for week, lst in sorted(cands.items()):
        for run in lst:
            rows = pre[run["run_id"]] if run["run_id"] in pre else fetch_dicts(
                con, f"SELECT * FROM {pf.ROWS} WHERE run_id = ?", [run["run_id"]])
            problem = pf.integrity_problem(run, rows)
            if problem is None:
                selected[week] = (run, rows)
                break
            skipped.append({"run_id": run["run_id"], "week": week, "reason": problem})
    side = {**{k: v for k, v in snap.items() if k != "_cands"}, "weeks": set(selected), "values": {}, "positions": {},
            "horizons": {w: pf.horizon_of(run) for w, (run, _) in selected.items()}}
    if selected:
        side["taken_at"] = max(run["created"] for run, _ in selected.values())
        side["run_ids"] = {str(w): run["run_id"] for w, (run, _) in sorted(selected.items())}
        side["label"] = _label(side["taken_at"].date(), side["cycle_week"])
    for week, (run, rows) in selected.items():
        for r in rows:
            pid, status = int(r["espn_player_id"]), r["coverage_status"]
            cell = {"value": None, "status": status, "p": r["p_play"], "x": r["projected_points"],
                    "team": r["nfl_team"], "horizon": pf.horizon_of(run), "run_id": run["run_id"]}
            if status == "projected" and r["expected_points"] is not None:
                cell["value"] = float(r["expected_points"])
            elif status in ps.ZERO_STATUSES:
                cell["value"] = 0.0
            side["values"].setdefault(pid, {})[week] = cell
            if r["position"]:
                side["positions"].setdefault(pid, r["position"])
    return side, skipped


# ----------------------------------------------------------------------------- contexts (which two snapshots)


def _espn_context(con, season: int, baseline: str | None) -> dict:
    ctx = {"label": ESPN_LABEL, "listed": [], "current": None, "baseline": None, "reason": None, "skipped": [],
           "excluded_runs": [], "notes": [], "kind": None, "final": None}
    league = latest_league(con, ["final_scoring_period"], season=season)
    ctx["final"] = int(league["final_scoring_period"]) if league and league["final_scoring_period"] else None
    snaps = _espn_snapshots(con, season)
    cur = snaps[0] if snaps else None
    ctx["listed"] = [_listed(s) for s in snaps[1:]]
    chosen = None
    if baseline is not None:
        chosen = next((s for s in snaps if s["id"] == baseline), None)
        if chosen is None:
            raise ValueError(f"baseline {baseline!r} is not a stored ESPN snapshot")
        if cur is None or chosen["date"] >= cur["date"]:
            raise ValueError("baseline must be older than the latest snapshot")
    if cur is None:
        ctx["reason"] = "No ESPN projection snapshot is stored yet, so there is no movement to show."
        return ctx
    if len(snaps) < 2:
        ctx["current"] = cur
        ctx["reason"] = (f"Only one ESPN projection snapshot is stored ({_day(cur['date'])}). Movement needs two, and a "
                         "missing snapshot is never reconstructed.")
        return ctx
    older = snaps[1:]
    if chosen is None:
        rank = lambda s: 10 ** 6 if s["cycle_week"] is None else s["cycle_week"]
        previous = [s for s in older if rank(s) < rank(cur)]
        chosen = previous[0] if previous else older[0]
        ctx["kind"] = "previous_week" if previous else "earlier_this_week"
        if not previous:
            ctx["notes"].append("No snapshot from an earlier week is stored, so the comparison is with an earlier "
                                f"snapshot of the same week ({chosen['label']}).")
    cur_side, base_side = _load_espn(con, season, [cur, chosen])
    ctx.update(current=cur_side, baseline=base_side)
    ctx["notes"].append("ESPN projections are stored once per day; only stored snapshots are compared and a missing day "
                        "is never reconstructed.")
    return ctx


def _ours_context(con, season: int, baseline: str | None) -> dict:
    ctx = {"label": OURS_LABEL, "listed": [], "current": None, "baseline": None, "reason": None, "skipped": [],
           "excluded_runs": [], "notes": [], "kind": None, "final": None}
    cutoff = None
    if baseline is not None:
        m = re.fullmatch(r"w(\d{1,2})", baseline)
        if not m:
            raise ValueError(f"baseline {baseline!r} is not a Fantasy Intel snapshot id (like w6)")
        cutoff = int(m.group(1))
    idx = _ours_index(con, season)
    ctx["excluded_runs"] = idx["excluded_runs"]
    everything = sorted((x for lst in idx["rejected"].values() for x in lst), key=lambda x: (x["week"], x["run_id"]))
    if idx["reason"]:
        ctx["reason"] = idx["reason"]
        return ctx
    snaps, fallback = idx["snaps"], idx["fallback_cutoffs"]
    failed: list[dict] = []
    cur_side = None
    for snap in snaps:      # the newest cutoff with at least one verified run
        side, skipped = _load_ours(con, snap)
        failed += skipped
        if side["weeks"]:
            cur_side = side
            break
    if cur_side is not None:
        c = cur_side["cutoff"]
        if cutoff is not None and cutoff >= c:
            raise ValueError("baseline must be older than the latest snapshot")
        ctx["listed"] = [_listed(s) for s in snaps if s["cutoff"] < c]
        older = [s for s in snaps if s["cutoff"] < c]
        if cutoff is not None and not any(s["cutoff"] == cutoff for s in older) and cutoff not in fallback:
            raise ValueError(f"baseline {baseline!r} is not a stored Fantasy Intel snapshot")
        base_side = None
        if cutoff is None or cutoff in {s["cutoff"] for s in older}:
            for snap in (older if cutoff is None else [s for s in older if s["cutoff"] == cutoff]):
                side, skipped = _load_ours(con, snap)
                failed += skipped
                if side["weeks"]:
                    base_side = side
                    break
        used = {c} | ({base_side["cutoff"]} if base_side else set())
        newer = sorted(k for k in idx["rejected"] if k > c)      # later cycles with no usable run at all
        ctx["skipped"] = sorted(failed + [x for k in used | set(newer) for x in idx["rejected"].get(k, [])],
                                key=lambda x: (x["week"], x["run_id"]))
        newer = sorted(set(newer) | {s["cutoff"] for s in snaps if s["cutoff"] > c})
        if newer:
            ctx["notes"].append(f"No run from the newer forecast cycle{'s' if len(newer) != 1 else ''} of "
                                f"{_weeks_text(newer)} passed the kickoff, artifact and integrity checks (see skipped), "
                                f"so the week {c} cycle is the latest compared.")
        between = [k for k in fallback if k < c and (base_side is None or k > base_side["cutoff"])
                   and (cutoff is None or k == cutoff)]
        if between:
            ctx["notes"].append(f"The week {max(between)} forecast was made by {ps.FALLBACK_VERSION}, an earlier model "
                                "version, so it is not compared: movement across model versions is never shown.")
        if base_side is None:   # also name a previous-model forecast of the SAME cutoff (as right after a switch)
            ctx["reason"] = _ours_reason(idx, 1, sorted(k for k in fallback if k <= c and (cutoff is None or k == cutoff)))
        ctx.update(current=cur_side, baseline=base_side)
        return ctx
    ctx["skipped"] = sorted(failed + everything, key=lambda x: (x["week"], x["run_id"]))
    if cutoff is not None and cutoff not in fallback and not any(s["cutoff"] == cutoff for s in snaps):
        raise ValueError(f"baseline {baseline!r} is not a stored Fantasy Intel snapshot")
    ctx["reason"] = _ours_reason(idx, 0, sorted(fallback))
    return ctx


def _ours_reason(idx: dict, n: int, fallback: list[int]) -> str:
    late = sum(x["reason"].startswith("made after") for lst in idx["rejected"].values() for x in lst)
    other = sum(len(lst) for lst in idx["rejected"].values()) - late
    out = [f"Movement needs two stored {ps.MODEL_VERSION} forecasts, each made before its week's first kickoff and "
           f"passing the artifact and integrity checks; {n} such forecast{'s are' if n != 1 else ' is'} stored."]
    if late:
        out.append(f"{late} stored run{'s were' if late != 1 else ' was'} made after its week's first kickoff and "
                   f"{'are' if late != 1 else 'is'} never compared.")
    if other:
        out.append(f"{other} stored run{'s were' if other != 1 else ' was'} rejected (see skipped).")
    if fallback:
        out.append(f"The stored forecast{'s' if len(fallback) != 1 else ''} from {_weeks_text(fallback)} "
                   f"{'were' if len(fallback) != 1 else 'was'} made by {ps.FALLBACK_VERSION}, an earlier model version; "
                   "movement across model versions is never shown.")
    if idx["excluded_runs"]:
        out.append(f"{len(idx['excluded_runs'])} run{'s were' if len(idx['excluded_runs']) != 1 else ' was'} made under "
                   f"different league scoring and {'are' if len(idx['excluded_runs']) != 1 else 'is'} never compared.")
    return " ".join(out)


# ----------------------------------------------------------------------------- comparison


def _weeks_text(weeks) -> str:
    ws = sorted(set(weeks))
    return f"week {ws[0]}" if len(ws) == 1 else "weeks " + ", ".join(map(str, ws[:-1])) + f" and {ws[-1]}"


def _resolve(side: dict, pid: int, weeks: list[int], team: str | None, is_bye, which: str, ours: bool):
    """One player's per-week cells in one snapshot: (cells [{week, value, status, bye, ...}], None), or (None, why)."""
    out = []
    for w in weeks:
        c = side["values"].get(pid, {}).get(w)
        bye = is_bye(team, w)
        if c is None or c["value"] is None:
            if not ours and c is None and bye:      # ESPN lists no row for a schedule bye: 0.0, flagged
                c = {"value": 0.0, "status": "bye"}
            elif ours:
                tail = f" ({COVERAGE.get(c['status'], c['status'].replace('_', ' '))})" if c and c["status"] else ""
                return None, f"no independent estimate for week {w}{tail} in the {which} run"
            else:
                return None, f"no ESPN projection for week {w} in the {which} snapshot"
        # a schedule bye (current team) flags an ESPN week only when ESPN projects nothing for it: a player who
        # changed teams keeps a real projection for his old team's bye week
        espn_bye = not ours and bye and c["value"] == 0.0
        status = "bye" if espn_bye else c["status"]
        out.append({**c, "week": w, "status": status, "bye": bool(espn_bye or c["status"] == "bye"),
                    "horizon": side["horizons"].get(w) if ours else None})
    return out, None


def _px(c: dict, other: dict) -> tuple:
    """(p, x) of one side of a week: a zero-status week is p = 0 with the other side's x (0 when both are zero)."""
    if c["status"] in ps.ZERO_STATUSES:
        return (0.0, 0.0) if other["status"] in ps.ZERO_STATUSES else (0.0, other.get("x"))
    return c.get("p"), c.get("x")


def _components(before: list[dict], after: list[dict]) -> dict | None:
    """Exact availability / per-game split of an ours change (module docstring); None when a week lacks p or x."""
    avail = per_game = 0.0
    for b, a in zip(before, after):
        p0, x0 = _px(b, a)
        p1, x1 = _px(a, b)
        if None in (p0, x0, p1, x1):
            return None
        avail += (p1 - p0) * x0
        per_game += p1 * (x1 - x0)
    return {"availability": avail, "per_game": per_game}


def _split(comps: dict | None, delta: float) -> dict | None:
    """Round the components so they add up to the shown change exactly: stored expected points are rounded to
    2 dp, so p*x differs from them by up to 0.005 a week, and that rounding is left in the per-game part."""
    if comps is None:
        return None
    avail = round(comps["availability"], 2)
    return {"availability": avail, "per_game": round(delta - avail, 2)}


def _ranks(totals: dict[int, float], position: dict[int, str]) -> dict[int, int]:
    """Rank within position by window total, highest first, ties by espn_player_id."""
    out: dict[int, int] = {}
    for pos in {position[p] for p in totals}:
        ordered = sorted((p for p in totals if position[p] == pos), key=lambda p: (-round(totals[p], 2), p))
        out.update({p: i for i, p in enumerate(ordered, 1)})
    return out


def _direction(delta: float) -> str:
    return "up" if delta >= THRESHOLDS["flat"] else "down" if delta <= -THRESHOLDS["flat"] else "flat"


def _players_meta(con, pids: list[int]) -> dict[int, dict]:
    meta = {r["espn_player_id"]: {**r, "gsis_id": None} for r in fetch_dicts(
        con, """SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players
                WHERE espn_player_id IN (SELECT unnest(?))""", [pids])} if pids else {}
    if pids and table_exists(con, "player_id_map"):      # an uncertain mapping is not shown as an id
        for r in fetch_dicts(con, """SELECT espn_player_id, gsis_id FROM player_id_map
                                     WHERE espn_player_id IN (SELECT unnest(?)) AND gsis_id IS NOT NULL
                                       AND match_confidence = 'high'""", [pids]):
            if r["espn_player_id"] in meta:
                meta[r["espn_player_id"]]["gsis_id"] = r["gsis_id"]
    return meta


def _payload(season: int, source: str, view: str, position: str | None, ctx: dict) -> dict:
    ours = source == "independent"
    return {"contract": CONTRACT, "season": season, "source": source, "view": view, "position": position,
            "available": False, "reason": ctx["reason"], "label": ctx["label"], "experimental": False,
            "notes": list(ctx["notes"]), "governance": ps.governance(ps.MODEL_VERSION) if ours else None,
            "baselines": ctx["listed"], "baseline": None, "current": None,
            "window": {"weeks": [], "excluded_weeks": [], "scoring_verified": ours,
                       "scoring_note": OURS_SCORING_NOTE if ours else ESPN_SCORING_NOTE},
            "summary": {"compared": 0, "risers": 0, "fallers": 0, "unchanged": 0, "not_comparable": 0,
                        "not_comparable_reasons": {}},
            "skipped": ctx["skipped"], "excluded_runs": ctx["excluded_runs"], "risers": [], "fallers": [], "players": [],
            "thresholds": dict(THRESHOLDS)}


def movement(con, season: int, *, source: str = "espn", view: str = "ros", position: str | None = None,
             baseline: str | None = None, limit: int = 15) -> dict:
    """The `projection-movers/v1` payload (module docstring). ValueError for an invalid source, view, position or
    baseline (a baseline that is unknown or not older than the latest snapshot). Every mover's `evidence` is None:
    app.analytics.mover_evidence.attach fills it for the listed risers and fallers."""
    source = ps.normalize(source)
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    position = position.upper() if position else None
    if position is not None and position not in POSITIONS:
        raise ValueError(f"position must be one of {', '.join(POSITIONS)}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    baseline = baseline or None
    if baseline is not None and source == "espn":
        try:
            date.fromisoformat(baseline)
        except ValueError:
            raise ValueError(f"baseline {baseline!r} is not an ESPN snapshot id (YYYY-MM-DD)") from None
    ours = source == "independent"
    ctx = _ours_context(con, season, baseline) if ours else _espn_context(con, season, baseline)
    out = _payload(season, source, view, position, ctx)
    cur, base = ctx["current"], ctx["baseline"]
    if cur is not None:
        out["current"] = _public(cur)
    if ctx["reason"] or cur is None or base is None:
        return out
    out["baseline"], out["current"] = _public(base), _public(cur)
    if base["taken_at"] >= cur["taken_at"]:
        out["reason"] = (f"The earlier forecast ({base['label']}) was stored after the latest one ({cur['label']}), so the "
                         "two cannot be compared as a change over time.")
        return out

    # window: the weeks both snapshots forecast that neither saw kick off
    first = pf.first_kickoffs(con, season)
    window, excluded = [], []
    for w in sorted(base["weeks"] | cur["weeks"] | {x["week"] for x in ctx["skipped"]}):
        ko = first.get(w)
        if ctx["final"] is not None and w > ctx["final"]:
            reason = "after the league's final scoring period"
        elif ko is None:
            reason = "kickoff time unknown"
        elif ko <= cur["taken_at"] or (cur["cycle_week"] is not None and w < cur["cycle_week"]):
            reason = LATE
        elif w not in base["weeks"]:
            reason = "not in the earlier snapshot"
        elif w not in cur["weeks"]:
            reason = "not in the latest snapshot"
        else:
            window.append(w)
            continue
        excluded.append({"week": w, "reason": reason})
    if view == "week" and window:
        if window[0] != cur["cycle_week"]:
            out["notes"].append(f"Week {window[0]} is the next week that can be compared"
                                + (f" (week {cur['cycle_week']} could not be)." if cur["cycle_week"] is not None else "."))
        window = window[:1]
    out["window"].update(weeks=window, excluded_weeks=excluded)
    if not window:
        why = sorted({e["reason"] for e in excluded})
        out["reason"] = ("No week can be compared between the two snapshots"
                         + (f" ({'; '.join(why)}; see excluded weeks)." if why else "."))
        return out

    # players
    pids = sorted(set(cur["values"]) | set(base["values"]))
    meta = _players_meta(con, pids)
    position_of = {}
    for pid in pids:
        pos = (meta.get(pid) or {}).get("position") or cur["positions"].get(pid) or base["positions"].get(pid)
        if pos in POSITIONS:
            position_of[pid] = pos
    games, covered = load_schedule(con, season, window)

    def is_bye(team, week):
        return bool(team) and week in covered and (team, week) not in games

    team_of = {pid: (meta.get(pid) or {}).get("pro_team") for pid in position_of}
    sides = {"before": (base, "earlier"), "after": (cur, "latest")}
    resolved = {k: {pid: _resolve(side, pid, window, team_of[pid], is_bye, which, ours) for pid in position_of}
                for k, (side, which) in sides.items()}
    # Rank only the players resolved on both sides: one missing forecast must not move anyone else's rank.
    comparable = [pid for pid in position_of
                  if resolved["before"][pid][0] is not None and resolved["after"][pid][0] is not None]
    totals = {k: {pid: sum(c["value"] for c in res[pid][0]) for pid in comparable} for k, res in resolved.items()}
    ranks = {k: _ranks(totals[k], position_of) for k in totals}

    n, reasons = len(window), {}
    movers, counts = [], {"up": 0, "down": 0, "flat": 0}
    for pid, pos in sorted(position_of.items()):
        if position and pos != position:
            continue
        (before, why_b), (after, why_a) = resolved["before"][pid], resolved["after"][pid]
        if before is None or after is None:
            why = why_b or why_a
            reasons[why] = reasons.get(why, 0) + 1
            continue
        b_raw, a_raw = totals["before"][pid], totals["after"][pid]
        b, a = round(b_raw, 2), round(a_raw, 2)
        delta = round(a_raw - b_raw, 2)
        direction = _direction(delta)
        counts[direction] += 1
        m = meta.get(pid) or {}
        movers.append({
            "espn_player_id": pid, "name": m.get("full_name") or f"ESPN player {pid}", "position": pos, "nfl_team": m.get("pro_team"),
            "gsis_id": m.get("gsis_id"), "before": b, "after": a, "delta": delta,
            "pct": round(delta / b, 4) if b_raw >= THRESHOLDS["pct_floor"] else None,
            "per_week_before": round(b_raw / n, 2), "per_week_after": round(a_raw / n, 2),
            "rank_before": ranks["before"][pid], "rank_after": ranks["after"][pid],
            "rank_change": ranks["before"][pid] - ranks["after"][pid],
            "direction": direction, "relevant": max(b_raw, a_raw) / n >= THRESHOLDS["relevant_per_week"],
            "weeks": [{"week": x["week"], "before": round(x["value"], 2), "after": round(y["value"], 2),
                       "delta": round(y["value"] - x["value"], 2), "bye": x["bye"] and y["bye"],
                       "bye_before": x["bye"], "bye_after": y["bye"],
                       "status_before": x["status"], "status_after": y["status"],
                       "horizon_before": x["horizon"], "horizon_after": y["horizon"]} for x, y in zip(before, after)],
            "components": _split(_components(before, after), delta) if ours else None, "evidence": None})

    key = lambda m: (m["name"] or "", m["espn_player_id"])
    movers_by_delta = sorted(movers, key=lambda m: (-abs(m["delta"]), *key(m)))
    out["risers"] = sorted((m for m in movers if m["relevant"] and m["delta"] >= THRESHOLDS["min_delta"]),
                           key=lambda m: (-m["delta"], *key(m)))[:limit]
    out["fallers"] = sorted((m for m in movers if m["relevant"] and m["delta"] <= -THRESHOLDS["min_delta"]),
                            key=lambda m: (m["delta"], *key(m)))[:limit]
    out["players"] = movers_by_delta[:MAX_PLAYERS]
    out["summary"] = {"compared": len(movers), "risers": counts["up"], "fallers": counts["down"],
                      "unchanged": counts["flat"], "not_comparable": sum(reasons.values()),
                      "not_comparable_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))}
    horizons = [h for side in (base, cur) for w, h in side["horizons"].items() if w in window]
    out["experimental"] = ours and any(h > 0 for h in horizons)
    out["available"] = True
    out["notes"] += _notes(ours, out["experimental"])
    return out


def _notes(ours: bool, experimental: bool) -> list[str]:
    if not ours:
        return []
    notes = [("Fantasy Intel's production model is approved by an owner override and is not shown to be better than "
              "ESPN's projections; movement here describes the model, not accuracy.")]
    if experimental:
        notes.append("A forecast of a week after the next one is experimental and not validated beyond the next week; "
                     "here the earlier forecast of each week looks further ahead than the latest.")
    return notes


def snapshots(con, season: int, source: str) -> list[dict]:
    """Newest-first stored snapshots of the source: {id, label, taken_at (ISO), cycle_week, model_version, run_ids}.
    ours lists only cutoffs that have a candidate run made before its week's kickoff (rows are not read here)."""
    source = ps.normalize(source)
    snaps = _ours_index(con, season)["snaps"] if source == "independent" else _espn_snapshots(con, season)
    return [_public(s) for s in snaps]
