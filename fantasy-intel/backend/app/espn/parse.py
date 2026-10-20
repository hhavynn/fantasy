"""Pure functions turning raw ESPN JSON into table rows.

No network, no database. Everything here is exercised by fixture tests.
Missing source values stay None (never coerced to 0).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .constants import (
    ACTIVITY_MAP,
    DEFAULT_POSITION_MAP,
    NON_STARTER_SLOTS,
    RECEPTION_STAT_ID,
    RESPONSE_META_KEY,
    SLOT_MAP,
    nflverse_team,
)


def ms_to_dt(value: Any) -> datetime | None:
    if value in (None, 0, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _norm_swid(swid: str | None) -> str:
    return (swid or "").strip().strip("{}").lower()


# ESPN identifies members by their SWID ("{GUID}"), which is half of the login
# cookie pair. We never store it: member ids are replaced by a one-way hash.
_SWID_RE = re.compile(r"^\{[0-9A-Za-z-]+\}$")


_MEMBER_HASH = re.compile(r"^m_[0-9a-f]{12}$")


def anon_member(member_id: Any) -> Any:
    """"m_<12 hex of sha256>". Idempotent: an id that is already a member hash (payloads scrubbed before writing,
    app/sync/raw_store.py) is returned unchanged, so scrubbed and unscrubbed payloads write the same rows."""
    if not isinstance(member_id, str) or not member_id or _MEMBER_HASH.match(member_id):
        return member_id
    return "m_" + hashlib.sha256(_norm_swid(member_id).encode()).hexdigest()[:12]


def scrub_member_ids(obj: Any) -> Any:
    """Recursively replace SWID-shaped strings with their hash (for raw JSON we keep)."""
    if isinstance(obj, dict):
        return {k: scrub_member_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_member_ids(v) for v in obj]
    if isinstance(obj, str) and _SWID_RE.match(obj):
        return anon_member(obj)
    return obj


# ------------------------------------------------------------------ league --

def parse_league(raw: dict, *, swid: str | None, my_team_id: int | None, synced_at: datetime) -> dict:
    """Returns {"league": row, "members": [...], "teams": [...], "matchups": [...]}"""
    league_id = raw["id"]
    season = raw["seasonId"]
    settings = raw.get("settings", {})
    status = raw.get("status", {})
    roster_settings = settings.get("rosterSettings", {})
    scoring = settings.get("scoringSettings", {})
    acq = settings.get("acquisitionSettings", {})
    sched = settings.get("scheduleSettings", {})
    trade = settings.get("tradeSettings", {})

    slot_counts = {
        SLOT_MAP.get(int(slot_id), str(slot_id)): count
        for slot_id, count in roster_settings.get("lineupSlotCounts", {}).items()
        if count
    }
    reception_points = None
    for item in scoring.get("scoringItems", []):
        if item.get("statId") == RECEPTION_STAT_ID:
            reception_points = item.get("points")

    final_period = status.get("finalScoringPeriod")
    current = raw.get("scoringPeriodId")
    if current is not None and final_period:
        current = min(current, final_period)

    league_row = {
        "league_id": league_id,
        "season": season,
        "name": settings.get("name"),
        "size": settings.get("size"),
        "scoring_type": scoring.get("scoringType"),
        "reception_points": reception_points,
        "is_faab": acq.get("isUsingAcquisitionBudget"),
        "faab_budget": acq.get("acquisitionBudget"),
        "current_week": current,
        "current_matchup_period": status.get("currentMatchupPeriod"),
        "final_scoring_period": final_period,
        "reg_season_matchups": sched.get("matchupPeriodCount"),
        "playoff_teams": sched.get("playoffTeamCount"),
        "trade_deadline": ms_to_dt(trade.get("deadlineDate")),
        "waiver_process_days": acq.get("waiverProcessDays"),
        "roster_slots": slot_counts,
        "scoring_items": scoring.get("scoringItems", []),
        "settings_raw": scrub_member_ids(settings),
        "synced_at": synced_at,
    }

    members = raw.get("members", [])
    member_rows = [{
        "league_id": league_id,
        "season": season,
        "member_id": anon_member(m.get("id")),
        "display_name": m.get("displayName"),
        "first_name": m.get("firstName"),
        "last_name": m.get("lastName"),
    } for m in members]
    member_names = {m.get("id"): m.get("displayName") or " ".join(
        x for x in (m.get("firstName"), m.get("lastName")) if x) for m in members}

    my_swid = _norm_swid(swid)
    team_rows = []
    for t in raw.get("teams", []):
        name = t.get("name") or " ".join(x for x in (t.get("location"), t.get("nickname")) if x) or f"Team {t['id']}"
        owners = t.get("owners", []) or []
        rec = t.get("record", {}).get("overall", {})
        counter = t.get("transactionCounter", {}) or {}
        if my_team_id is not None:
            is_mine = t["id"] == my_team_id
        else:
            is_mine = bool(my_swid) and any(_norm_swid(o) == my_swid for o in owners)
        team_rows.append({
            "league_id": league_id,
            "season": season,
            "team_id": t["id"],
            "abbrev": t.get("abbrev"),
            "name": name.strip(),
            "owner_ids": [anon_member(o) for o in owners],
            "owner_names": ", ".join(filter(None, (member_names.get(o) for o in owners))) or None,
            "division_id": t.get("divisionId"),
            "wins": rec.get("wins"),
            "losses": rec.get("losses"),
            "ties": rec.get("ties"),
            "points_for": rec.get("pointsFor"),
            "points_against": rec.get("pointsAgainst"),
            "playoff_seed": t.get("playoffSeed"),
            "waiver_rank": t.get("waiverRank"),
            "faab_spent": counter.get("acquisitionBudgetSpent"),
            "acquisitions": counter.get("acquisitions"),
            "drops": counter.get("drops"),
            "trades": counter.get("trades"),
            "move_to_ir": counter.get("moveToIR"),
            "matchup_acquisitions": counter.get("matchupAcquisitionTotals"),
            "is_mine": is_mine,
            "synced_at": synced_at,
        })

    matchup_rows = []
    for m in raw.get("schedule", []):
        home, away = m.get("home") or {}, m.get("away") or {}
        matchup_rows.append({
            "league_id": league_id,
            "season": season,
            "matchup_id": m.get("id"),
            "matchup_period": m.get("matchupPeriodId"),
            "home_team_id": home.get("teamId"),
            "away_team_id": away.get("teamId"),
            "home_points": home.get("totalPoints"),
            "away_points": away.get("totalPoints"),
            "winner": m.get("winner"),
            "playoff_tier": m.get("playoffTierType"),
        })

    return {"league": league_row, "members": member_rows, "teams": team_rows, "matchups": matchup_rows}


# ----------------------------------------------------------------- players --

def _period_points(stats: Iterable[dict], season: int, period: int, source_id: int) -> float | None:
    for s in stats or []:
        if (s.get("seasonId") == season and s.get("scoringPeriodId") == period
                and s.get("statSourceId") == source_id and s.get("statSplitTypeId") == 1):
            return s.get("appliedTotal")
    return None


def _season_totals(stats: Iterable[dict], season: int) -> tuple[float | None, float | None]:
    for s in stats or []:
        if (s.get("seasonId") == season and s.get("scoringPeriodId") == 0
                and s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0):
            return s.get("appliedTotal"), s.get("appliedAverage")
    return None, None


def player_row(player: dict, season: int, synced_at: datetime, *, pool_entry: dict | None = None) -> dict:
    """Normalise an ESPN `player` object (+ optional poolEntry wrapper)."""
    own = player.get("ownership") or {}
    season_pts, season_avg = _season_totals(player.get("stats"), season)
    row = {
        "espn_player_id": player.get("id"),
        "full_name": player.get("fullName"),
        "position": DEFAULT_POSITION_MAP.get(player.get("defaultPositionId")),
        "default_position_id": player.get("defaultPositionId"),
        "pro_team_id": player.get("proTeamId"),
        "pro_team": nflverse_team(player.get("proTeamId")),
        "eligible_slots": player.get("eligibleSlots"),
        "injury_status": player.get("injuryStatus"),
        "injured": player.get("injured"),
        "percent_owned": own.get("percentOwned"),
        "percent_started": own.get("percentStarted"),
        "percent_change": own.get("percentChange"),
        "season_points": season_pts,
        "season_avg_points": season_avg,
        "synced_at": synced_at,
    }
    if pool_entry is not None:
        row["availability_status"] = pool_entry.get("status")
        row["on_team_id"] = pool_entry.get("onTeamId")
    return row


def parse_rosters(raw: dict, *, league_id: int, season: int, scoring_period: int, synced_at: datetime) -> dict:
    """Roster entries for one scoring period -> lineup, player and points rows."""
    lineup_rows, player_rows, points_rows = [], [], []
    for team in raw.get("teams", []):
        team_id = team["id"]
        for e in (team.get("roster") or {}).get("entries", []):
            ppe = e.get("playerPoolEntry") or {}
            player = ppe.get("player") or {}
            pid = e.get("playerId") or player.get("id")
            slot_id = e.get("lineupSlotId")
            stats = player.get("stats") or []
            actual = _period_points(stats, season, scoring_period, 0)
            projected = _period_points(stats, season, scoring_period, 1)
            lineup_rows.append({
                "league_id": league_id,
                "season": season,
                "scoring_period": scoring_period,
                "team_id": team_id,
                "espn_player_id": pid,
                "lineup_slot_id": slot_id,
                "lineup_slot": SLOT_MAP.get(slot_id),
                "is_starter": slot_id is not None and slot_id not in NON_STARTER_SLOTS,
                "acquisition_type": e.get("acquisitionType"),
                "acquisition_date": ms_to_dt(e.get("acquisitionDate")),
                "injury_status": e.get("injuryStatus") or player.get("injuryStatus"),
                "points": actual,
                "projected_points": projected,
                "synced_at": synced_at,
            })
            if player:
                row = player_row(player, season, synced_at, pool_entry={"status": "ONTEAM", "onTeamId": team_id})
                row["espn_player_id"] = pid
                player_rows.append(row)
            if actual is not None or projected is not None:
                points_rows.append({
                    "season": season, "espn_player_id": pid, "scoring_period": scoring_period,
                    "actual_points": actual, "projected_points": projected, "synced_at": synced_at,
                })
    return {"lineups": lineup_rows, "players": player_rows, "points": points_rows}


def parse_player_pool(raw: dict, *, season: int, synced_at: datetime) -> list[dict]:
    rows = []
    for wrap in raw.get("players", []):
        player = wrap.get("player") or {}
        if not player.get("id"):
            continue
        rows.append(player_row(player, season, synced_at, pool_entry=wrap))
    return rows


def parse_pro_players(raw: Any, *, season: int, synced_at: datetime) -> list[dict]:
    rows = []
    for p in raw or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        rows.append(player_row(p, season, synced_at))
    return rows


def parse_player_cards(raw: dict, *, season: int, synced_at: datetime) -> list[dict]:
    """Per-week league-scoring points (statSplitTypeId 1) from any `players[].player.stats`
    payload: kona_playercard or kona_player_info. statSourceId 0 = actual, 1 = ESPN projection.
    Season splits (0) and statSplitTypeId 2 are not single-week values and are ignored here."""
    out: dict[tuple, dict] = {}
    for wrap in raw.get("players", []):
        player = wrap.get("player") or {}
        pid = player.get("id") or wrap.get("id")
        for s in player.get("stats") or []:
            if s.get("seasonId") != season or s.get("statSplitTypeId") != 1:
                continue
            period = s.get("scoringPeriodId")
            if not period:
                continue
            key = (pid, period)
            row = out.setdefault(key, {
                "season": season, "espn_player_id": pid, "scoring_period": period,
                "actual_points": None, "projected_points": None, "synced_at": synced_at,
            })
            if s.get("statSourceId") == 0:
                row["actual_points"] = s.get("appliedTotal")
            elif s.get("statSourceId") == 1:
                row["projected_points"] = s.get("appliedTotal")
    return list(out.values())


def parse_projection_week(raw: dict, *, season: int, scoring_period: int, synced_at: datetime,
                          rostered_ids: Iterable[int], expected_rostered_ids: Iterable[int], limit: int) -> dict:
    """ESPN's projection for exactly one scoring period from a kona_player_info response.

    Returns {"rows", "returned_ids", "complete", "coverage"}.

    rows: one per returned player whose payload has a projection entry (statSourceId 1 / split 1)
      for this period. 0 is a measured projection; a null appliedTotal stays None. Entries for
      other periods are ignored, so a week's request can only ever write that week.
    returned_ids: every player ESPN explicitly returned. Their week state is reconciled from this
      response (returned without a projection entry -> no projection).
    complete: the response provably contains ESPN's whole ONTEAM + FREEAGENT + WAIVERS universe,
      so players it omits may be cleared too. Requires all of:
        * ESPN's X-Fantasy-Filter-Player-Count (attached by the client) is present,
        * the number of distinct players returned equals that count (so not truncated),
        * fewer players than `limit` (a full page could hide more),
        * every independently expected rostered player (`expected_rostered_ids`: rostered players
          with a current NFL team) was returned.
      Otherwise only returned players are reconciled; omitted players keep their previous values.
    No free-agent count is ever invented: the FA universe is only "known" through ESPN's total."""
    returned: dict[int, str | None] = {}
    rows: dict[int, dict] = {}
    for wrap in raw.get("players", []) or []:
        player = wrap.get("player") or {}
        pid = player.get("id") or wrap.get("id")
        if pid is None:
            continue
        returned[pid] = wrap.get("status")
        for s in player.get("stats") or []:
            if (s.get("seasonId") == season and s.get("scoringPeriodId") == scoring_period
                    and s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1):
                rows[pid] = {"season": season, "espn_player_id": pid, "scoring_period": scoring_period,
                             "projected_points": s.get("appliedTotal"), "synced_at": synced_at}
    rostered = set(rostered_ids)
    expected_rostered = set(expected_rostered_ids) & rostered
    valued = {pid: r["projected_points"] for pid, r in rows.items() if r["projected_points"] is not None}
    available = {pid for pid, status in returned.items() if pid not in rostered and status != "ONTEAM"}
    total = ((raw.get(RESPONSE_META_KEY) or {}).get("filter_player_count"))

    def returned_block(ids: set[int]) -> dict:
        ids = ids & returned.keys()
        return {"returned": len(ids), "with_projection": len(ids & valued.keys()),
                "without_projection": len(ids - valued.keys()), "zero": sum(1 for pid in ids if valued.get(pid) == 0)}

    truncated = len(returned) >= limit
    omitted_rostered = sorted(expected_rostered - returned.keys())
    if total is None:
        reason = "ESPN player count header missing: universe not verifiable"
    elif len(returned) != total:
        reason = f"returned {len(returned)} of ESPN's {total} matching players"
    elif truncated:
        reason = f"response filled the {limit}-player limit"
    elif omitted_rostered:
        reason = f"{len(omitted_rostered)} expected rostered player(s) not returned"
    else:
        reason = None
    rostered_block = returned_block(rostered)
    rostered_block.update({
        "rostered": len(rostered),
        "expected": len(expected_rostered),            # rostered players with a current NFL team
        "expected_returned": len(expected_rostered & returned.keys()),
        "expected_with_projection": len(expected_rostered & valued.keys()),
        "omitted": len(omitted_rostered),
    })
    coverage = {
        "week": scoring_period,
        "espn_total": total,                           # authoritative universe size; None = unknown
        "returned": len(returned),
        "returned_with_projection": len(valued),
        "returned_without_projection": len(returned) - len(valued),
        "zero": sum(1 for v in valued.values() if v == 0),
        "truncated": truncated, "complete": reason is None, "reason": reason,
        "rostered": rostered_block,
        "available": returned_block(available),         # returned FA/waiver players; no expected count exists
    }
    return {"rows": list(rows.values()), "returned_ids": sorted(returned), "complete": reason is None,
            "coverage": coverage}


def merge_player_rows(*sources: Iterable[dict]) -> list[dict]:
    """Merge player rows by id; later sources override earlier non-null values."""
    merged: dict[int, dict] = {}
    for source in sources:
        for row in source:
            pid = row.get("espn_player_id")
            if pid is None:
                continue
            current = merged.setdefault(pid, {})
            for k, v in row.items():
                if v is not None or k not in current:
                    current[k] = v
    return list(merged.values())


# ------------------------------------------------------------ transactions --

def parse_transactions(raw: dict, *, league_id: int, season: int, synced_at: datetime) -> dict:
    tx_rows, item_rows = [], []
    for t in raw.get("transactions", []) or []:
        tid = str(t.get("id"))
        tx_rows.append({
            "league_id": league_id,
            "season": season,
            "transaction_id": tid,
            "type": t.get("type"),
            "status": t.get("status"),
            "team_id": t.get("teamId"),
            "scoring_period": t.get("scoringPeriodId"),
            "proposed_date": ms_to_dt(t.get("proposedDate")),
            "process_date": ms_to_dt(t.get("processDate")),
            "bid_amount": t.get("bidAmount"),
            "execution_type": t.get("executionType"),
            "related_transaction_id": (str(t["relatedTransactionId"]) if t.get("relatedTransactionId") else None),
            "member_id": anon_member(t.get("memberId")),
            "is_pending": t.get("isPending"),
            "raw": scrub_member_ids(t),
            "synced_at": synced_at,
        })
        for i, item in enumerate(t.get("items") or []):
            item_rows.append({
                "transaction_id": tid,
                "item_index": i,
                "item_type": item.get("type"),
                "espn_player_id": item.get("playerId"),
                "from_team_id": item.get("fromTeamId"),
                "to_team_id": item.get("toTeamId"),
                "from_lineup_slot_id": item.get("fromLineupSlotId"),
                "to_lineup_slot_id": item.get("toLineupSlotId"),
            })
    return {"transactions": tx_rows, "items": item_rows}


def _valid_team(value: Any, team_ids: set[int]) -> int | None:
    return value if isinstance(value, int) and value in team_ids else None


def parse_activity(raw: dict, *, league_id: int, season: int, team_ids: set[int]) -> list[dict]:
    rows = []
    for topic in raw.get("topics", []) or []:
        topic_id = str(topic.get("id"))
        topic_date = ms_to_dt(topic.get("date"))
        for idx, msg in enumerate(topic.get("messages", []) or []):
            mtype = msg.get("messageTypeId")
            if mtype not in ACTIVITY_MAP:
                continue
            frm, to, for_ = msg.get("from"), msg.get("to"), msg.get("for")
            bid = None
            from_team = to_team = None
            if mtype == 244:  # trade leg: player moves from -> to
                from_team, to_team = _valid_team(frm, team_ids), _valid_team(to, team_ids)
                team = from_team
            elif mtype == 239:
                team = _valid_team(for_, team_ids)
            elif mtype in (178, 180):  # adds
                team = _valid_team(to, team_ids)
                if mtype == 180:
                    bid = frm if isinstance(frm, (int, float)) else None
            else:  # 179 / 181 drops
                team = _valid_team(to, team_ids) or _valid_team(frm, team_ids)
            rows.append({
                "league_id": league_id,
                "season": season,
                "message_id": str(msg.get("id") or f"{topic_id}:{idx}"),
                "topic_id": topic_id,
                "message_type_id": mtype,
                "action": ACTIVITY_MAP[mtype],
                "topic_date": topic_date,
                "team_id": team,
                "from_team_id": from_team,
                "to_team_id": to_team,
                "espn_player_id": msg.get("targetId"),
                "bid_amount": bid,
                "raw": scrub_member_ids(msg),
            })
    return rows


def parse_draft(raw: dict, *, league_id: int, season: int) -> list[dict]:
    detail = raw.get("draftDetail") or {}
    if not detail.get("drafted"):
        return []
    rows = []
    for p in detail.get("picks", []):
        rows.append({
            "league_id": league_id,
            "season": season,
            "overall_pick": p.get("overallPickNumber") or p.get("id"),
            "round": p.get("roundId"),
            "round_pick": p.get("roundPickNumber"),
            "team_id": p.get("teamId"),
            "espn_player_id": p.get("playerId"),
            "bid_amount": p.get("bidAmount"),
            "keeper": p.get("keeper"),
        })
    return rows
