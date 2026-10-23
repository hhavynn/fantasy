"""League compatibility diagnostic, read-only (contract league-compatibility/v1; issue #253, decision D-12).

For one league, per analysis tool: is the answer supported, and if not, why, what is missing, which provider boundary
causes it and what the manager can do. It is computed per request from the league's synced file (no writes, no provider
calls) on top of the league rules (app/analytics/league_rules.py): the rules page's `engine_support` rows (by their
stable `code`) are the source of every scoring statement here, and nothing re-derives scoring support.

Four dimensions are kept apart and never collapsed into one "ready" flag:

  import   what the sync stored: slot counts, scoring rules, the current week's rosters, every rostered player's data;
  roster   whether the engines can fill and count this league's roster: slots no engine fills, capacity, IR rules;
  scoring  whether the projections a tool reads exist in this league's scoring (provider projections, Fantasy Intel's
           own (Ours), the kicker and D/ST models) and cover the rostered players. Roster support is not scoring
           fidelity: a structurally handled league can still get partial or unsupported scoring for a tool;
  feature  what the tool itself does with the league: lineup totals known-only, rules it does not model.

Statuses are league_rules' own (handled / partial / unsupported / unverified). An entry takes the worst of its findings
(unsupported > unverified > partial > handled); every finding stays listed under `evidence` by its code. Codes name
the concrete gap, e.g. `unfilled_slots`, `players_missing`, `projection_coverage` (docs:
fantasy-intel/docs/compatibility/253-gap-inventory.md).

No league ids, member ids, player names or credentials are returned: counts, slot labels and positions only.
"""

from __future__ import annotations

import json
from typing import Any

from ..db.connection import fetch_dicts, table_exists
from .league_rules import HANDLED, PARTIAL, UNSUPPORTED, UNVERIFIED, build_league_rules
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY
from .reserve_slots import is_reserved

CONTRACT = "league-compatibility/v1"
DIMENSIONS = ("import", "roster", "scoring", "feature")
SEVERITY = {HANDLED: 0, PARTIAL: 1, UNVERIFIED: 2, UNSUPPORTED: 3}
SKILL = ("QB", "RB", "WR", "TE")
TEAMLESS = {None, "", "FA"}
FAMILY = {"QB": "skill_projections", "RB": "skill_projections", "WR": "skill_projections", "TE": "skill_projections",
          "K": "kicker_projections", "D/ST": "dst_projections"}
FAMILY_TEXT = {"skill_projections": "QB, RB, WR and TE", "kicker_projections": "kicker",
               "dst_projections": "D/ST"}
ALL = ("QB", "RB", "WR", "TE", "K", "D/ST")

# tool -> (label, kind, positions read, checks roster capacity). Kinds: lineup (one week's recommendation), totals
# (optimal-lineup totals over weeks), waivers, bye (schedule only), kdst (K and D/ST streaming), rankings (players).
TOOLS: dict[str, tuple[str, str, tuple[str, ...], bool]] = {
    "start_sit": ("Start/Sit", "lineup", ALL, False),
    "what_if": ("Start/Sit what-if", "lineup", ALL, False),
    "lineup_projections": ("Projected lineups", "totals", ALL, False),
    "team_value": ("Team value", "totals", ALL, True),
    "waivers": ("Waivers", "waivers", ALL, True),
    "add_drop": ("Add/drop simulator", "totals", ALL, True),
    "trades": ("Trade analyzer", "totals", ALL, True),
    "trade_discovery": ("Trade discovery", "totals", ALL, True),
    "bye_planner": ("Bye planner", "bye", (), False),
    "kdst_streaming": ("Kicker and D/ST streaming", "kdst", ("K", "D/ST"), False),
    "rankings": ("Player rankings", "rankings", SKILL, False),
    "power_rankings": ("Power rankings", "totals", ALL, False),
    "playoff_race": ("Playoff race", "totals", ALL, False),
}


# ---- entries ---------------------------------------------------------------------------------------------------------

def finding(code: str, status: str, reason: str, *, missing_input: str | None = None,
            provider_boundary: str | None = None, action: str | None = None, value: Any = None) -> dict:
    return {"code": code, "status": status, "reason": reason, "missing_input": missing_input,
            "provider_boundary": provider_boundary, "action": action, "value": value}


def entry(findings: list[dict], handled_reason: str) -> dict:
    """The worst status of the findings; the reasons of every finding that is not handled, most severe first."""
    ordered = sorted(findings, key=lambda f: -SEVERITY[f["status"]])
    bad = [f for f in ordered if f["status"] != HANDLED]

    def first(key: str, pool: list[dict]) -> str | None:
        return next((f[key] for f in pool if f[key]), None)

    return {
        "status": bad[0]["status"] if bad else HANDLED,
        "reason": " ".join(f["reason"] for f in bad) if bad else handled_reason,
        "missing_input": first("missing_input", bad),
        "provider_boundary": first("provider_boundary", ordered),
        "action": first("action", bad),
        "evidence": [{"code": f["code"], "status": f["status"], "value": f["value"]} for f in findings],
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---- facts from the league file ---------------------------------------------------------------------------------------

def load_facts(con, lg: dict) -> dict:
    """The few league-file facts the diagnostic needs beyond the `leagues` row (one league per file)."""
    from . import projection_source as ps
    from .league_rules import provider_of, skill_runs_rescored

    season, league_id = lg.get("season"), lg.get("league_id")
    week, final = lg.get("current_week"), lg.get("final_scoring_period")
    week = min(int(week), int(final)) if week and final else (int(week) if week else None)
    teams = fetch_dicts(con, "SELECT team_id FROM fantasy_teams WHERE league_id = ? AND season = ?", [league_id, season])
    rows = fetch_dicts(con, """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, l.lineup_slot, fp.espn_player_id AS known_id,
               fp.position, fp.pro_team
        FROM lineup_snapshots l LEFT JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ?""", [league_id, season, week]) if week else []
    settings = lg.get("settings_raw")
    settings = json.loads(settings) if isinstance(settings, str) else settings
    provider = provider_of(settings if isinstance(settings, dict) else {})
    source = ps.default_source(con, season)
    # The projections the engines read (projection_source.league_projections, as trade_data and waiver_data do) and,
    # for a league whose default is Ours, whether a verified run is shown (select_run, the switch's own selection).
    projected: set = set()
    independent = None
    if week and table_exists(con, "espn_player_weekly_points"):
        shown_values = ps.league_projections(con, season, [week], source)
        projected = {pid for pid, by_week in shown_values.items() if week in by_week}
        if source == "independent":
            independent = {"available": ps.select_run(con, season, week)["run"] is not None}
    if source == "independent" and independent and independent["available"]:
        shown = "ours"
    else:
        shown = "provider" if projected else "none"
    coverage: dict[str, dict] = {}
    for r in rows:
        if r["known_id"] is None or is_reserved(r) or r["pro_team"] in TEAMLESS or r["position"] not in ALL:
            continue
        c = coverage.setdefault(r["position"], {"rostered": 0, "unprojected": 0})
        c["rostered"] += 1
        c["unprojected"] += r["espn_player_id"] not in projected
    rostered = {r["espn_player_id"] for r in rows}
    fa_projected: dict[str, int] = {}
    for r in fetch_dicts(con, """SELECT espn_player_id, position FROM fantasy_players
                                 WHERE availability_status IN ('FREEAGENT', 'WAIVERS')"""):
        if r["espn_player_id"] not in rostered and r["espn_player_id"] in projected:
            fa_projected[r["position"]] = fa_projected.get(r["position"], 0) + 1
    return {
        "provider": provider, "week": week, "teams": len(teams),
        "teams_with_roster": len({r["team_id"] for r in rows}),
        "players_missing": sum(1 for r in rows if r["known_id"] is None),
        "projection_source": shown, "independent": independent, "ours_rescored": skill_runs_rescored(con, lg),
        "coverage": coverage, "free_agents_projected": fa_projected,
    }


# ---- the diagnostic ---------------------------------------------------------------------------------------------------

def _started_positions(slot_codes: set[str]) -> set[str]:
    out = {s for s in slot_codes if s in FIXED_SLOTS}
    for s in slot_codes:
        out |= set(FLEX_ELIGIBILITY.get(s, ()))
    return out


class _League:
    """The league rules and facts, with the shared findings every tool reuses."""

    def __init__(self, rules: dict, facts: dict):
        self.rules, self.facts = rules, facts
        self.provider = facts.get("provider") or rules["source"]["provider"]
        self.name = rules["source"]["provider_label"]
        self.week = facts.get("week")
        self.support = {r["code"]: r for r in rules["engine_support"]}
        slots = rules["roster"]["slots"]
        self.slots = {s["code"]: s["count"] for s in slots}
        self.unfilled = [s["code"] for s in slots if s["kind"] in ("starter", "unknown")
                         and s["engine"]["status"] == UNSUPPORTED]
        self.starts = _started_positions(set(self.slots))
        self.starters = rules["roster"]["starters"]["value"]

    def wk(self) -> str:
        return f"week {self.week}" if self.week else "the current week"

    # -- import
    def import_findings(self) -> list[dict]:
        f, lg = self.facts, self.rules
        out = []
        if not self.slots:
            out.append(finding("roster_slots", UNSUPPORTED, "No roster slot counts were synced, so no lineup can be "
                               "built.", missing_input="roster slot counts", action="Sync the league again."))
        if not lg["scoring"]["rules"] and not lg["scoring"]["zero_rules"]:
            out.append(finding("scoring_rules", PARTIAL, "No scoring rules were synced.",
                               missing_input="scoring settings", action="Sync the league again."))
        teams, have = f.get("teams") or 0, f.get("teams_with_roster") or 0
        if teams and have < teams:
            out.append(finding("rosters", PARTIAL, f"{_plural(teams - have, 'team')} of {teams} have no roster for "
                               f"{self.wk()}.", missing_input=f"rosters for {self.wk()}",
                               action="Sync the league again.", value=teams - have))
        if f.get("players_missing"):
            n = f["players_missing"]
            out.append(finding("players_missing", PARTIAL, f"{_plural(n, 'rostered player')} {'is' if n == 1 else 'are'}"
                               " not in the player data (positions Fantasy Intel does not store, such as individual "
                               "defensive players), so rosters leave them out.",
                               missing_input="player data for those players",
                               provider_boundary="Fantasy Intel stores offensive players, kickers and team defenses "
                                                 "only.", value=n))
        return out

    # -- roster
    def unfilled_finding(self, kind: str) -> list[dict]:
        if not self.unfilled:
            return []
        slots = ", ".join(self.unfilled)
        text = {
            "lineup": f"{slots}: no engine fills these slots, so the recommendation leaves them out and its projected "
                      "gain over your lineup does not count your starters there.",
            "totals": f"{slots}: no engine fills these slots, so lineup totals leave them out.",
            "waivers": f"{slots}: no engine fills these slots, so roster capacity is unknown and add/drop moves are "
                       "not planned.",
            "bye": f"{slots}: who may fill these slots is not established, so weeks that need them are unconfirmed.",
            "league": f"{slots}: no engine fills these slots; projected lineups and totals leave them out.",
        }[kind]
        return [finding("unfilled_slots", PARTIAL, text, missing_input="an engine for these slots",
                        action="Read lineup totals as covering the other starting slots only.", value=self.unfilled)]

    def missing_players_finding(self, capacity: bool) -> list[dict]:
        n = self.facts.get("players_missing") or 0
        if not n:
            return []
        if capacity:
            return [finding("players_missing", UNSUPPORTED, f"{_plural(n, 'rostered player')} not in the player data "
                            "are left out of active roster counts, so roster capacity cannot be checked.",
                            missing_input="player data for those players",
                            action="Check your open roster spots in the provider's app.", value=n)]
        return [finding("players_missing", PARTIAL, f"{_plural(n, 'rostered player')} not in the player data "
                        "are left out of rosters.", missing_input="player data for those players", value=n)]

    # -- scoring
    def scoring_findings(self, positions: tuple[str, ...]) -> list[dict]:
        used = [p for p in positions if p in self.starts]
        if not used:
            return []
        out = []
        f = self.facts
        shown = f.get("projection_source")
        if self.provider == "espn":
            if shown == "none":
                out.append(finding("provider_projections", UNSUPPORTED, f"No projections are stored for {self.wk()}.",
                                   missing_input=f"projections for {self.wk()}",
                                   action="Check again after the next sync."))
            else:
                out.append(finding("provider_projections", HANDLED, "ESPN projects every player in this league's own "
                                   "scoring.", provider_boundary="ESPN's own projections, in this league's scoring."))
        else:
            boundary = (f"{self.name} publishes no projections, so Fantasy Intel's own (Ours) are the only ones."
                        if self.provider != "espn" else None)
            for code in dict.fromkeys(FAMILY[p] for p in used):
                row = self.support.get(code)
                if row is None:
                    continue
                if row["status"] == HANDLED:
                    out.append(finding(code, HANDLED, row["detail"], provider_boundary=boundary))
                elif row["status"] == PARTIAL:      # re-scored from the reference scoring (league_rules, rescore.py)
                    out.append(finding(code, PARTIAL, row["detail"],
                                       missing_input=f"{FAMILY_TEXT[code]} projections made in this league's scoring",
                                       provider_boundary=boundary, value={"follow_up": "#254"}))
                else:
                    out.append(finding(code, UNSUPPORTED, row["detail"],
                                       missing_input=f"{FAMILY_TEXT[code]} projections in this league's scoring",
                                       provider_boundary=boundary,
                                       action="See League rules for the scoring rules the models do not implement."))
            if shown != "ours" and not any(x["status"] == UNSUPPORTED for x in out):
                out.append(finding("independent_run", UNVERIFIED, "No verified run of Fantasy Intel's own projections "
                                   f"(Ours) in this league's scoring is stored for {self.wk()}, so there are no "
                                   "projections to use.",
                                   missing_input=f"projections for {self.wk()}", provider_boundary=boundary,
                                   action="Check again after the next sync."))
        cov = f.get("coverage") or {}
        gaps = {p: cov[p]["unprojected"] for p in used if cov.get(p, {}).get("unprojected")}
        if gaps:
            n = sum(gaps.values())
            out.append(finding("projection_coverage", PARTIAL,
                               f"{_plural(n, 'rostered player')} ({', '.join(gaps)}) "
                               f"{'has' if n == 1 else 'have'} no projection for {self.wk()}.",
                               missing_input="projections for those players", value=gaps))
        return out

    def coverage_gap(self, positions: tuple[str, ...]) -> dict:
        cov = self.facts.get("coverage") or {}
        return {p: cov[p]["unprojected"] for p in positions if p in self.starts and cov.get(p, {}).get("unprojected")}

    # -- per tool
    def tool(self, code: str) -> dict:
        label, kind, positions, capacity = TOOLS[code]
        imp = entry(self.import_findings(), "League settings, rosters and players for "
                                             f"{self.wk()} were synced.")
        roster, scoring, feature = [], [], []
        slot_kind = {"lineup": "lineup", "totals": "totals", "waivers": "waivers", "bye": "bye"}.get(kind)
        if slot_kind:
            roster += self.unfilled_finding(slot_kind)
        if kind in ("lineup", "totals", "waivers"):
            roster += self.missing_players_finding(capacity)
        if code in ("waivers",) and self.provider != "espn":
            roster.append(finding("roster_restrictions", UNVERIFIED, f"{self.name}'s synced settings carry no roster "
                                  "restrictions, so the waiver planner cannot establish whether a claim needs a drop.",
                                  missing_input="roster restrictions",
                                  provider_boundary=f"{self.name} league settings as synced.",
                                  action=f"Check your open roster spots in {self.name} before claiming."))
        if code in ("waivers", "add_drop") and self.provider != "espn" and self.slots.get("IR"):
            roster.append(finding("ir_eligibility", PARTIAL, "IR eligibility allows only players listed IR or out; "
                                  "this league's own IR rules are not read.", missing_input="the league's IR rules",
                                  action="Check who may use an IR slot in your league settings."))
        if kind == "kdst":
            extra = {p: self.slots.get(p, 0) for p in ("K", "D/ST") if self.slots.get(p, 0) > 1}
            if extra:
                roster.append(finding("streaming_slots", PARTIAL, f"The plan streams one slot per position; this "
                                      f"league starts {', '.join(f'{n} {p}' for p, n in extra.items())}.",
                                      missing_input="a plan for every K and D/ST slot", value=extra))
        if kind != "bye":
            scoring += self.scoring_findings(positions)
        if code == "waivers":
            refs = [c for c in ("ppr", "te_premium") if c in self.support]
            scoring.append(finding("player_value_basis", PARTIAL, "Waiver scores rank players by recent usage "
                                   "(expected points, full PPR) as a percentile within each position, not by this "
                                   "league's scoring.", missing_input="usage values in this league's scoring",
                                   action="Compare the projected points shown beside each claim.", value=refs or None))
        # feature
        if kind == "lineup":
            lock = self.support.get("lineup_lock")
            if lock is not None:
                feature.append(finding("lineup_lock", lock["status"], lock["detail"],
                                       missing_input="the league's lineup-lock rule" if lock["status"] != HANDLED
                                       else None))
        if kind in ("lineup", "totals"):
            gaps = self.coverage_gap(positions)
            if gaps:
                n = sum(gaps.values())
                feature.append(finding("totals_known_only", UNVERIFIED, f"Lineup totals are known-only while "
                                       f"{_plural(n, 'rostered player')} ({', '.join(gaps)}) "
                                       f"{'has' if n == 1 else 'have'} no projection.",
                                       missing_input="projections for those players",
                                       action="Read totals marked known-only as partial sums.", value=gaps))
        if code == "trade_discovery":
            thin = sorted(p for p in self.starts if not (self.facts.get("free_agents_projected") or {}).get(p))
            if thin:
                feature.append(finding("replacement_level", PARTIAL, f"No free agent at {', '.join(thin)} has a "
                                       "projection, so asset balance uses the league-structure replacement level, "
                                       "which falls to 0 when the pool is too small.",
                                       missing_input="projected free agents at those positions", value=thin))
        if code in ("waivers", "add_drop", "trades", "trade_discovery") and "undroppable" in self.support:
            feature.append(finding("undroppable", PARTIAL, self.support["undroppable"]["detail"],
                                   missing_input="the undroppable list"))
        if code == "waivers":
            feature.append(finding("missing_value", PARTIAL, "A player without usage data counts as 0 in the waiver "
                                   "lineup math, and flex and superflex choices compare position percentiles, not "
                                   "points.", missing_input="usage data for every player"))
        if kind == "kdst" and not any(self.slots.get(p) for p in ("K", "D/ST")):
            feature.append(finding("not_started", UNSUPPORTED, "The league starts no kicker or D/ST.",
                                   action="Nothing to plan."))
        if kind == "bye":
            scoring_entry = entry([], "Uses NFL schedules and availability only, no projections or scoring.")
        else:
            scoring_entry = entry(scoring, "Projections exist in this league's scoring for every rostered player "
                                           "this tool reads.")
        return {
            "tool": code, "label": label,
            "import": imp,
            "roster": entry(roster, self.roster_handled(kind)),
            "scoring": scoring_entry,
            "feature": entry(feature, "Available for this league."),
            "projection_source": None if kind == "bye" else self.facts.get("projection_source"),
            "refs": sorted({x["code"] for x in roster + scoring + feature if x["code"] in self.support}),
        }

    def roster_handled(self, kind: str) -> str:
        if kind == "rankings":
            return "Ranks players by position; lineup slots are not used."
        n = self.starters if isinstance(self.starters, int) else len(self.slots)
        return (f"All {_plural(n, 'starting slot')} are filled by the lineup engines; IR and taxi players never "
                "start.")


def build_compatibility(lg: dict, facts: dict) -> dict:
    """The diagnostic payload from one `leagues` row (all columns) and its league-file facts (load_facts)."""
    rules = build_league_rules(lg, ours_rescored=facts.get("ours_rescored"))
    L = _League(rules, facts)
    league_roster = L.unfilled_finding("league") + L.missing_players_finding(False)
    return {
        "contract": CONTRACT,
        "source": rules["source"],
        "week": L.week,
        "dimensions": {
            "import": entry(L.import_findings(), f"League settings, rosters and players for {L.wk()} were synced."),
            "roster": entry(league_roster, L.roster_handled("totals")),
            "scoring": entry(L.scoring_findings(ALL), "Projections exist in this league's scoring for every rostered "
                                                      "player."),
        },
        "tools": [L.tool(code) for code in TOOLS],
        "unmapped_count": len(rules["unmapped"]),
        "note": "Each tool is judged on four separate dimensions; a connected league is not automatically supported "
                "by every tool. Statuses restate what the engines do with this league's synced data.",
    }
