"""Start-decision shortlist (issue #175): two to four players for one lineup slot, side by side.

Built on the Player Comparison Lab (app.analytics.player_compare): every player is the lab's own card (same leakage
cutoff, same projections in this league's scoring, same rule that a missing number is None and never 0, same
position-applicability of usage metrics). Slot eligibility is the lineup engine's (app.analytics.lineup.eligible over
the league's synced starting slots), so a player can fill a slot here exactly when Start/Sit could start him there.

Nothing is estimated. "Best projected" is the highest stored projection for the current week among the players who
can fill the chosen slot, from the source the page shows (ESPN's, or the Fantasy Intel model when it is selected):
- a player on bye, not eligible for the slot, or without a projection is left out, each with the reason;
- when an eligible player has no projection the result is `incomplete`: the best of those that have one, said so;
- players within NEXT_WEEK_EDGE points of the top are all leaders (`close`), never a fake winner.
Which player to start is the viewer's call; the API never names one.
"""

from __future__ import annotations

import json
import re

from ..db.connection import utcnow
from . import forward_ours
from . import player_compare as pc
from .legacy_league import latest_league
from .league_rules import provider_of
from .lineup import FLEX_ELIGIBILITY, eligible, lineup_slots
from .projections import league_window, load_schedule
from .start_sit import NO_PROJECTIONS

MIN_PLAYERS, MAX_PLAYERS = 2, 4
CLOSE_GAP = pc.NEXT_WEEK_EDGE          # projected points: a smaller gap to the top is a tie, as in the two-player lab
DISCLAIMER = ("A shortlist of stored projections, usage and injury reports for one lineup slot. \"Best projected\" "
              "is the highest stored projection for the week, nothing more; it is not a start/sit verdict, a "
              "probability or a consensus.")
SLEEPER_WAIVERS = ("Sleeper does not publish waiver status, so an unrostered player is listed as a free agent even "
                   "when he may still be on waivers.")


# An ESPN / Sleeper player id: ASCII digits only (int() would also take "2_01" or other scripts' digits, each its own
# cache entry), at most 10 of them (a 40-digit id overflows the database's BIGINT), negative for team defenses.
_ID = re.compile(r"-?[0-9]{1,10}")
NO_COMMON_SLOT = ("These players never share a lineup slot in this league, so ranking them against each other says "
                  "nothing about a start decision. Pick a slot to compare the players who can fill it.")


class ShortlistError(ValueError):
    """A request the API answers with 422: bad ids, too few or too many players, or a slot the league does not have."""


def parse_ids(raw: str | None) -> list[int]:
    """`ids` as given ("12,-16001,7"): whole numbers (team defenses are negative), each once, MIN..MAX of them."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    ids: list[int] = []
    for p in parts:
        if not (p.isascii() and _ID.fullmatch(p)):
            raise ShortlistError(f"'{p[:24]}' is not a player id. Ids are whole numbers of up to 10 digits "
                                 "(team defenses are negative).")
        pid = int(p)
        if pid in ids:
            raise ShortlistError(f"Player {pid} is on the shortlist twice. Each player can be listed once.")
        ids.append(pid)
    if not MIN_PLAYERS <= len(ids) <= MAX_PLAYERS:
        raise ShortlistError(f"A shortlist compares {MIN_PLAYERS} to {MAX_PLAYERS} players; got {len(ids)}.")
    return ids


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def league_slots(roster_slots) -> tuple[list[dict], list[str]]:
    """(the league's starting slots the lineup engine fills, each once in lineup order with its count and eligible
    positions; the starting slots no engine fills). Both empty when the league's slots are not synced."""
    slots = lineup_slots(roster_slots if isinstance(roster_slots, dict) else {})
    out: list[dict] = []
    for s in slots:
        if out and out[-1]["slot"] == s:
            out[-1]["count"] += 1
        else:
            out.append({"slot": s, "count": 1, "positions": list(FLEX_ELIGIBILITY.get(s, (s,)))})
    return out, list(slots.unsupported)


def roster_status(availability: str | None, on_team_id: int | None, team_id: int | None) -> str:
    """this_team | other_team | free_agent | waivers | unknown, relative to the team the view is for."""
    if availability == "ONTEAM":
        if team_id is not None and on_team_id is not None and on_team_id == team_id:
            return "this_team"
        return "other_team" if on_team_id is not None else "unknown"
    return {"FREEAGENT": "free_agent", "WAIVERS": "waivers"}.get(availability or "", "unknown")


def _source_name(field: str) -> str:
    return "Fantasy Intel" if field == "ours" else "ESPN"


def _a(src: str) -> str:
    """The source with its article: "an ESPN", "a Fantasy Intel"."""
    return f"{'an' if src[:1] in 'AEIOU' else 'a'} {src}"


def share_a_slot(positions: list[str | None], labels: list[str]) -> bool:
    """Whether one starting slot of the league takes every position listed. Without synced slots only players of
    the same position are known to compete for one."""
    if not labels:
        return len(set(positions)) == 1 and positions[0] is not None
    return any(all(eligible(s, p) for p in positions) for s in labels)


def source_label(entries: list[dict], field: str) -> str:
    """The source the block names: ESPN or Fantasy Intel only when at least one player on the list has a number from it
    for the week (D-14: a source with no values is never named), else NO_PROJECTIONS ("Unavailable")."""
    has = any(e["next_week"] and not e["next_week"]["on_bye"] and e["next_week"][field] is not None for e in entries)
    return _source_name(field) if has else NO_PROJECTIONS


def best_projected(entries: list[dict], field: str, week: int, slot: str | None, *, common_slot: bool = True) -> dict:
    """The best-projected block from each player's {espn_player_id, name, position, slot_eligible, next_week}.
    `common_slot` False (no slot chosen and no league slot takes every player, e.g. QB and K): no player is named."""
    src = _source_name(field)
    label = source_label(entries, field)
    considered: list[tuple[float, dict]] = []
    excluded: list[dict] = []
    for e in entries:
        pid, name, nw = e["espn_player_id"], e["name"], e["next_week"]
        if e["slot_eligible"] is False:
            excluded.append({"espn_player_id": pid, "reason": "not_eligible",
                             "text": f"{name} ({e['position'] or 'unknown position'}) cannot start at {slot}."})
        elif nw is None:
            excluded.append({"espn_player_id": pid, "reason": "no_projection",
                             "text": f"{name} has no projection (the league's projection window is unknown)."})
        elif nw["on_bye"]:
            excluded.append({"espn_player_id": pid, "reason": "bye", "text": f"{name} is on bye in week {week}."})
        elif nw[field] is None:
            excluded.append({"espn_player_id": pid, "reason": "no_projection",
                             "text": f"{name} has no {src} projection for week {week}."})
        else:
            considered.append((float(nw[field]), e))
    missing = [x for x in excluded if x["reason"] == "no_projection"]
    out = {"source": field, "source_label": label, "week": week, "slot": slot, "leaders": [], "value": None, "gap": None,
           "considered": [e["espn_player_id"] for _, e in considered], "excluded": excluded,
           "complete": not missing, "status": "none", "text": ""}
    if not common_slot:
        out.update(considered=[], text=NO_COMMON_SLOT)
        return out
    if not considered:
        if slot is not None and all(e["slot_eligible"] is False for e in entries):
            out["text"] = f"No player here can fill {slot}, so none is best projected."
        else:
            out["text"] = (f"No player who can fill {'the slot' if slot is None else slot} has a projection for "
                           f"week {week}, so none is best projected.")
        return out
    considered.sort(key=lambda t: (-t[0], t[1]["espn_player_id"]))
    top = considered[0][0]
    leaders = [e for v, e in considered if round(top - v, 9) < CLOSE_GAP - 1e-9]
    rest = [v for v, _ in considered[len(leaders):]]
    out.update(leaders=[e["espn_player_id"] for e in leaders], value=round(top, 2),
               gap=None if not rest else round(top - rest[0], 2))
    names = " and ".join(e["name"] for e in leaders)
    if len(considered) == 1:
        out["status"] = "only" if not missing else "incomplete"
        out["text"] = f"{names} is the only player here with {_a(src)} projection for week {week} ({top:.1f})."
    elif len(leaders) > 1:
        out["status"] = "close"
        out["text"] = (f"{names} are within {CLOSE_GAP} points of each other at the top ({src}, week {week}); "
                       "the projections do not separate them.")
    else:
        out["status"] = "leader"
        out["text"] = (f"{names} has the highest {src} projection for week {week}: {top:.1f}, "
                       f"{out['gap']:.1f} ahead of the next player.")
    if missing:
        out["status"] = "incomplete"
        out["text"] += (" Not every player who can fill the slot has a projection, so this is the best of those that "
                        "do, not of the whole shortlist.")
    return out


def shortlist(con, ids: list[int], *, slot: str | None = None, team: dict | None = None, source: str | None = None,
              now=None) -> dict:
    """The shortlist payload. `team` is the perspective block of the team the view is for (api.perspective), or None.
    Raises ShortlistError (422), player_compare.UnknownPlayer (404) and player_compare.LeagueUnavailable (503)."""
    source = forward_ours.normalize(source, con)
    lg = pc.load_league(con)
    season, as_of = lg["season"], int(lg["current_week"])
    done = as_of - 1
    extra = latest_league(con, ["roster_slots", "settings_raw"], season=season) or {}
    slots, unsupported = league_slots(_json(extra.get("roster_slots")))
    labels = [s["slot"] for s in slots]
    if slot is not None and slot not in labels:
        raise ShortlistError(f"{slot} is not a starting slot in this league" + (
            f"; choose one of {', '.join(labels)}." if labels else " (its lineup slots are not synced)."))
    settings = _json(extra.get("settings_raw"))
    provider = provider_of(settings) if isinstance(settings, dict) and settings else "unknown"

    rows = {}
    for i, pid in enumerate(ids):
        row = pc._player_row(con, season, pid)
        if row is None:
            raise pc.UnknownPlayer(f"player {i + 1}", pid)
        rows[pid] = row
    sched_games, covered = load_schedule(con, season, list(range(1, done + 1))) if done >= 1 else ({}, set())
    provider_projections = forward_ours.ps.default_source(con, season) == "espn"
    ctx = {"season": season, "league_id": lg["league_id"], "as_of": as_of, "completed": done, "source": source,
           "sched_games": sched_games, "covered": covered, "now": now or utcnow(),
           "provider_projections": provider_projections}
    team_id = team["team_id"] if team else None
    field = "ours" if source == "independent" else "espn"

    players, entries, notes = [], [], []
    ours_reasons: dict[str, list[str]] = {}
    for i, pid in enumerate(ids):
        side = pc._load_side(con, ctx, pid, f"player {i + 1}")
        card, row = side.card, rows[pid]
        proj = card["projection"]
        slot_ok = None if slot is None else eligible(slot, side.position)
        players.append({
            "card": card,
            "roster": roster_status(row["availability_status"], row["on_team_id"], team_id),
            "eligible_slots": [s for s in labels if eligible(s, side.position)],
            "slot_eligible": slot_ok,
        })
        entries.append({"espn_player_id": pid, "name": side.name, "position": side.position, "slot_eligible": slot_ok,
                        "next_week": proj["next_week"] if proj else None})
        notes += side.notes
        if source == "independent" and proj and proj["ros"]["ours_reason"]:
            ours_reasons.setdefault(proj["ros"]["ours_reason"], []).append(side.name)

    limitations: list[str] = []
    scoring = pc.scoring_block(lg)
    if not scoring["settings_synced"]:
        limitations.append("League scoring settings are not synced; provider numbers are still in this league's "
                           "scoring.")
    if not labels:
        limitations.append("This league's lineup slots are not synced, so slot eligibility cannot be shown.")
    elif unsupported:
        limitations.append(f"The league also starts {', '.join(unsupported)}, which no engine here fills; those "
                           "slots are not offered.")
    if provider == "sleeper" and any(p["roster"] == "free_agent" for p in players):
        limitations.append(SLEEPER_WAIVERS)
    for reason, names in ours_reasons.items():
        who = "" if len(names) == len(ids) else f" for {', '.join(names)}"
        notes.append(f"Fantasy Intel projections{who}: {reason}")
    for n in notes:
        if n not in limitations:
            limitations.append(n)

    window = league_window(con, season)
    return {
        "as_of_week": as_of, "completed_through_week": done, "final_week": window.final if window else None,
        "source": source, "provider": provider, "scoring": scoring,
        "slots": slots, "unsupported_slots": unsupported, "slot": slot,
        "team": None if team is None else {"team_id": team["team_id"], "name": team["name"],
                                           "is_owner_default": bool(team.get("is_owner_default"))},
        "players": players,
        "best_projected": best_projected(
            entries, field, as_of, slot,
            common_slot=slot is not None or share_a_slot([e["position"] for e in entries], labels)),
        "limitations": limitations, "disclaimer": DISCLAIMER, "freshness": pc._freshness(con),
    }
