"""Golden responses for every legacy GET endpoint (Phase 2 of the multi-league plan).

`cases(app, con)` enumerates the live router's GET routes outside /v2, fills path parameters from the database (a
rostered player, a team, a GSIS id), and adds the perspective variant (`team_id`) for routes that accept one. `mask()`
replaces values that legitimately change between two builds of the same data (wall-clock timestamps, the data
version) with a placeholder, so two responses are compared on content only. Goldens are pretty-printed JSON with
sorted keys: a diff in review shows exactly which field changed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

GOLDEN_DIR = Path(__file__).resolve().parent
MASK = "<masked>"
# Keys whose values are wall-clock or build stamps, not data.
VOLATILE_KEYS = {"computed_at", "generated_at", "synced_at", "published_at", "data_version", "fetched_at",
                 "league_synced_at", "roster_synced_at", "last_synced_at", "refreshed_at", "built_at", "as_of",
                 "now", "server_time", "created_at", "updated_at", "checked_at", "run_at", "started_at",
                 "finished_at", "source_updated_at", "snapshot_at", "captured_at", "run_id", "snapshot_version"}
# Search diagnostics: wall time and work counters, which depend on what an in-process cache already held.
VOLATILE_PREFIXES = ("lineup_solves", "exact_evaluations_cached")
VOLATILE_DIAGNOSTICS = {"seconds"}
# ISO-8601 datetimes with a time part (dates alone are data: game dates, weeks).
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
# A sync stamp inside text ("ESPN injury status INJURY_RESERVE (synced 2026-10-20 18:02:11.5)"); other datetimes in
# text are data (kickoffs)
_SYNCED_STAMP = re.compile(r"(synced )\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")


@dataclass(frozen=True)
class Case:
    name: str          # file stem, stable and filesystem-safe
    path: str          # request path with query string, relative to the API root (e.g. /api/overview?team_id=2)


def legacy_get_routes(app) -> list:
    """The legacy single-league GET routes (full paths outside /v2), sorted by path."""
    from app.api.routing import iter_routes
    out = [r for r in iter_routes(app) if "GET" in r.methods and r.path.startswith("/api")]
    return sorted(out, key=lambda r: r.path)


def _accepts(info, name: str) -> bool:
    return any(p.name == name for p in info.route.dependant.query_params)


def sample_ids(con) -> dict:
    """Path parameter values present in the database: two rostered players, the second team, a GSIS id."""
    q = lambda sql: (con.execute(sql).fetchone() or [None])[0]   # noqa: E731
    return {
        "espn_player_id": q("SELECT espn_player_id FROM lineup_snapshots ORDER BY espn_player_id LIMIT 1"),
        "other_player_id": q("SELECT DISTINCT espn_player_id FROM lineup_snapshots ORDER BY espn_player_id LIMIT 1 OFFSET 1"),
        # a four-player shortlist (issue #175): the first rostered RB, WR and TE by id, then the first QB
        "shortlist_ids": ",".join(str(x[0]) for x in con.execute(
            "SELECT min(l.espn_player_id) FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id) "
            "WHERE fp.position IN ('RB', 'WR', 'TE', 'QB') GROUP BY fp.position "
            "ORDER BY fp.position = 'QB', fp.position").fetchall()),
        "team_id": q("SELECT team_id FROM fantasy_teams ORDER BY team_id LIMIT 1 OFFSET 1"),
        "gsis_id": q("SELECT gsis_id FROM player_id_map WHERE gsis_id IS NOT NULL ORDER BY gsis_id LIMIT 1"),
    }


# Routes whose required query parameters cannot be inferred from the signature alone.
def _required_query(path: str, ids: dict, client) -> dict | None:
    if path == "/api/compare":
        return {"a": ids["espn_player_id"], "b": ids["other_player_id"]}
    if path == "/api/compare/shortlist":
        # Four rostered players for the league's FLEX slot (issue #175), when the league starts one.
        q = {"ids": ids["shortlist_ids"]}
        body = client.get(path + "?" + urlencode(q)).json() if client is not None else {}
        if any(s.get("slot") == "FLEX" for s in body.get("slots") or []):
            q["slot"] = "FLEX"
        return q
    if path == "/api/research/opportunity":
        return {"position": "WR"}
    if path == "/api/trades/discover/evidence":
        rows = client.get("/api/trades/discover").json() if client is not None else {}
        key = next((x["key"] for items in (rows.get("sections") or {}).values() for x in items), None)
        return {"key": key} if key else None
    if path == "/api/waivers/add-drop":
        # One evaluated pair: the best-projected available player for the owner's lowest-projected active player (the
        # team variant keeps this drop, so it records the "not on this roster" answer).
        opts = (client.get(path).json().get("options") or {}) if client is not None else {}
        add = next((o["espn_player_id"] for o in opts.get("add") or []), None)
        active = [o for o in opts.get("drop") or [] if not (o["on_ir"] or o["on_taxi"])]
        drop = min(active, key=lambda o: (o["known_projected_points"] is None, o["known_projected_points"] or 0,
                                          o["espn_player_id"]), default=None)
        q = {k: v for k, v in (("add", add), ("drop", drop and drop["espn_player_id"])) if v is not None}
        return q or None
    if path == "/api/waivers/claim-plan":
        # A three-claim plan from the add/drop pickers: the two best-projected available players both dropping the
        # owner's lowest-projected active player (a shared-drop conflict, with bids), then an add-only claim.
        opts = (client.get("/api/waivers/add-drop").json().get("options") or {}) if client is not None else {}
        adds = [o["espn_player_id"] for o in opts.get("add") or []][:3]
        active = [o for o in opts.get("drop") or [] if not (o["on_ir"] or o["on_taxi"])]
        drop = min(active, key=lambda o: (o["known_projected_points"] is None, o["known_projected_points"] or 0,
                                          o["espn_player_id"]), default=None)
        if len(adds) < 3 or drop is None:
            return None
        d = drop["espn_player_id"]
        return {"claims": f"{adds[0]}:{d}:5,{adds[1]}:{d}:3,{adds[2]}"}
    if path == "/api/waivers/faab":
        # The player on waivers and a free agent, both dropping the owner's IR-slot occupant when there is one (which
        # clears the fixture's IR block, so the guidance itself is reached: no gain, no bid needed). Neither claim gets
        # a range in this fixture, so the combined check records no exclusive group. Otherwise the claim plan's three
        # claims without bids.
        opts = (client.get("/api/waivers/add-drop").json().get("options") or {}) if client is not None else {}
        adds = opts.get("add") or []
        waiver = next((o["espn_player_id"] for o in adds if o.get("availability_status") == "WAIVERS"), None)
        free = next((o["espn_player_id"] for o in adds if o.get("availability_status") == "FREEAGENT"), None)
        ir = next((o["espn_player_id"] for o in opts.get("drop") or [] if o["on_ir"]), None)
        if waiver is not None and free is not None and ir is not None:
            return {"claims": f"{waiver}:{ir},{free}:{ir}"}
        q = _required_query("/api/waivers/claim-plan", ids, client)
        if not q:
            return None
        return {"claims": ",".join(":".join(c.split(":")[:2]).rstrip(":") for c in q["claims"].split(","))}
    if path == "/api/trades/scenarios":
        # Four saved trades (issue #183) from the analyzer's rosters: the owner's first player for another team's
        # first, the same player for that team's second (a shared-player conflict), a stale one naming a third team's
        # player as the other team's (a roster move since saving), then the first trade with its sides swapped (a
        # duplicate).
        teams = (client.get("/api/trades/options").json().get("teams") or []) if client is not None else []
        mine = next((t for t in teams if t["is_mine"] and t["roster"]), None)
        others = [t for t in teams if mine and t["team_id"] != mine["team_id"] and t["roster"]]
        if mine is None or len(others) < 2 or len(others[0]["roster"]) < 2:
            return None
        m, o, third = mine["team_id"], others[0], others[1]
        give, get1, get2 = mine["roster"][0]["espn_player_id"], o["roster"][0]["espn_player_id"], o["roster"][1]["espn_player_id"]
        moved = third["roster"][0]["espn_player_id"]
        return {"scenarios": f"{m}:{o['team_id']}:{give}:{get1},{m}:{o['team_id']}:{give}:{get2},"
                             f"{m}:{o['team_id']}:{give}:{moved},{o['team_id']}:{m}:{get1}:{give}"}
    return {}


# Query variants recorded beside a route's default answer (and so replayed through /v2 by the league-scope parity and
# colliding-ids tests): targeted trade discovery (issue #182) by position, by a named RB on another roster and by
# the bye need of the thinnest roster (in the E2E fixture, team 4, whose every week is short). Ids come from the data.
def extra_cases(con) -> list[Case]:
    q = lambda sql: (con.execute(sql).fetchone() or [None])[0]   # noqa: E731
    latest = "(SELECT max(scoring_period) FROM lineup_snapshots)"
    player = q(f"SELECT l.espn_player_id FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id) "
               f"WHERE l.scoring_period = {latest} AND fp.position = 'RB' AND l.team_id <> "
               "(SELECT team_id FROM fantasy_teams WHERE is_mine ORDER BY team_id LIMIT 1) "
               "ORDER BY l.team_id, l.espn_player_id LIMIT 1")
    thin = q(f"SELECT team_id FROM lineup_snapshots WHERE scoring_period = {latest} GROUP BY team_id "
             "ORDER BY count(*), team_id LIMIT 1")
    rival = q("SELECT team_id FROM fantasy_teams WHERE NOT coalesce(is_mine, false) ORDER BY team_id LIMIT 1")
    out = [Case("api_trades_discover_summary__targeted_rb", "/api/trades/discover/summary?mode=targeted&target_position=RB")]
    if rival is not None:
        # League analyzer (issue #191): the owner's team compared with the first other team, over this week only.
        out.append(Case("api_league_analyzer__compare_week", f"/api/league-analyzer?vs={rival}&horizon=week"))
    if player is not None:
        out.append(Case("api_trades_discover_summary__targeted_player",
                        f"/api/trades/discover/summary?mode=targeted&target_player={player}"))
    if thin is not None:
        out.append(Case("api_trades_discover_summary__targeted_bye",
                        f"/api/trades/discover/summary?mode=targeted&need=bye&team_id={thin}"))
    # The playoff-race what-if (issue #192): the first two undecided regular-season games, one given to its home side
    # and one forced unresolved, so the answer carries the `whatif` block.
    open_games = [r[0] for r in con.execute(
        "SELECT matchup_id FROM matchups WHERE coalesce(winner, '') NOT IN ('HOME', 'AWAY', 'TIE') "
        "AND coalesce(playoff_tier, 'NONE') = 'NONE' AND home_team_id IS NOT NULL AND away_team_id IS NOT NULL "
        "ORDER BY matchup_period, matchup_id LIMIT 2").fetchall()]
    if len(open_games) == 2:
        out.append(Case("api_playoff_race__whatif", "/api/playoff-race?" + urlencode(
            [("override", f"{open_games[0]}:home"), ("override", f"{open_games[1]}:none")])))
    return out


def cases(app, con, client=None) -> list[Case]:
    """Every legacy GET case. `client` (a TestClient on the legacy API) resolves data-dependent parameters such as
    a trade-discovery row key; without it those routes are recorded with their default (validation) answer."""
    ids = sample_ids(con)
    out = []
    for r in legacy_get_routes(app):
        path = r.path
        for name, value in ids.items():
            path = path.replace("{" + name + "}", str(value))
        if "{" in path:
            raise AssertionError(f"no sample value for a path parameter in {r.path}")
        stem = re.sub(r"[^A-Za-z0-9]+", "_", r.path.strip("/")).strip("_") or "root"
        required = _required_query(r.path, ids, client)
        query = "?" + urlencode(required) if required else ""
        out.append(Case(stem, path + query))
        if _accepts(r, "team_id") and "{team_id}" not in r.path:
            sep = "&" if query else "?"
            out.append(Case(stem + "__team", path + query + sep + urlencode({"team_id": ids["team_id"]})))
    routes = {r.path for r in legacy_get_routes(app)}
    out += [c for c in extra_cases(con) if c.path.partition("?")[0] in routes]
    return out


def mask(value, key: str | None = None):
    if key is not None and (key in VOLATILE_KEYS or key in VOLATILE_DIAGNOSTICS or key.startswith(VOLATILE_PREFIXES)) \
            and value is not None:
        return MASK                      # the whole value, even an object (e.g. per-stage timings)
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str) and _DATETIME.match(value):
        return MASK
    if isinstance(value, str):
        return _SYNCED_STAMP.sub(r"\1" + MASK, value)
    return value


def render(status: int, body) -> str:
    return json.dumps({"status": status, "body": mask(body)}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def response_text(client, case: Case, prefix: str = "") -> str:
    r = client.get(prefix + case.path)
    try:
        body = r.json()
    except ValueError:
        body = {"non_json": r.text[:200]}
    return render(r.status_code, body)


E2E_DIR = GOLDEN_DIR / "e2e"
E2E_SEASON = "2026"


def build_e2e_db(path: Path) -> Path:
    """The E2E fixture league (scripts/build_e2e_db.py): synthetic, deterministic, no network."""
    import importlib.util
    import sys
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("build_e2e_db", scripts / "build_e2e_db.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.build(Path(path))
    finally:
        sys.path.remove(str(scripts))
    return Path(path)


def record(client, app, db_path: Path, out_dir: Path) -> list[Case]:
    """Write one golden file per case (and delete stale ones)."""
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cs = cases(app, con, client)
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for c in cs:
        (out_dir / f"{c.name}.json").write_text(f"// {c.path}\n" + response_text(client, c))
        keep.add(f"{c.name}.json")
    for old in out_dir.glob("*.json"):
        if old.name not in keep:
            old.unlink()
    return cs


def read_golden(out_dir: Path, name: str) -> tuple[str, str]:
    """(path recorded with the golden, golden body text)."""
    text = (out_dir / f"{name}.json").read_text()
    first, _, body = text.partition("\n")
    return first.removeprefix("// "), body
