"""Immutable, content-addressed snapshots of the source frames a frozen pregame forecast used.

Why: the nflverse sync REPLACES each season's injury reports, weekly rosters, schedules, box
scores, snaps and expected points on every run (app/nfl/sync.py, replace_frame_partition), and the
play-by-play injury evidence file is rewritten. Without a copy, a later correction or a report
updated after kickoff would silently change what a past forecast appears to have known.

What is captured (per forecast, for the target season and the PRIOR_SEASONS before it, which is
every season the feature pipeline reads): the exact `Sources` frames the forecast used, one parquet
file per (family, season), named by the sha256 of its content. Identical content is stored once.
A write-once manifest per capture lists the parts. Nothing is ever overwritten: a corrected source
produces a new part and a new manifest; earlier ones stay byte-identical. Generated files live in
the git-ignored weekly model directory and are never published.

Timestamps (never invented or backfilled):
  event         the NFL week (season, week) and the team's kickoff from the schedule
  published     provider publication time: only nflverse injury rows carry one (`date_modified`),
                and many 2025-2026 rows have none; its coverage is recorded, never imputed
  retrieved_at  upper bound on when we downloaded the content: the finish time of the sync that
                wrote the table (sync_runs) or the play-by-play file's write time; null if unknown
  captured_at   when this snapshot was taken (the forecast's creation time); always known
  known_by      retrieved_at when known, else captured_at: the latest time we provably had it. For a
                team's injury rows, the verified per-team retrieval time from the `injury_retrievals` log
                (manifest field `injury_retrievals`, added by newer captures) is preferred when present,
                and is never later than captured_at. Older manifests lack the field and read as before.
  used_at       the ledger run's created_at (the forecast that read the manifest)
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import polars as pl

from .forecast_features import (EXITS_SCHEMA, INJURY_SCHEMA, POSITIONS, PRIOR_SEASONS, ROSTER_SCHEMA, Sources,
                                _panel_schema, classify_games, key)

SNAPSHOT_SCHEMA = "source-snapshot/v1"
FAMILIES = ("panel", "games", "defense", "rosters", "injuries", "exits")
REQUIRED = ("panel", "games", "defense")
CANONICAL = {"panel": _panel_schema(), "rosters": ROSTER_SCHEMA, "injuries": INJURY_SCHEMA, "exits": EXITS_SCHEMA}
PARTICIPATION_INJURIES = "injuries_participation"     # week-W designations incl. the ESPN-status fallback

# Evidence behind the "injury" reading of a player's last pre-forecast game (`injury_evidence`).
PBP_VERIFIED = "pbp_injury_mention"                       # play-by-play says he was injured in that game
REPORT_LISTING = "injury_report_listing"                  # snap drop + a NEW injury-report listing after it
SNAP_DROP_ONLY = "snap_drop_without_injury_evidence"      # snap drop, no injury evidence (role, rest, script...)
NO_DROP = "no_snap_drop"
UNKNOWN = "unknown"                                       # no snap share or no reference games


class ProvenanceError(Exception):
    """A snapshot is missing, unreadable or does not match its content hash."""


def _utc_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def frame_hash(df: pl.DataFrame) -> str:
    """sha256 of the schema and the rows in a canonical order (independent of row order)."""
    h = hashlib.sha256(json.dumps([[c, str(t)] for c, t in df.schema.items()]).encode())
    h.update(df.sort(df.columns, nulls_last=True).write_csv().encode() if df.height else b"")
    return h.hexdigest()


def _canonical(family: str, df: pl.DataFrame) -> pl.DataFrame:
    schema = CANONICAL.get(family)
    if schema is None:
        return df
    return df.cast({c: t for c, t in schema.items() if c in df.columns}, strict=False).select(
        [c for c in schema if c in df.columns])


def _link_into_place(tmp: str, path: Path) -> bool:
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    return True


def _write_part(root: Path, family: str, season: int, df: pl.DataFrame) -> str:
    df = df.sort(df.columns, nulls_last=True)            # canonical row order: same content, same bytes
    digest = frame_hash(df)
    path = Path(root) / "snapshots" / family / f"s{season}" / f"{digest}.parquet"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{digest}.", suffix=".partial")
        os.close(fd)
        try:
            df.write_parquet(tmp)
        except BaseException:
            os.unlink(tmp)
            raise
        _link_into_place(tmp, path)
    return digest


def read_part(root: Path, family: str, season: int, digest: str) -> pl.DataFrame:
    path = Path(root) / "snapshots" / family / f"s{season}" / f"{digest}.parquet"
    if not path.exists():
        raise ProvenanceError(f"snapshot {path} is missing: the forecast cannot be reconstructed (restore it from "
                              "a backup; never substitute the current database)")
    try:
        df = pl.read_parquet(path)
    except Exception as err:
        raise ProvenanceError(f"snapshot {path} is unreadable: {err}") from err
    if frame_hash(df) != digest:
        raise ProvenanceError(f"snapshot {path} does not match its content hash")
    return df


def capture(root: Path, sources: Sources, season: int, week: int, *, captured_at, retrieved_at: dict | None = None,
            meta: dict | None = None, extra: dict[str, pl.DataFrame] | None = None, con=None) -> dict:
    """Snapshot every source family for seasons [season - PRIOR_SEASONS, season] and write a
    write-once manifest. `retrieved_at`: {family: {season: timestamp | None}}. `extra`: additional
    week-level frames (e.g. PARTICIPATION_INJURIES). An identical capture returns the existing
    manifest (its earlier captured_at is kept: the content was known since then).
    `con`: the live database; when given, the manifest also records per team the latest verified
    injury retrieval (injury_freshness.manifest_view: strictly before the capture and the team's
    kickoff). Without it the field is absent, exactly as in manifests made before it existed."""
    seasons = list(range(season - PRIOR_SEASONS, season + 1))
    parts = {}
    for fam in FAMILIES:
        frame = getattr(sources, fam)
        for s in seasons:
            k = f"{fam}/s{s}"
            if frame is None:
                parts[k] = {"status": "unavailable", "hash": None, "rows": 0, "retrieved_at": None}
                continue
            part = _canonical(fam, frame.filter(pl.col("season") == s))
            parts[k] = {"status": "captured", "hash": _write_part(root, fam, s, part), "rows": part.height,
                        "retrieved_at": _utc_iso(((retrieved_at or {}).get(fam) or {}).get(s))}
    for name, frame in (extra or {}).items():
        part = _canonical("injuries", frame)
        parts[f"{name}/s{season}w{week:02d}"] = {"status": "captured", "hash": _write_part(root, name, season, part),
                                                 "rows": part.height, "retrieved_at": None}
    body = {"schema": SNAPSHOT_SCHEMA, "season": season, "week": week, "seasons": seasons, "parts": parts,
            "meta": meta or {}}
    if con is not None:       # additive field: absent in older manifests, which still load and reconstruct
        from .injury_freshness import manifest_view
        body["injury_retrievals"] = manifest_view(con, season, week, captured_at)
    mid = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    path = Path(root) / "snapshots" / "manifests" / f"s{season}w{week:02d}" / f"{mid}.json"
    if path.exists():
        return load_manifest(root, season, week, mid)
    manifest = {**body, "manifest_id": mid, "captured_at": _utc_iso(captured_at)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{mid}.", suffix=".partial")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(manifest, indent=1, default=str))
    if not _link_into_place(tmp, path):
        return load_manifest(root, season, week, mid)
    return manifest


def load_manifest(root: Path, season: int, week: int, manifest_id: str) -> dict:
    path = Path(root) / "snapshots" / "manifests" / f"s{season}w{week:02d}" / f"{manifest_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise ProvenanceError(f"source manifest {path} is missing or unreadable ({err.__class__.__name__})") from err


def manifests(root: Path, season: int) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((Path(root) / "snapshots" / "manifests").glob(f"s{season}w*/*.json"))]


def reconstruct(root: Path, manifest: dict) -> tuple[Sources, dict[str, pl.DataFrame]]:
    """The Sources (and extra frames) exactly as captured. Never falls back to a live table."""
    frames = {}
    for fam in FAMILIES:
        parts = []
        for s in manifest["seasons"]:
            p = manifest["parts"].get(f"{fam}/s{s}") or {}
            if p.get("status") == "captured":
                parts.append(read_part(root, fam, s, p["hash"]))
        if fam in REQUIRED and not parts:
            raise ProvenanceError(f"manifest {manifest['manifest_id']} has no {fam} snapshot")
        frames[fam] = _canonical(fam, pl.concat(parts, how="vertical_relaxed")) if parts else None
    frames["panel"] = frames["panel"].sort("season", "week", "gsis_id", maintain_order=True)   # as Sources.concat
    extra = {}
    for k, p in manifest["parts"].items():
        name, _, sw = k.partition("/")
        if name not in FAMILIES and p.get("status") == "captured":
            extra[name] = read_part(root, name, manifest["season"], p["hash"])
    return Sources(**frames), extra


def known_by(manifest: dict, part: str, team: str | None = None) -> datetime | None:
    """The latest time we provably had `part`. With `team` and an injuries part, the verified per-team
    retrieval time recorded in the manifest (`injury_retrievals`) wins over the part's sync-level upper
    bound, capped at captured_at so it can never be later than the capture. Nothing is invented: no
    recorded retrieval for the team means the unchanged rule (retrieved_at, else captured_at)."""
    p = manifest["parts"].get(part) or {}
    ts = p.get("retrieved_at") or manifest.get("captured_at")
    if team is not None and part.startswith("injuries/"):
        hit = ((manifest.get("injury_retrievals") or {}).get("teams") or {}).get(team) or {}
        if hit.get("retrieved_at") and hit.get("source") == "nflverse":
            ts, cap = hit["retrieved_at"], manifest.get("captured_at")
            if cap and datetime.fromisoformat(cap) < datetime.fromisoformat(ts):
                ts = cap
    return datetime.fromisoformat(ts) if ts else None


def injuries_as_of(root: Path, season: int, week: int, kickoffs: dict[str, datetime]) -> tuple[pl.DataFrame, dict]:
    """The week-W injury report rows of each team as we knew them strictly before THAT team's
    kickoff, from the persisted snapshots alone (never the current table). Teams with no snapshot
    known before kickoff get no rows and are listed under `no_pregame_snapshot`."""
    part = f"injuries/s{season}"
    caps = [m for m in manifests(root, season) if (m["parts"].get(part) or {}).get("status") == "captured"]
    rows, used, missing = [], {}, []
    for team, ko in sorted(kickoffs.items()):
        # Applying week W's per-team retrieval time here is valid because every nflverse sync downloads and
        # replaces the whole season feed, so the snapshot's season-wide injuries part was written by that
        # same download for every team (manifest_view only records a time the writing sync itself logged).
        before = [(t, m) for t, m in ((known_by(m, part, team), m) for m in caps) if t is not None and t < ko]
        if not before:
            missing.append(team)
            continue
        t, m = max(before, key=lambda c: (c[0], c[1].get("captured_at") or ""))
        df = read_part(root, "injuries", season, m["parts"][part]["hash"])
        rows.append(df.filter((pl.col("week") == week) & (pl.col("team") == team)))
        used[team] = {"manifest_id": m["manifest_id"], "known_by": t.isoformat(), "kickoff": ko.isoformat()}
    frame = pl.concat(rows, how="vertical_relaxed") if rows else pl.DataFrame(schema=INJURY_SCHEMA)
    return frame, {"teams": used, "no_pregame_snapshot": missing}


# ----------------------------------------------------------------------------- source metadata


def injury_timestamp_coverage(con, season: int) -> dict:
    """How many nflverse injury rows of a season carry a provider timestamp (date_modified), and
    how many have no player id (they cannot reach any model). Read-only."""
    from ..db.connection import table_columns, table_exists

    if con is None or not table_exists(con, "nfl_injuries"):
        return {"rows": 0, "with_date_modified": None, "column_present": False, "unmapped_rows": 0}
    cols = set(table_columns(con, "nfl_injuries"))
    dm = "count(date_modified)" if "date_modified" in cols else "NULL"
    rows, with_dm, unmapped = con.execute(
        f"SELECT count(*), {dm}, count(*) - count(gsis_id) FROM nfl_injuries WHERE season = ?", [season]).fetchone()
    return {"rows": int(rows), "with_date_modified": None if with_dm is None else int(with_dm),
            "column_present": "date_modified" in cols, "unmapped_rows": int(unmapped)}


def publication_time_status(cov: dict | None) -> str:
    if not cov or not cov.get("column_present"):
        return "missing"
    if not cov.get("rows"):
        return "unknown"
    share = (cov.get("with_date_modified") or 0) / cov["rows"]
    return "available" if share == 1 else ("partial" if share > 0 else "missing")


def source_meta(coverage: dict[int, dict] | None = None, fallback_teams=()) -> dict:
    try:
        from importlib.metadata import version
        reader = version("nflreadpy")
    except Exception:
        reader = None
    return {"provider": "nflverse", "reader": {"nflreadpy": reader},
            "injury_report_timestamps": {str(s): {**c, "publication_time": publication_time_status(c)}
                                         for s, c in sorted((coverage or {}).items())},
            "espn_status_fallback_teams": sorted(fallback_teams)}


# ----------------------------------------------------------------------------- injury evidence


def injury_evidence(sources: Sources, season: int, week: int, gsis_ids: list[str],
                    report_times: dict | None = None) -> dict[str, dict]:
    """Per player, what supports the injury reading of his LAST game before the forecast, using the
    forecast's own classification (forecast_features.classify_games, same inputs). Descriptive only:
    nothing here is a model input. A snap drop alone is never called an injury."""
    cutoff = key(season, week)
    panel = sources.panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff)
                                 & (pl.col("season") >= season - PRIOR_SEASONS)
                                 & pl.col("gsis_id").is_in(gsis_ids) & pl.col("position").is_in(POSITIONS))
    if panel.height == 0:
        return {}
    cp = classify_games(panel, sources, season, week)
    last = cp.sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).first()
    pbp, through = set(), {}
    if sources.exits is not None:
        ex = sources.exits.filter(pl.col("season") * 100 + pl.col("week") < cutoff)
        pbp = {(int(s), int(w), g) for s, w, g in ex.filter(pl.col("injured_in_game") == 1)
               .select("season", "week", "gsis_id").iter_rows()}
        through = {int(s): int(w) for s, w in ex.group_by("season").agg(pl.col("week").max()).iter_rows()}
    out = {}
    for r in last.iter_rows(named=True):
        s, w, g, cls = int(r["season"]), int(r["week"]), r["gsis_id"], r["gclass"]
        if (s, w, g) in pbp:
            ev = PBP_VERIFIED
        elif cls == "injury_exit":
            ev = REPORT_LISTING
        elif cls in ("role_low", "listed_low", "blowout_low"):
            ev = SNAP_DROP_ONLY
        elif cls == "full":
            ev = NO_DROP
        else:
            ev = UNKNOWN
        out[g] = {"last_game": [s, w], "game_class": cls, "evidence": ev,
                  "pbp_covered": s in through and w <= through[s],
                  "report_publication_time": (report_times or {}).get(str(s), {}).get("publication_time", "unknown")
                  if ev == REPORT_LISTING else None}
    return out


__all__ = ["capture", "reconstruct", "load_manifest", "manifests", "injuries_as_of", "injury_evidence",
           "frame_hash", "source_meta", "injury_timestamp_coverage", "ProvenanceError", "SNAPSHOT_SCHEMA"]
