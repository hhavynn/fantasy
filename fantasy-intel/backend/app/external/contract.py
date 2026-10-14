"""Generic normalized CSV contract for external player-week utilization.

Pure functions only (no database): column specs, parsing, source keys and
row-level validation. See docs/external_utilization_contract.md.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------- columns --

IDENTITY_COLUMNS = ["season", "week", "provider", "dataset", "provider_player_id", "gsis_id",
                    "player_name", "team", "position", "source_updated_at"]

# metric -> (kind, semantics)
#   kind: count = integer >= 0, rate = fraction in [0, 1], nonneg = float >= 0, any = float
#   semantics: "standard" = definition is compatible with the app-level definition in the
#              contract doc; "provider_defined" = the provider's own definition is preserved
#              as-is and must never be treated as a universal metric.
METRICS: dict[str, tuple[str, str]] = {
    "routes": ("count", "standard"),
    "route_participation": ("rate", "provider_defined"),
    "source_tprr": ("nonneg", "provider_defined"),
    "source_yprr": ("any", "provider_defined"),
    "pass_snaps": ("count", "standard"),
    "pass_block_snaps": ("count", "standard"),
    "pass_block_rate": ("rate", "provider_defined"),
    "slot_rate": ("rate", "provider_defined"),
    "wide_rate": ("rate", "provider_defined"),
    "inline_rate": ("rate", "provider_defined"),
    "backfield_rate": ("rate", "provider_defined"),
    "two_minute_route_share": ("rate", "provider_defined"),
    "long_down_route_share": ("rate", "provider_defined"),
    "short_down_route_share": ("rate", "provider_defined"),
    "man_tprr": ("nonneg", "provider_defined"),
    "zone_tprr": ("nonneg", "provider_defined"),
}
PROVIDER_DEFINED = sorted(m for m, (_, sem) in METRICS.items() if sem == "provider_defined")

# Canonical nflverse stats. If a file carries them they are IGNORED (never stored).
CANONICAL_STAT_COLUMNS = {"targets", "receptions", "receiving_yards", "air_yards", "receiving_air_yards",
                          "target_share", "air_yards_share", "wopr", "fantasy_points", "fantasy_points_ppr",
                          "carries", "rushing_yards", "snaps", "offense_snaps", "offense_pct"}

KNOWN_COLUMNS = set(IDENTITY_COLUMNS) | set(METRICS)
NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan", "-"}
MAX_WEEK = 22   # 18 regular-season weeks + 4 postseason rounds


# ---------------------------------------------------------------- issues --

@dataclass
class RowIssue:
    severity: str                  # error | warning
    check_name: str
    detail: str
    row_number: int | None = None  # 1-based data row (header = row 0)
    week: int | None = None
    source_player_key: str | None = None
    player_name: str | None = None
    provider_player_id: str | None = None


@dataclass
class ParsedFile:
    rows: list[dict] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[RowIssue]:
        return [i for i in self.issues if i.severity == "error"]


# --------------------------------------------------------------- helpers --

def normalize_exact_name(name: str | None) -> str:
    """Strict normalization for identity: lowercase, strip accents/punctuation, collapse
    spaces. Suffixes (Jr., II...) are KEPT, so 'Marvin Harrison' != 'Marvin Harrison Jr.'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("'", "").replace(".", ""))
    return " ".join(s.split())


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_player_key(provider_player_id: str | None, gsis_id: str | None, player_name: str | None,
                      team: str | None, position: str | None) -> str | None:
    """Stable per-provider identity for a source row (never only gsis-based when the
    provider has its own id). Name-derived keys hash the normalized identity fields."""
    if provider_player_id:
        return f"id:{provider_player_id}"
    if gsis_id:
        return f"gsis:{gsis_id}"
    if player_name and team and position:
        raw = "|".join([normalize_exact_name(player_name), team.upper(), position.upper()])
        return "nm:" + hashlib.sha1(raw.encode()).hexdigest()[:16]
    return None


def _is_null(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in NULL_TOKENS)


def _parse_number(v: Any, kind: str) -> float | int | None:
    if _is_null(v):
        return None
    x = float(str(v).strip())
    if x != x:  # NaN
        return None
    if kind == "count":
        if x != int(x):
            raise ValueError("must be a whole number")
        return int(x)
    return x


def _parse_ts(v: Any) -> datetime | None:
    """ISO-8601 -> UTC-naive datetime (the app's persisted timestamp convention).
    With an explicit offset or trailing Z: converted to UTC, then tzinfo dropped.
    Without an offset: interpreted as already UTC. Never depends on the machine's timezone."""
    if _is_null(v):
        return None
    s = str(v).strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _text(v: Any) -> str | None:
    return None if _is_null(v) else str(v).strip()


# ----------------------------------------------------------------- parse --

def parse_rows(header: list[str], records: list[dict], *, provider: str | None, dataset: str | None) -> ParsedFile:
    """Parse + validate normalized CSV records. Hard errors make the whole file invalid;
    warnings are kept. NULL (blank/NA) is preserved as None; an explicit 0 stays 0."""
    out = ParsedFile()
    cols = [c.strip() for c in header]
    out.ignored_columns = [c for c in cols if c not in KNOWN_COLUMNS]
    for c in out.ignored_columns:
        if c in CANONICAL_STAT_COLUMNS:
            out.issues.append(RowIssue("warning", "canonical_stat_ignored",
                                       f"column '{c}' ignored: nflverse is canonical for this stat"))
        else:
            out.issues.append(RowIssue("warning", "unknown_column_ignored", f"column '{c}' is not in the contract"))

    for n, rec in enumerate(records, start=1):
        rec = {k.strip(): v for k, v in rec.items() if k is not None}
        r: dict[str, Any] = {}
        try:
            r["season"] = _parse_number(rec.get("season"), "count")
            r["week"] = _parse_number(rec.get("week"), "count")
        except ValueError:
            out.issues.append(RowIssue("error", "invalid_season_week", "season/week must be whole numbers",
                                       row_number=n))
            continue
        r["provider"] = _text(rec.get("provider")) or provider
        r["dataset"] = _text(rec.get("dataset")) or dataset
        for c in ("provider_player_id", "gsis_id", "player_name", "team", "position"):
            r[c] = _text(rec.get(c))
        if r["team"]:
            r["team"] = r["team"].upper()
        if r["position"]:
            r["position"] = r["position"].upper()
        ctx = dict(row_number=n, week=r["week"], player_name=r["player_name"],
                   provider_player_id=r["provider_player_id"])

        def err(name, detail):
            out.issues.append(RowIssue("error", name, detail, **ctx))

        if r["season"] is None or not (1999 <= r["season"] <= 2100):
            err("invalid_season", f"season {rec.get('season')!r} is missing or invalid")
        if r["week"] is None or not (1 <= r["week"] <= MAX_WEEK):
            err("invalid_week", f"week {rec.get('week')!r} must be 1..{MAX_WEEK}")
        if not r["provider"]:
            err("missing_provider", "provider missing (column or --provider)")
        if not r["dataset"]:
            err("missing_dataset", "dataset missing (column or --dataset)")
        try:
            r["source_updated_at"] = _parse_ts(rec.get("source_updated_at"))
        except ValueError:
            err("invalid_source_updated_at", f"unparseable source_updated_at {rec.get('source_updated_at')!r}")
            r["source_updated_at"] = None
        key = source_player_key(r["provider_player_id"], r["gsis_id"], r["player_name"], r["team"], r["position"])
        if key is None:
            err("missing_identity", "need provider_player_id, gsis_id, or player_name + team + position")
        r["source_player_key"] = key
        ctx["source_player_key"] = key

        for m, (kind, _) in METRICS.items():
            try:
                r[m] = _parse_number(rec.get(m), kind)
            except ValueError as e:
                err("non_numeric_value", f"{m}={rec.get(m)!r}: {e}")
                r[m] = None
                continue
            v = r[m]
            if v is None:
                continue
            if kind in ("count", "nonneg") and v < 0:
                err(f"negative_{m}", f"{m} = {v} < 0")
            if kind == "rate" and not (0.0 <= v <= 1.0):
                err(f"{m}_out_of_range", f"{m} = {v} outside [0, 1] (rates are fractions, not percents)")
        if r.get("pass_snaps") is not None and r.get("pass_block_snaps") is not None \
                and r["pass_block_snaps"] > r["pass_snaps"]:
            err("pass_block_gt_pass_snaps", f"pass_block_snaps {r['pass_block_snaps']} > pass_snaps {r['pass_snaps']}")

        # Ratios are undefined with zero routes: never store a fake 0.0.
        if r.get("routes") == 0:
            for m in ("source_tprr", "source_yprr", "man_tprr", "zone_tprr"):
                if r.get(m) is not None:
                    out.issues.append(RowIssue("warning", "ratio_with_zero_routes",
                                               f"{m} = {r[m]} supplied with routes = 0; stored as NULL", **ctx))
                    r[m] = None
        if r.get("routes") is not None and r.get("pass_snaps") is not None and r["routes"] > r["pass_snaps"]:
            out.issues.append(RowIssue("warning", "routes_gt_pass_snaps",
                                       f"routes {r['routes']} > pass_snaps {r['pass_snaps']}", **ctx))
        if all(r.get(m) is not None for m in ("routes", "pass_block_snaps", "pass_snaps")) \
                and r["routes"] + r["pass_block_snaps"] > r["pass_snaps"]:
            out.issues.append(RowIssue("warning", "routes_plus_blocks_gt_pass_snaps",
                                       "routes + pass_block_snaps > pass_snaps (provider definitions may differ)",
                                       **ctx))
        r["row_number"] = n
        out.rows.append(r)

    seen: dict[tuple, int] = {}
    for r in out.rows:
        if r["source_player_key"] is None:
            continue
        k = (r["provider"], r["dataset"], r["season"], r["week"], r["source_player_key"])
        if k in seen:
            out.issues.append(RowIssue("error", "duplicate_source_key",
                                       f"duplicate provider/dataset/season/week/source key {k[2:]} "
                                       f"(rows {seen[k]} and {r['row_number']})", row_number=r["row_number"],
                                       week=r["week"], source_player_key=r["source_player_key"],
                                       player_name=r["player_name"], provider_player_id=r["provider_player_id"]))
        else:
            seen[k] = r["row_number"]
    return out


def derived_ratio(numerator: float | None, routes: int | None) -> float | None:
    """App-level TPRR / YPRR: nflverse numerator / external routes. NULL when routes is
    missing or zero (undefined), NULL when the nflverse numerator is unknown."""
    if routes is None or routes <= 0 or numerator is None:
        return None
    return numerator / routes
