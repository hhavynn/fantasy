"""Ask Fantasy Intel (issue #219, contract ask/v1): one question in, one existing read-only analysis out, as a card.

Mounted twice like every legacy router: here (the classic single-league deployment), and under /v2/leagues/{league_id}
through league_scope.mount_legacy_routers, whose dependency authorizes the caller for the league (unknown and
inaccessible leagues are the same 404) before this handler runs, so before the question is read at all.

The question is parsed by app/analytics/ask.py (a deterministic, anchored grammar; no model). A parsed question runs
exactly one existing handler in-process with typed arguments only (the screener runs twice for a "last N weeks"
window: once to learn the latest week with data, once for that exact range). Every number on the card is copied from
that handler's response, never recomputed, and missing stays missing (null, never 0). The card names the intent, how
the question was read, the handler's own provenance or freshness, and a link to the page that shows the same view.
Nothing is written, logged, cached here or sent anywhere; identical league-scoped GETs are answered from the
per-league response cache like every other route."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..analytics import ask as grammar
from ..analytics.legacy_league import latest_league
from ..analytics.lineup_what_if import MAX_ALTERNATIVES
from ..analytics.screener import METRIC_BY_KEY, UNKNOWN_OWNERSHIP
from . import matchup as matchup_api
from . import (
    player_compare,
    playoff_race,
    routes,
    schedule_intelligence,
    screener,
    start_sit,
    trade_values,
)
from .deps import db, rows
from .perspective import resolve_team
from .signal_provenance import league_provider

router = APIRouter(prefix="/api", tags=["ask"])

TOP = 5                # rows on a card; the linked page has the rest
NEEDS_TEAM = frozenset({"waivers_best", "start_sit", "replacement", "start_choice", "matchup", "byes", "playoffs"})


@router.get("/ask")
def ask(
    q: str | None = Query(None, description=f"The question (at most {grammar.MAX_CHARS} characters and "
                                            f"{grammar.MAX_WORDS} words)"),
    team_id: int | None = Query(None, description="Fantasy team ID (defaults to the team in view)"),
):
    """Answer one supported question about this league from an existing analysis, ask a specific clarifying question,
    or abstain. 422 for a question longer than the limit; the league is authorized before the question is read."""
    if q is not None and (len(q) > grammar.MAX_CHARS or len(q.split()) > grammar.MAX_WORDS):
        raise HTTPException(422, f"A question is at most {grammar.MAX_CHARS} characters and {grammar.MAX_WORDS} "
                                 "words.")
    question = grammar.normalize(q or "")
    # The bound holds for the text the grammar reads too: NFKC can expand a character into many, and format
    # characters (zero-width spaces) become word breaks.
    if len(question) > grammar.MAX_CHARS or len(question.split()) > grammar.MAX_WORDS:
        raise HTTPException(422, f"A question is at most {grammar.MAX_CHARS} characters and {grammar.MAX_WORDS} "
                                 "words.")
    parsed = grammar.parse(q or "")
    if isinstance(parsed, grammar.Unsupported):
        return _unsupported(question, parsed)
    with db() as con:
        lg = latest_league(con, ["league_id", "season", "name", "current_week"])
        if lg is None:
            return _card("unavailable", question, parsed, reason="This league has not been synced yet.")
        perspective = resolve_team(con, lg, team_id)        # an unknown team_id is a 404, as on every page
        if parsed.intent in NEEDS_TEAM and perspective is None:
            return _needs_team(question, parsed)
        players = {}
        for slot in ("p", "p2"):
            ref = parsed.slots.get(slot)
            if ref is None:
                continue
            found = _resolve_player(con, lg, ref, question)
            if isinstance(found, dict) and found.get("status") == "clarify":
                return {**_card("clarify", question, parsed), "clarify": found["clarify"]}
            if found is None:
                return _card("abstain", question, parsed, code="no_player",
                             reason=f"No player named \"{ref.text}\" is in this league's player pool.")
            players[slot] = found
        if "p2" in players and players["p"]["espn_player_id"] == players["p2"]["espn_player_id"]:
            return _card("abstain", question, parsed, code="same_player", reason="Name two different players.")
        week = parsed.slots.get("week")
        if week == "next":
            week = int(lg["current_week"] or 0) + 1
        ctx = {"lg": lg, "perspective": perspective, "team_id": team_id, "players": players, "week": week,
               "provider": league_provider(con)}
    try:
        return EXECUTORS[parsed.intent](question, parsed, ctx)
    except HTTPException as err:
        return _from_http(question, parsed, err)


# ------------------------------------------------------------------------------------------------------ cards --

def _card(status: str, question: str, parsed: grammar.Parsed | None = None, *, code: str | None = None,
          reason: str | None = None, interpretation: str | None = None, answer: dict | None = None,
          notes: list[str] | None = None, provenance: dict | None = None, freshness: Any = None,
          link: dict | None = None) -> dict:
    intent = parsed.intent if parsed is not None else None
    return {
        "contract": grammar.CONTRACT,
        "status": status,                    # answered | clarify | abstain | unavailable
        "question": question,
        "intent": {"id": intent, "label": grammar.INTENTS[intent]} if intent else None,
        "interpretation": interpretation,
        "answer": answer,
        "notes": notes or [],
        "provenance": provenance,
        "freshness": freshness,
        "link": link,
        "clarify": None,
        "abstain": {"code": code, "reason": reason} if status in ("abstain", "unavailable") else None,
        "examples": list(grammar.EXAMPLES) if status in ("abstain", "clarify") else [],
    }


def _unsupported(question: str, u: grammar.Unsupported) -> dict:
    if u.clarify:
        card = _card("clarify", question, code=u.code, reason=u.reason)
        card["clarify"] = {"code": u.code, "question": u.clarify, "reason": u.reason, "options": []}
        return card
    return _card("abstain", question, code=u.code, reason=u.reason)


def _needs_team(question: str, parsed: grammar.Parsed) -> dict:
    card = _card("clarify", question, parsed)
    card["clarify"] = {"code": "needs_team", "question": "Which team? Pick a team in the team menu, then ask again.",
                       "reason": "This answer is for one team's roster, and no team is in view.", "options": []}
    return card


def _from_http(question: str, parsed: grammar.Parsed, err: HTTPException) -> dict:
    detail = str(err.detail)
    if err.status_code == 404 and ("No team is marked as yours" in detail or "team was not identified" in detail):
        return _needs_team(question, parsed)
    if err.status_code in (404, 422):
        return _card("abstain", question, parsed, code="invalid" if err.status_code == 422 else "not_found",
                     reason=detail)
    if err.status_code == 503:
        return _card("unavailable", question, parsed, code="unavailable", reason=detail)
    raise err


def _link(path: str, query: dict, ctx: dict) -> dict:
    q = {k: v for k, v in query.items() if v not in (None, "", [], ())}
    if ctx["team_id"] is not None:
        q["team"] = ctx["team_id"]
    return {"path": path, "query": q}


def _fact(label: str, value: Any, unit: str | None = None) -> dict:
    return {"label": label, "value": value, "unit": unit}


def _team_name(ctx: dict) -> str:
    return ctx["perspective"]["name"]


# ---------------------------------------------------------------------------------------------------- players --

def _resolve_player(con, lg: dict, ref: grammar.PlayerRef, question: str) -> dict | None:
    """The one league player the name means, a clarification listing the candidates, or None (no such player). An
    exact name match (accents, case and punctuation folded) wins; a single word may also match a last name. A
    qualifier (position, NFL team, id) narrows same-name players. Never a fuzzy or partial guess."""
    pool = rows(con, """SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.availability_status,
                               fp.on_team_id, t.name AS fantasy_team
                        FROM fantasy_players fp
                        LEFT JOIN fantasy_teams t ON t.league_id = ? AND t.season = ? AND t.team_id = fp.on_team_id
                        WHERE fp.full_name IS NOT NULL""", [lg["league_id"], lg["season"]])
    key = grammar.fold(ref.text)
    cands = [p for p in pool if grammar.fold(p["full_name"]) == key]
    if not cands and " " not in key:
        cands = [p for p in pool if grammar.fold(p["full_name"]).split(" ")[-1] == key]
    if ref.player_id is not None:
        cands = [p for p in cands if p["espn_player_id"] == ref.player_id]
    if ref.position:
        cands = [p for p in cands if p["position"] == ref.position]
    if ref.team:
        cands = [p for p in cands if (p["pro_team"] or "").upper() == ref.team]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    cands.sort(key=lambda p: (p["full_name"], p["position"] or "", p["pro_team"] or "", p["espn_player_id"]))
    by_pos_team: dict = {}
    for p in cands:
        by_pos_team.setdefault((p["position"], p["pro_team"]), []).append(p)
    options = []
    for p in cands[:8]:
        unique = len(by_pos_team[(p["position"], p["pro_team"])]) == 1
        qual = f"{p['position']}, {p['pro_team']}" if unique else f"id {p['espn_player_id']}"
        # The re-ask keeps the asker's own words and adds a qualifier, so it reads the same after provider copy
        # rewording (which never touches *_name values, and only ever the site's uppercase wording).
        options.append({"player_id": p["espn_player_id"], "name": p["full_name"], "position": p["position"],
                        "pro_team": p["pro_team"], "fantasy_team_name": p["fantasy_team"],
                        "ask": _requalify(question, ref, f"{ref.text} ({qual})")})
    return {"status": "clarify", "clarify": {
        "code": "ambiguous_player", "question": f"Which \"{ref.text}\"?",
        "reason": f"{len(cands)} players in this league's pool match that name.", "options": options}}


def _requalify(question: str, ref: grammar.PlayerRef, qualified: str) -> str:
    """The same question with this slot (its name and any earlier qualifier, at the span the grammar matched) replaced
    by an unambiguous one: the clarification's re-ask. Any other slot, even with the same name, is left as asked."""
    if ref.span is None:
        return qualified
    start, end = ref.span
    return question[:start] + qualified + question[end:]


def _roster_status(p: dict, provider: str) -> dict:
    """The screener's ownership wording (app/analytics/screener.ownership): unknown is never available, and a provider
    without a waiver state (Sleeper) says "Not rostered", never "Free agent"."""
    s = p.get("availability_status")
    if s == "ONTEAM":
        return {"status": "rostered", "label": "Rostered", "available": False, "fantasy_team": p.get("fantasy_team")}
    if s == "WAIVERS":
        return {"status": "waivers", "label": "On waivers", "available": True, "fantasy_team": None}
    if s == "FREEAGENT":
        if provider == "espn":
            return {"status": "free_agent", "label": "Free agent", "available": True, "fantasy_team": None}
        return {"status": "not_rostered", "label": "Not rostered", "available": True, "fantasy_team": None}
    return {"status": "unknown", "label": UNKNOWN_OWNERSHIP, "available": False, "fantasy_team": None}


def _who(p: dict) -> dict:
    return {"player_id": p["espn_player_id"], "name": p["full_name"], "position": p["position"],
            "pro_team": p["pro_team"]}


# -------------------------------------------------------------------------------------------------- executors --

def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _screen_call(**kw) -> dict:
    return screener.screen(pos=kw.get("pos"), season=None, week_from=kw.get("week_from"), week_to=kw.get("week_to"),
                           last=kw.get("last"), min_games=kw.get("min_games"), min_routes=kw.get("min_routes"),
                           f=kw.get("f"), owner=kw.get("owner"), sort=kw.get("sort"), dir=kw.get("dir"),
                           limit=str(TOP))


def _screen_card(question: str, parsed: grammar.Parsed, ctx: dict, call: dict, *, title: str | None = None) -> dict:
    out = _screen_call(**call)
    q = out["query"]
    keys = []
    for token in q.get("f") or []:
        k = token.split(":")[1]
        if k not in keys:
            keys.append(k)
    window = out.get("data", {}) or {}
    win = window.get("window") or {}
    if out["status"] == "no_data":
        return _card("unavailable", question, parsed, code="no_data", reason=out.get("message"))
    span = f"weeks {win.get('from')}-{win.get('to')}" if win.get("from") != win.get("to") else f"week {win.get('to')}"
    if win.get("last"):
        span += f", each player's last {win['last']} games played"
    pos = ", ".join(q.get("pos") or [])
    interp = f"{title + ': ' if title else ''}{pos} where {out['expression']} ({span}"
    interp += f", {q['owner']} players)" if q.get("owner") not in (None, "any") else ")"
    columns = [{"key": "name", "label": "Player"}, {"key": "position", "label": "Pos"}, {"key": "team", "label": "NFL"},
               {"key": "games", "label": "Games"}]
    columns += [{"key": k, "label": METRIC_BY_KEY[k].short, "unit": METRIC_BY_KEY[k].unit} for k in keys]
    columns.append({"key": "ownership", "label": "Roster status"})
    shown = []
    for r in out["rows"]:
        cells = {"name": r["name"], "position": r["position"], "team": r["team"], "games": r["games"],
                 "ownership": (r.get("ownership") or {}).get("label")}
        for k in keys:
            m = r["metrics"].get(k) or {}
            cells[k] = {"value": m.get("value"), "n": m.get("n"), "of": m.get("of")}
        shown.append({"cells": cells, "player_id": (r.get("ownership") or {}).get("player_id")})
    c = out["counts"]
    facts = [_fact("Matched", c["matched"]), _fact("Undecided (missing inputs)", c["incomplete"]),
             _fact("Shown", c["shown"])]
    notes = []
    if c["incomplete"]:
        notes.append(f"{c['incomplete']} player(s) could not be decided because an input is missing; missing is never "
                     "counted as 0. The screener lists them.")
    if any(k in ("route_participation", "routes_pg") for k in keys):
        notes.append(window.get("route_data", {}).get("note") or "Route data comes only from an authorized import.")
    headline = (f"{c['matched']} player(s) match." if c["matched"] else "No player matches this screen.")
    link_q = {"pos": q.get("pos"), "from": q.get("from"), "to": q.get("to"), "last": q.get("last"),
              "min_games": q.get("min_games") if q.get("min_games") != 1 else None, "min_routes": q.get("min_routes"),
              "owner": q.get("owner") if q.get("owner") != "any" else None, "sort": q.get("sort"), "dir": q.get("dir")}
    fg, fm, fo, fv = [], [], [], []
    for token in q.get("f") or []:
        g, m, o, v = token.split(":")
        fg.append(g)
        fm.append(m)
        fo.append(o)
        fv.append(v)
    link_q.update({"fg": fg, "fm": fm, "fo": fo, "fv": fv})
    link = {"path": "/screener", "query": {k: v for k, v in link_q.items() if v not in (None, "", [], ())}}
    return _card("answered", question, parsed, interpretation=interp, link=link, notes=notes,
                 freshness={"data_through_week": window.get("latest_week"), "cutoffs": {k: window.get("cutoffs", {})
                            .get(k) for k in keys}, "nfl_synced_at": window.get("nfl_synced_at")},
                 answer={"headline": headline, "facts": facts, "columns": columns, "rows": shown,
                         "truncated": out.get("truncated", False)})


def run_screen(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    s = parsed.slots
    f = [f"{g}:{k}:{op}:{_fmt(v)}" for g, k, op, v in s["filters"]]
    call = {"pos": list(s["positions"]) or None, "f": f, "owner": s["owner"], "sort": s["sort"], "dir": s["dir"]}
    if s.get("last_games"):
        call["last"] = str(s["last_games"])
    if s.get("week_from"):
        call.update(week_from=str(s["week_from"]), week_to=str(s["week_to"]))
    if s.get("last_weeks"):
        probe = _screen_call(**call)                        # the latest week with data, for an exact week range
        latest = ((probe.get("data") or {}).get("latest_week"))
        if latest is None:
            return _card("unavailable", question, parsed, code="no_data", reason=probe.get("message"))
        call.update(week_from=str(max(1, latest - s["last_weeks"] + 1)), week_to=str(latest))
    return _screen_card(question, parsed, ctx, call)


def run_screen_preset(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    p = grammar.PRESET_BY_KEY[parsed.slots["preset"]]
    pq = p["query"]
    call = {"pos": list(pq.get("pos") or []) or None, "f": list(pq.get("f") or []), "owner": pq.get("owner"),
            "sort": pq.get("sort"), "dir": pq.get("dir"),
            "last": str(pq["last"]) if pq.get("last") else None,
            "min_games": str(pq["min_games"]) if pq.get("min_games") else None,
            "min_routes": str(pq["min_routes"]) if pq.get("min_routes") else None}
    card = _screen_card(question, parsed, ctx, call, title=p["title"])
    if card["answer"] is not None:
        card["notes"].insert(0, p["summary"])
    return card


def run_waivers_best(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    pos = parsed.slots.get("position")
    out = routes.waivers(position=pos, recommendation=None, limit=TOP, offset=0, view="BEST", team_id=ctx["team_id"])
    columns = [{"key": "name", "label": "Player"}, {"key": "position", "label": "Pos"}, {"key": "team", "label": "NFL"},
               {"key": "recommendation", "label": "Call"}, {"key": "drop_name", "label": "Suggested drop"}]
    shown = [{"player_id": r["espn_player_id"],
              "cells": {"name": r.get("full_name"), "position": r.get("position"), "team": r.get("pro_team"),
                        "recommendation": r.get("recommendation"), "drop_name": r.get("drop_name")}}
             for r in out["players"]]
    where = f" at {pos}" if pos else ""
    who = out.get("perspective") or ctx["perspective"]
    headline = (f"Top waiver suggestions{where} for {who['name']}." if shown
                else f"No waiver suggestion{where} for {who['name']} in the Best view.")
    notes = []
    if out.get("roster_newer_than_analysis"):
        notes.append("The roster changed after this waiver analysis was computed.")
    cov = out.get("coverage") or {}
    if cov.get("limitation"):
        notes.append(cov["limitation"])
    return _card("answered", question, parsed, interpretation=f"Waivers, Best view{where}, for {who['name']}",
                 answer={"headline": headline, "facts": [_fact("Suggestions", out.get("total"))], "columns": columns,
                         "rows": shown, "truncated": (out.get("total") or 0) > len(shown)},
                 notes=notes, provenance=out.get("provenance"), freshness={"computed_at": out.get("computed_at")},
                 link=_link("/waivers", {"view": "BEST", "pos": pos}, ctx))


SS_HEADLINE = MappingProxyType({
    "upgrade_available": "Start/Sit finds a projected upgrade.",
    "optimal": "The submitted lineup matches the best projected one.",
    "incomplete_data": "Some players have no projection, so Start/Sit claims no gain.",
    "all_locked": "Every rostered player's game has kicked off: nothing can change this week.",
    "future_preview": "A future week: a preview of the best projected lineup, not a submitted one.",
})


def _start_sit(ctx: dict) -> dict:
    return start_sit.get_start_sit(team_id=ctx["team_id"], week=ctx["week"], source=None)


def run_start_sit(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    out = _start_sit(ctx)
    s = out["summary"]
    status = s.get("status")
    strength = s.get("recommendation_strength") or {}
    facts = [_fact("Submitted lineup, projected", s.get("actual_projected_total"), "pts"),
             _fact("Best projected lineup", s.get("recommended_projected_total"), "pts"),
             _fact("Projected gain", s.get("projected_gain"), "pts"),
             _fact("Call strength", strength.get("label"))]
    if s.get("missing_projections"):
        facts.append(_fact("Starters without a projection", len(s["missing_projections"])))
    columns = [{"key": "move", "label": "Move"}, {"key": "gain", "label": "Projected gain", "unit": "pts"}]
    moves = [d for d in out["decisions"] if d.get("action") != "NO_CHANGE"]
    shown = [{"cells": {"move": d.get("explanation"), "gain": d.get("projected_net_gain")}} for d in moves[:TOP]]
    notes = [strength["reason"]] if strength.get("reason") else []
    week = out.get("scoring_period")
    return _card("answered", question, parsed, interpretation=f"Start/Sit for {out['team']['name']}, week {week}",
                 answer={"headline": SS_HEADLINE.get(status, "Start/Sit result."), "facts": facts, "columns": columns,
                         "rows": shown, "truncated": len(moves) > TOP, "status": status},
                 notes=notes, provenance=out.get("provenance"), freshness=out.get("freshness"),
                 link=_link("/start-sit", {"week": ctx["week"]}, ctx))


def _on_roster(out: dict, pid: int) -> bool:
    ids = {p.get("player_id") for p in out.get("actual_lineup") or []}
    ids |= {p.get("player_id") for p in out.get("current_bench") or []}
    ids |= {p.get("player_id") for p in out.get("recommended_lineup") or []}
    return pid in ids


def _preview(question: str, parsed: grammar.Parsed, out: dict, ctx: dict) -> dict | None:
    """Start/Sit lists no alternatives for a future-week preview: nothing about a slot can be established then."""
    if not out.get("is_future_preview"):
        return None
    return _card("abstain", question, parsed, code="preview",
                 reason=f"Week {out.get('scoring_period')} is a preview: Start/Sit lists legal alternatives for the "
                        f"current week only.", link=_link("/start-sit", {"week": out.get("scoring_period")}, ctx))


def run_replacement(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    p = ctx["players"]["p"]
    pid = p["espn_player_id"]
    out = _start_sit(ctx)
    team = out["team"]["name"]
    preview = _preview(question, parsed, out, ctx)
    if preview is not None:
        return preview
    if not _on_roster(out, pid):
        return _card("abstain", question, parsed, code="not_on_roster",
                     reason=f"{p['full_name']} is not on {team}'s roster for this week.")
    link = _link("/start-sit", {"week": ctx["week"]}, ctx)
    interp = f"Start/Sit for {team}, week {out.get('scoring_period')}: the legal alternatives to {p['full_name']}"
    alt = next((a for a in out.get("alternatives") or [] if a.get("starter_id") == pid), None)
    columns = [{"key": "name", "label": "Player"}, {"key": "position", "label": "Pos"},
               {"key": "projection", "label": "Projected", "unit": "pts"},
               {"key": "delta", "label": "Change to the lineup total", "unit": "pts"}]
    if alt is not None:
        opts = alt.get("options") or []
        shown = [{"player_id": o["player_id"], "whatif": o.get("whatif"),
                  "cells": {"name": o["name"], "position": o["position"], "projection": o.get("projection"),
                            "delta": o.get("delta")}} for o in opts[:TOP]]
        headline = (f"{p['full_name']} starts at {alt['label']} in Start/Sit's best projected lineup; these are the "
                    f"closest legal alternatives Start/Sit lists for that slot (at most {MAX_ALTERNATIVES})." if shown
                    else
                    f"No legal alternative to {p['full_name']} at {alt['label']} (kickoff locks, eligibility and IR "
                    f"rules apply).")
        return _card("answered", question, parsed, interpretation=interp, link=link,
                     answer={"headline": headline, "facts": [_fact("Slot", alt["label"])], "columns": columns,
                             "rows": shown, "truncated": len(opts) > TOP},
                     notes=[("A change is the projected lineup total with that player in the slot instead; a player "
                             "without a projection adds nothing known.")],
                     provenance=out.get("provenance"), freshness=out.get("freshness"))
    move = next((d for d in out["decisions"] if any(b.get("id") == pid for b in d.get("bench_players") or [])), None)
    if move is not None:
        shown = [{"player_id": x["id"], "cells": {"name": x["name"], "position": x["position"],
                                                   "projection": x.get("projection"), "delta": None}}
                 for x in move.get("start_players") or []]
        return _card("answered", question, parsed, interpretation=interp, link=link,
                     answer={"headline": f"Start/Sit already moves {p['full_name']} out: {move.get('explanation')}",
                             "facts": [_fact("Projected gain of the move", move.get("projected_net_gain"), "pts")],
                             "columns": columns, "rows": shown, "truncated": False},
                     provenance=out.get("provenance"), freshness=out.get("freshness"))
    kept = next((x for x in out.get("recommended_lineup") or [] if x.get("player_id") == pid), None)
    if kept is not None:
        # Start/Sit lists no alternatives for a slot whose starter is kickoff-locked or has no legal replacement.
        locked = kept.get("lock_status") == "LOCKED"
        return _card("answered", question, parsed, interpretation=interp, link=link,
                     answer={"headline": f"{p['full_name']} starts at {kept.get('slot') or 'his slot'} in Start/Sit's "
                                         f"best projected lineup, and Start/Sit lists no legal replacement for that "
                                         f"slot" + (" (his game has kicked off)." if locked else "."),
                             "facts": [_fact("Slot", kept.get("slot")), _fact("Lock", kept.get("lock_status"))],
                             "columns": [], "rows": [], "truncated": False},
                     provenance=out.get("provenance"), freshness=out.get("freshness"))
    return _card("answered", question, parsed, interpretation=interp, link=link,
                 answer={"headline": f"{p['full_name']} is not in Start/Sit's best projected lineup this week, so "
                                     f"there is no starting slot to replace.", "facts": [], "columns": [], "rows": [],
                         "truncated": False},
                 provenance=out.get("provenance"), freshness=out.get("freshness"))


def run_start_choice(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    a, b = ctx["players"]["p"], ctx["players"]["p2"]
    out = _start_sit(ctx)
    team = out["team"]["name"]
    preview = _preview(question, parsed, out, ctx)
    if preview is not None:
        return preview
    for x in (a, b):
        if not _on_roster(out, x["espn_player_id"]):
            return _card("abstain", question, parsed, code="not_on_roster",
                         reason=f"{x['full_name']} is not on {team}'s roster for this week.")
    link = _link("/start-sit", {"week": ctx["week"]}, ctx)
    interp = f"Start/Sit for {team}, week {out.get('scoring_period')}: {a['full_name']} or {b['full_name']}"
    for starter, other in ((a, b), (b, a)):
        alt = next((x for x in out.get("alternatives") or [] if x.get("starter_id") == starter["espn_player_id"]),
                   None)
        opt = next((o for o in (alt or {}).get("options") or [] if o["player_id"] == other["espn_player_id"]), None)
        if alt is not None and opt is not None:
            return _card("answered", question, parsed, interpretation=interp, link=link, answer={
                "headline": f"Start/Sit's best projected lineup starts {starter['full_name']} at {alt['label']}. "
                            f"Starting {other['full_name']} there instead changes the projected total by the amount "
                            f"below.",
                "facts": [_fact("Slot", alt["label"]), _fact(f"{other['full_name']} projected", opt.get("projection"),
                                                             "pts"),
                          _fact("Change to the lineup total", opt.get("delta"), "pts")],
                "columns": [], "rows": [], "truncated": False, "whatif": opt.get("whatif")},
                provenance=out.get("provenance"), freshness=out.get("freshness"))
    starting = {x.get("player_id") for x in out.get("recommended_lineup") or []}
    a_in, b_in = a["espn_player_id"] in starting, b["espn_player_id"] in starting
    if a_in != b_in:
        # One starts and the other is not among the slot's listed alternatives: the list is capped, so this does not
        # establish that the other cannot take the slot, or what the swap is worth.
        return _card("abstain", question, parsed, code="not_listed", link=link,
                     reason=f"Start/Sit lists only the closest {MAX_ALTERNATIVES} legal alternatives for each slot, so "
                            f"it cannot say here what starting {(b if a_in else a)['full_name']} instead of "
                            f"{(a if a_in else b)['full_name']} would change. Try the move on the Start/Sit page.")
    headline = (f"Start/Sit's best projected lineup starts both {a['full_name']} and {b['full_name']} this week."
                if a_in else
                f"Neither {a['full_name']} nor {b['full_name']} is in Start/Sit's best projected lineup this week.")
    return _card("answered", question, parsed, interpretation=interp, link=link,
                 answer={"headline": headline, "facts": [], "columns": [], "rows": [], "truncated": False},
                 provenance=out.get("provenance"), freshness=out.get("freshness"))


def run_matchup(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    out = matchup_api.get_matchup(week=ctx["week"], team_id=ctx["team_id"], source=None)
    me, opp = out["me"], out.get("opponent")
    week = out.get("week")
    link = _link("/matchup", {"week": ctx["week"]}, ctx)
    if opp is None:
        return _card("answered", question, parsed, interpretation=f"Matchup for {me['name']}, week {week}", link=link,
                     answer={"headline": f"{me['name']} has no opponent in week {week}.", "facts": [], "columns": [],
                             "rows": [], "truncated": False},
                     notes=out.get("notes") or [], provenance=out.get("provenance"))
    facts = [_fact(f"{me['name']} projected", me.get("projected_points"), "pts"),
             _fact(f"{opp['name']} projected", opp.get("projected_points"), "pts"),
             _fact(f"{me['name']} actual", me.get("actual_points"), "pts"),
             _fact(f"{opp['name']} actual", opp.get("actual_points"), "pts"),
             _fact("Status", out.get("status"))]
    notes = list(out.get("notes") or [])
    if not (me.get("projection_complete") and opp.get("projection_complete")):
        notes.append("A side's projection is incomplete, so its projected total is not shown as a number.")
    return _card("answered", question, parsed, interpretation=f"Matchup for {me['name']}, week {week}", link=link,
                 answer={"headline": f"Week {week}: {me['name']} vs {opp['name']}.", "facts": facts, "columns": [],
                         "rows": [], "truncated": False},
                 notes=notes, provenance=out.get("provenance"))


def run_player(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    p = ctx["players"]["p"]
    out = routes.player_detail(p["espn_player_id"])
    pl = out["player"]
    status = _roster_status({**pl, "fantasy_team": pl.get("fantasy_team")}, ctx["provider"])
    facts = [_fact("Position", pl.get("position")), _fact("NFL team", pl.get("pro_team")),
             _fact("Injury status", pl.get("injury_status")),
             _fact("Roster status", status["label"] + (f" ({status['fantasy_team']})" if status["fantasy_team"]
                                                       else "")),
             _fact("Season points", pl.get("season_points"), "pts"),
             _fact("Points per game", pl.get("season_avg_points"), "pts"),
             _fact("xFP per game", pl.get("xfp_per_game"), "pts")]
    recent = (out.get("league_points") or [])[-3:] if isinstance(out.get("league_points"), list) else []
    columns = [{"key": "week", "label": "Week"}, {"key": "actual", "label": "Points", "unit": "pts"},
               {"key": "projected", "label": "Projected", "unit": "pts"}]
    shown = [{"cells": {"week": r.get("scoring_period"), "actual": r.get("actual_points"),
                        "projected": r.get("projected_points")}} for r in recent]
    return _card("answered", question, parsed, interpretation=f"Player snapshot: {pl['full_name']}",
                 answer={"headline": f"{pl['full_name']} ({pl.get('position')}, {pl.get('pro_team')}).",
                         "facts": facts, "columns": columns, "rows": shown, "truncated": False},
                 notes=["League points are this league's scoring; the last three stored weeks are shown."],
                 provenance=out.get("provenance"),
                 link={"path": "/players", "query": {"id": pl["espn_player_id"]}})


def run_ownership(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    p = ctx["players"]["p"]
    out = routes.player_detail(p["espn_player_id"])
    pl = out["player"]
    status = _roster_status({**pl, "fantasy_team": pl.get("fantasy_team")}, ctx["provider"])
    if status["status"] == "rostered":
        headline = f"{pl['full_name']} is rostered by {status['fantasy_team'] or 'a team in this league'}."
    elif status["available"]:
        headline = f"{pl['full_name']} is available: {status['label'].lower()}."
    else:
        headline = f"{pl['full_name']}: {status['label'].lower()}, so not shown as available."
    facts = [_fact("Roster status", status["label"]), _fact("Fantasy team", status["fantasy_team"]),
             _fact("Rostered in ESPN leagues", pl.get("percent_owned"), "%")]
    if ctx["provider"] != "espn":
        facts = facts[:2]
    return _card("answered", question, parsed, interpretation=f"Roster status in {ctx['lg']['name']}: "
                                                              f"{pl['full_name']}",
                 answer={"headline": headline, "facts": facts, "columns": [], "rows": [], "truncated": False,
                         "available": status["available"]},
                 provenance=out.get("provenance"), link={"path": "/players", "query": {"id": pl["espn_player_id"]}})


def run_compare(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    a, b = ctx["players"]["p"], ctx["players"]["p2"]
    out = player_compare.compare_players(a=a["espn_player_id"], b=b["espn_player_id"], source=None)
    names = {"a": out["players"][0]["player"]["full_name"], "b": out["players"][1]["player"]["full_name"]}
    columns = [{"key": "point", "label": "Point"}, {"key": "leader_name", "label": "Edge"},
               {"key": "text", "label": "Why"}]
    shown = [{"cells": {"point": s.get("title"), "leader_name": names.get(s.get("leader")) if s.get("leader") else None,
                        "text": s.get("text")}} for s in out.get("summary") or []]
    return _card("answered", question, parsed, interpretation=f"Compare {names['a']} and {names['b']}",
                 answer={"headline": f"{names['a']} vs {names['b']}.", "facts": [], "columns": columns,
                         "rows": shown[:TOP], "truncated": len(shown) > TOP},
                 notes=list(out.get("limitations") or [])[:3] + ([out["disclaimer"]] if out.get("disclaimer") else []),
                 freshness=out.get("freshness"),
                 link={"path": "/compare", "query": {"a": a["espn_player_id"], "b": b["espn_player_id"]}})


def _values_head(out: dict) -> list[str]:
    return [x for x in (out.get("label"), out.get("note")) if x]


def run_trade_value(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    p = ctx["players"]["p"]
    out = trade_values.trade_values()
    link = {"path": "/trades/values", "query": {"player": p["espn_player_id"]}}
    if not out.get("available"):
        return _card("unavailable", question, parsed, code="unavailable", reason=out.get("reason"), link=link)
    row = next((r for r in out.get("players") or [] if r.get("espn_player_id") == p["espn_player_id"]), None)
    if row is None:
        return _card("abstain", question, parsed, code="not_valued",
                     reason=f"{p['full_name']} is not in the trade-value table (it covers rostered players).",
                     link=link)
    facts = [_fact("Value", row.get("value")), _fact("Value per week", row.get("value_per_week")),
             _fact("Rank", row.get("rank")), _fact("Position rank", row.get("position_rank")),
             _fact("Weeks counted", row.get("weeks_counted"))]
    notes = _values_head(out)
    if row.get("rank") is None and row.get("rank_reason"):
        notes.append(row["rank_reason"])
    return _card("answered", question, parsed, interpretation=f"Trade value: {row['name']}",
                 answer={"headline": f"{row['name']} ({row.get('position')}, {row.get('team_name') or 'not rostered'}).",
                         "facts": facts, "columns": [], "rows": [], "truncated": False},
                 notes=notes, provenance=out.get("provenance"), link=link)


def run_trade_values_top(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    pos = parsed.slots.get("position")
    out = trade_values.trade_values()
    link = {"path": "/trades/values", "query": {"pos": pos} if pos else {}}
    if not out.get("available"):
        return _card("unavailable", question, parsed, code="unavailable", reason=out.get("reason"), link=link)
    ranked = [r for r in out.get("players") or [] if r.get("rank") is not None and (pos is None
                                                                                   or r.get("position") == pos)]
    ranked.sort(key=lambda r: (r["rank"], r.get("espn_player_id")))
    columns = [{"key": "rank", "label": "Rank"}, {"key": "name", "label": "Player"}, {"key": "position", "label": "Pos"},
               {"key": "team", "label": "Fantasy team"}, {"key": "value", "label": "Value"}]
    shown = [{"player_id": r["espn_player_id"], "cells": {"rank": r["rank"], "name": r["name"],
                                                          "position": r.get("position"), "team": r.get("team_name"),
                                                          "value": r.get("value")}} for r in ranked[:TOP]]
    where = f" at {pos}" if pos else ""
    return _card("answered", question, parsed, interpretation=f"Top trade values{where}",
                 answer={"headline": f"The highest trade values{where} in this league." if shown
                         else f"No ranked trade value{where}.", "facts": [], "columns": columns, "rows": shown,
                         "truncated": len(ranked) > TOP},
                 notes=_values_head(out), provenance=out.get("provenance"), link=link)


def run_byes(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    out = schedule_intelligence.bye_planner(team_id=ctx["team_id"])
    weeks = out.get("weeks") or []
    # "this week" is the league's current week; the planner drops a week once its games have kicked off, and then
    # the card abstains rather than answer for another week
    want = ctx["week"] if ctx["week"] is not None else ctx["lg"].get("current_week")
    link = {"path": "/schedule-intelligence", "query": {"team": ctx["team_id"]} if ctx["team_id"] is not None
            else {}, "hash": "bye-planner"}
    row = next((w for w in weeks if w.get("week") == want), None)
    team = (out.get("team") or {}).get("name") or _team_name(ctx)
    if row is None:
        return _card("abstain", question, parsed, code="no_week",
                     reason=f"The bye planner has no week {want} (it covers the remaining weeks).", link=link)

    by_id = {pl.get("espn_player_id"): pl.get("name") for pl in out.get("players") or []}

    def names(ids):
        """The planner lists player ids; their names come from its own players table (an id it does not name is shown
        as the id, never dropped)."""
        return [by_id.get(i) or f"player id {i}" for i in ids or []]

    facts = [_fact("Week", row["week"]), _fact("Status", row.get("status_text") or row.get("status")),
             _fact("Starting slots covered", row.get("covered")), _fact("Starting slots required", row.get("required")),
             _fact("Starters on bye", ", ".join(n for n in names(row.get("starters_on_bye")) if n) or None),
             _fact("Players on bye", ", ".join(n for n in names(row.get("byes")) if n) or None)]
    s = out.get("summary") or {}
    notes = []
    if s.get("conflict_weeks"):
        notes.append("Weeks with a bye conflict: " + ", ".join(str(w) for w in s["conflict_weeks"]) + ".")
    return _card("answered", question, parsed, interpretation=f"Bye planner for {team}, week {row['week']}",
                 answer={"headline": f"Week {row['week']}: {row.get('status_text') or row.get('status')}.",
                         "facts": facts, "columns": [], "rows": [], "truncated": False},
                 notes=notes, provenance=out.get("provenance"), link=link)


def run_playoffs(question: str, parsed: grammar.Parsed, ctx: dict) -> dict:
    out = playoff_race.playoff_race(team_id=ctx["team_id"], base="projected", override=None)
    focus = out.get("focus_team_id")
    t = next((x for x in out.get("teams") or [] if x.get("team_id") == focus), None)
    link = _link("/playoff-race", {}, ctx)
    if t is None:
        return _needs_team(question, parsed)
    cur, proj = t.get("current") or {}, t.get("projected") or {}
    spots = ((out.get("scenario") or {}).get("cutline") or {}).get("playoff_spots")

    def rec(r):
        r = r or {}
        return f"{r.get('wins')}-{r.get('losses')}" + (f"-{r['ties']}" if r.get("ties") else "")

    facts = [_fact("Record", rec(cur.get("record"))), _fact("Current place", cur.get("rank")),
             _fact("Current status", cur.get("status")), _fact("Projected record", rec(proj.get("record"))),
             _fact("Projected place", proj.get("rank")), _fact("Projected status", proj.get("status")),
             _fact("Playoff spots", spots)]
    notes = [("Fantasy Intel computes no playoff odds or probability: these are the deterministic race's places "
              "and statuses, current and in the projected scenario."), *list(out.get("notes") or [])[:2]]
    clinch = (out.get("rules") or {}).get("clinch")
    if clinch:
        notes.append(clinch)
    return _card("answered", question, parsed, interpretation=f"Playoff race for {t['name']} (projected scenario)",
                 answer={"headline": f"{t['name']} is {cur.get('status')} the playoff line now and "
                                     f"{proj.get('status')} it in the projected scenario. This is a deterministic "
                                     f"scenario, not a probability.",
                         "facts": facts, "columns": [], "rows": [], "truncated": False},
                 notes=notes, link=link)


EXECUTORS: MappingProxyType[str, Callable[[str, grammar.Parsed, dict], dict]] = MappingProxyType({
    "screen": run_screen, "screen_preset": run_screen_preset, "waivers_best": run_waivers_best,
    "start_sit": run_start_sit, "replacement": run_replacement, "start_choice": run_start_choice,
    "matchup": run_matchup, "player": run_player, "ownership": run_ownership, "compare": run_compare,
    "trade_value": run_trade_value, "trade_values_top": run_trade_values_top, "byes": run_byes,
    "playoffs": run_playoffs,
})
assert set(EXECUTORS) == set(grammar.INTENTS), "every intent has exactly one executor"
