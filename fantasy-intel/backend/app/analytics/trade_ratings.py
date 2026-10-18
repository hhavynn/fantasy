"""Adapter contract between Trade Discovery and the 0-100 player ratings (`ratings-100.v1`,
docs/player_ratings_100.md, developed on its own branch). Read-only; nothing here is a trade input.

The ratings are position-relative mid-rank percentiles of recent opportunity and production. They
are shown in trade evidence as SUPPORTING context next to the quantitative asset baseline (ESPN
points above replacement, analytics/trade_assets.py). The contract this module enforces:

  * Display only. No rating enters a canonical delta, an asset label, a verdict or a ranking.
  * Never a price. A 0-100 rating is a rank within one pool, not a tradable value: ratings are
    never summed across players, never compared across positions, and opportunity ratings with
    different bases (`xfp`, `targets+carries`) are never compared with each other.
  * Missing is missing. A player without a stored 0-100 rating (too few games, no usage data, an
    older database without the columns, or a different scale version) gets None, never 0 and never
    a converted legacy 0-10 value.
  * Versioned. Only SUPPORTED_VERSIONS are read; any other `rating_scale_version` is reported as
    unsupported rather than guessed at.

The ratings branch owns `player_scores` and its columns; this module only reads them, and only when
they exist (`table_columns`), so it works unchanged before and after that branch lands.
"""

from __future__ import annotations

from ..db.connection import fetch_dicts, table_columns, table_exists

SUPPORTED_VERSIONS = ("ratings-100.v1",)
RATING_FIELDS = ("opportunity_score_100", "production_score_100", "value_score_100")
REQUIRED_COLUMNS = (*RATING_FIELDS, "rating_scale_version")
NOTE = ("Ratings are position-relative 0-100 percentiles of recent opportunity and production (ratings-100.v1): "
        "supporting context only. They are not projected points, not a trade price, not comparable across "
        "positions or opportunity bases, and they never change a delta, a label or the order of trades.")


def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "version": None, "players": {}, "note": NOTE}


def load_ratings(con, season: int, espn_ids) -> dict:
    """{available, reason, version, players: {espn_player_id: rating}, note}. A rating is
    {position, opportunity_basis, games, opportunity_score_100, production_score_100,
    value_score_100, rating_scale_version}; players without a supported 0-100 rating are absent."""
    ids = sorted({int(i) for i in espn_ids if i is not None})
    if not table_exists(con, "player_scores") or not table_exists(con, "player_id_map"):
        return unavailable("no player_scores table")
    cols = set(table_columns(con, "player_scores"))
    if not set(REQUIRED_COLUMNS) <= cols:
        return unavailable("player_scores has no 0-100 rating columns (ratings-100.v1 not computed in this database)")
    if not ids:
        return {"available": True, "reason": None, "version": SUPPORTED_VERSIONS[0], "players": {}, "note": NOTE}
    ph = ", ".join("?" * len(ids))
    rows = fetch_dicts(con, f"""
        SELECT m.espn_player_id, s.position, s.opportunity_basis, s.games, s.opportunity_score_100,
               s.production_score_100, s.value_score_100, s.rating_scale_version
        FROM player_id_map m JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = ?
        WHERE m.espn_player_id IN ({ph})""", [season, *ids])
    players, unsupported = {}, set()
    for r in rows:
        if r["rating_scale_version"] not in SUPPORTED_VERSIONS:
            if r["rating_scale_version"] is not None:
                unsupported.add(r["rating_scale_version"])
            continue
        if all(r[k] is None for k in RATING_FIELDS):
            continue
        players[int(r["espn_player_id"])] = {k: r[k] for k in ("position", "opportunity_basis", "games", *RATING_FIELDS,
                                                               "rating_scale_version")}
    reason = f"unsupported rating scale version(s): {', '.join(sorted(unsupported))}" if unsupported else None
    return {"available": True, "reason": reason, "version": SUPPORTED_VERSIONS[0], "players": players, "note": NOTE}


def comparable(a: dict | None, b: dict | None) -> bool:
    """Two ratings may be set side by side only within one pool: same position and, for opportunity,
    the same basis."""
    return bool(a and b and a.get("position") and a.get("position") == b.get("position")
                and a.get("opportunity_basis") == b.get("opportunity_basis"))


def evidence(ratings: dict, sends, receives) -> dict:
    """Per traded player the stored rating (or None) plus the same-pool pairs, for display."""
    players = ratings.get("players") or {}
    out = {pid: players.get(pid) for pid in [*sends, *receives]}
    pairs = [{"you_send": s, "you_receive": t, "position": players[s]["position"]}
             for s in sends for t in receives if comparable(players.get(s), players.get(t))]
    return {"available": ratings.get("available", False), "reason": ratings.get("reason"),
            "version": ratings.get("version"), "players": out, "comparable_pairs": pairs, "note": NOTE}
