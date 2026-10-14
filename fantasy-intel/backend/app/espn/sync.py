"""ESPN league sync: fetch everything over the network, then write in one transaction."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..config import DATA_DIR
from ..db.connection import SyncRun, replace_where, upsert_rows, utcnow
from . import parse

log = logging.getLogger("espn.sync")

CARD_BATCH = 100


@dataclass
class EspnPayloads:
    league: dict
    pro_players: Any = None
    rosters: dict[int, dict] = field(default_factory=dict)       # period -> raw
    transactions: dict[int, dict] = field(default_factory=dict)  # period -> raw
    activity: list[dict] = field(default_factory=list)           # raw pages
    pool: dict | None = None
    draft: dict | None = None
    cards: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _try(warnings: list[str], label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as err:  # keep going; record what failed
        msg = f"{label} failed: {type(err).__name__}: {err}"
        log.warning(msg)
        warnings.append(msg)
        return None


def fetch_all(client, *, max_activity_pages: int = 40) -> EspnPayloads:
    """Network phase. `client` is an EspnClient (or a fake with the same methods)."""
    league = client.league()  # fatal on failure (bad credentials / league id)
    p = EspnPayloads(league=league)
    current = min(league.get("scoringPeriodId") or 1, league.get("status", {}).get("finalScoringPeriod") or 99)

    p.pro_players = _try(p.warnings, "pro players", client.pro_players)
    for period in range(1, current + 1):
        log.info("period %s: rosters + transactions", period)
        roster = _try(p.warnings, f"roster week {period}", lambda: client.roster_for_period(period))
        if roster is not None:
            p.rosters[period] = roster
        tx = _try(p.warnings, f"transactions week {period}", lambda: client.transactions(period))
        if tx is not None:
            p.transactions[period] = tx

    for page in range(max_activity_pages):
        data = _try(p.warnings, f"activity page {page}", lambda: client.activity(offset=page * 50, size=50))
        if not data or not data.get("topics"):
            break
        p.activity.append(data)
        if len(data["topics"]) < 50:
            break

    p.pool = _try(p.warnings, "free-agent pool", lambda: client.player_pool(current))
    p.draft = _try(p.warnings, "draft", client.draft)

    ids = set()
    for raw in [league, *p.rosters.values()]:
        for t in raw.get("teams", []):
            for e in (t.get("roster") or {}).get("entries", []):
                if e.get("playerId"):
                    ids.add(e["playerId"])
    for w in (p.pool or {}).get("players", []):
        if w.get("id"):
            ids.add(w["id"])
    ids_sorted = sorted(ids)
    final_period = league.get("status", {}).get("finalScoringPeriod") or 18
    for i in range(0, len(ids_sorted), CARD_BATCH):
        batch = ids_sorted[i:i + CARD_BATCH]
        card = _try(p.warnings, f"player cards {i}", lambda: client.player_cards(batch, final_period))
        if card:
            p.cards.append(card)
    return p


def save_raw(p: EspnPayloads, run_id: str) -> Path:
    out = DATA_DIR / "raw" / "espn" / run_id
    out.mkdir(parents=True, exist_ok=True)

    def dump(name: str, obj: Any) -> None:
        if obj is not None:  # member SWIDs are hashed before anything touches disk
            (out / f"{name}.json").write_text(json.dumps(parse.scrub_member_ids(obj)))

    dump("league", p.league)
    dump("pro_players", p.pro_players)
    dump("pool", p.pool)
    dump("draft", p.draft)
    for k, v in p.rosters.items():
        dump(f"roster_{k:02d}", v)
    for k, v in p.transactions.items():
        dump(f"transactions_{k:02d}", v)
    for i, v in enumerate(p.activity):
        dump(f"activity_{i:02d}", v)
    for i, v in enumerate(p.cards):
        dump(f"cards_{i:02d}", v)
    return out


def write_all(con, p: EspnPayloads, *, swid: str | None, my_team_id: int | None) -> dict:
    """Database phase. Idempotent: rerunning with the same payloads yields the same rows."""
    now = utcnow()
    lg = parse.parse_league(p.league, swid=swid, my_team_id=my_team_id, synced_at=now)
    league_id, season = lg["league"]["league_id"], lg["league"]["season"]
    current = lg["league"]["current_week"]
    team_ids = {t["team_id"] for t in lg["teams"]}
    stats: dict[str, Any] = {"warnings": list(p.warnings)}
    if not any(t["is_mine"] for t in lg["teams"]):
        stats["warnings"].append("Could not identify your team (set ESPN_MY_TEAM_ID in .env).")

    con.execute("BEGIN TRANSACTION")
    try:
        upsert_rows(con, "leagues", [lg["league"]], ["league_id", "season"])
        upsert_rows(con, "league_members", lg["members"], ["league_id", "season", "member_id"])
        stats["teams"] = upsert_rows(con, "fantasy_teams", lg["teams"], ["league_id", "season", "team_id"])
        stats["matchups"] = upsert_rows(con, "matchups", lg["matchups"], ["league_id", "season", "matchup_id"])

        # Rosters per period; the base league payload doubles as the current period.
        rosters = dict(p.rosters)
        rosters.setdefault(current, p.league)
        lineup_count, points_rows, roster_players = 0, [], []
        for period, raw in sorted(rosters.items()):
            r = parse.parse_rosters(raw, league_id=league_id, season=season, scoring_period=period, synced_at=now)
            lineup_count += replace_where(
                con, "lineup_snapshots", r["lineups"],
                "league_id = ? AND season = ? AND scoring_period = ?", [league_id, season, period])
            points_rows += r["points"]
            if period == current:
                roster_players = r["players"]
        stats["lineup_rows"] = lineup_count
        stats["roster_periods"] = sorted(rosters)

        # Player universe: pro players (base) <- FA pool <- current rosters.
        base = parse.parse_pro_players(p.pro_players, season=season, synced_at=now) if p.pro_players else []
        pool = parse.parse_player_pool(p.pool, season=season, synced_at=now) if p.pool else []
        for row in pool + roster_players:
            row["detail_synced_at"] = now
        players = parse.merge_player_rows(base, pool, roster_players)
        if base:
            # Complete universe fetched: replace so stale availability can't linger.
            con.execute("DELETE FROM fantasy_players")
        stats["players"] = upsert_rows(con, "fantasy_players", players, ["espn_player_id"])
        stats["free_agent_pool"] = len(pool)
        today = date.today()
        upsert_rows(con, "player_ownership_history", [{
            "snapshot_date": today,
            "espn_player_id": r["espn_player_id"],
            "percent_owned": r.get("percent_owned"),
            "percent_started": r.get("percent_started"),
            "percent_change": r.get("percent_change"),
        } for r in pool + roster_players if r.get("percent_owned") is not None],
            ["snapshot_date", "espn_player_id"])

        for card in p.cards:
            points_rows += parse.parse_player_cards(card, season=season, synced_at=now)
        # Cards are fetched last and cover every week; prefer them over roster stats
        # but never let a None overwrite a known value.
        merged_points: dict[tuple, dict] = {}
        for r in points_rows:
            key = (r["espn_player_id"], r["scoring_period"])
            cur = merged_points.setdefault(key, dict(r))
            for k in ("actual_points", "projected_points"):
                if r.get(k) is not None:
                    cur[k] = r[k]
        stats["weekly_points"] = upsert_rows(
            con, "espn_player_weekly_points", list(merged_points.values()),
            ["season", "espn_player_id", "scoring_period"])

        tx_rows, item_rows = [], []
        for raw in p.transactions.values():
            t = parse.parse_transactions(raw, league_id=league_id, season=season, synced_at=now)
            tx_rows += t["transactions"]
            item_rows += t["items"]
        stats["transactions"] = upsert_rows(con, "transactions", tx_rows, ["transaction_id"])
        tx_ids = sorted({r["transaction_id"] for r in tx_rows})
        if tx_ids:
            con.execute("DELETE FROM transaction_items WHERE transaction_id IN (SELECT unnest(?))", [tx_ids])
            upsert_rows(con, "transaction_items", item_rows, ["transaction_id", "item_index"])
        stats["transaction_items"] = len(item_rows)
        stats["transaction_types"] = _count(tx_rows, "type")
        stats["transaction_statuses"] = _count(tx_rows, "status")

        act_rows = []
        for raw in p.activity:
            act_rows += parse.parse_activity(raw, league_id=league_id, season=season, team_ids=team_ids)
        stats["activity_messages"] = upsert_rows(con, "activity_messages", act_rows, ["message_id"])

        if p.draft:
            stats["draft_picks"] = upsert_rows(
                con, "draft_picks", parse.parse_draft(p.draft, league_id=league_id, season=season),
                ["league_id", "season", "overall_pick"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    stats["league"] = lg["league"]["name"]
    stats["current_week"] = current
    return stats


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


def run_espn_sync(con_factory, settings, client=None, save_raw_payloads: bool = True) -> dict:
    """Fetch then write. `con_factory` opens a read-write connection (context manager)."""
    from .client import EspnClient

    if client is None:
        if not settings.league_id:
            raise SystemExit("ESPN_LEAGUE_ID is not set. Copy .env.example to .env and fill it in.")
        if not settings.has_espn_credentials:
            log.warning("ESPN_S2/SWID not set; this only works for public leagues.")
        client = EspnClient(settings.league_id, settings.season, settings.espn_s2, settings.swid)

    payloads = fetch_all(client)
    with con_factory() as con:
        run = SyncRun(con, "espn")
        if save_raw_payloads:
            save_raw(payloads, run.run_id)
        try:
            stats = write_all(con, payloads, swid=settings.swid, my_team_id=settings.my_team_id)
        except Exception as err:
            run.finish("error", f"{type(err).__name__}: {err}")
            raise
        run.finish("ok", "warnings: %d" % len(stats["warnings"]), stats)
    return stats
