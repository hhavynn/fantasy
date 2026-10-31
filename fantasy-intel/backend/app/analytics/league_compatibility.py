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
from .lineup import FIXED_SLOTS, FLEX_SLOTS, dual_positions, is_flex, lineup_slots, separable, slot_positions
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

def active_dual_position_count(rows: list[dict], slots: list[str]) -> int:
    """Unique rostered, non-reserve players with provider evidence for a second base position that this league's
    starting `slots` take: the lineup solver's own test (lineup.dual_positions), so a WR/TE in a league without a TE
    or flex slot that takes TE is not counted, exactly as the solver would never start him there.

    Eligible flex/bench/IR slot IDs alone are NOT dual-position evidence. No IDs or names leave this function.
    """
    return len({
        r["espn_player_id"] for r in rows
        if r.get("known_id") is not None and not is_reserved(r) and dual_positions(r, slots)
    })


def load_facts(con, lg: dict) -> dict:
    """The few league-file facts the diagnostic needs beyond the `leagues` row (one league per file)."""
    from . import projection_source as ps
    from .league_rules import provider_of, skill_runs_rescored

    season, league_id = lg.get("season"), lg.get("league_id")
    week, final = lg.get("current_week"), lg.get("final_scoring_period")
    week = min(int(week), int(final)) if week and final else (int(week) if week else None)
    teams = fetch_dicts(con, "SELECT team_id FROM fantasy_teams WHERE league_id = ? AND season = ?", [league_id, season])
    rows = fetch_dicts(con, """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, l.lineup_slot, l.is_starter,
               fp.espn_player_id AS known_id, fp.position, fp.pro_team, fp.eligible_slots
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
    # A verified run whose rows all abstain shows no number: "none", exactly as a week without any projection.
    if not projected:
        shown = "none"
    else:
        shown = "ours" if source == "independent" and independent and independent["available"] else "provider"
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
    fillable = {*FIXED_SLOTS, *FLEX_SLOTS}
    roster_slots = lg.get("roster_slots")
    roster_slots = json.loads(roster_slots) if isinstance(roster_slots, str) else roster_slots
    starting = lineup_slots(roster_slots if isinstance(roster_slots, dict) else {})
    return {
        "provider": provider, "week": week, "teams": len(teams),
        # starters the engines load (in the player data) who sit in a slot no engine fills (IDP, P, HC, ...)
        "starters_in_unfilled_slots": sum(1 for r in rows if r["known_id"] is not None and r["is_starter"]
                                          and r["lineup_slot"] and r["lineup_slot"] not in fillable),
        "teams_with_roster": len({r["team_id"] for r in rows}),
        "players_missing": sum(1 for r in rows if r["known_id"] is None),
        "active_dual_position_players": active_dual_position_count(rows, starting),
        "projection_source": shown, "independent": independent, "ours_rescored": skill_runs_rescored(con, lg),
        "coverage": coverage, "free_agents_projected": fa_projected,
    }


# ---- the diagnostic ---------------------------------------------------------------------------------------------------

def _started_positions(slot_codes: set[str]) -> set[str]:
    out = {s for s in slot_codes if s in FIXED_SLOTS}
    for s in slot_codes:
        if is_flex(s):
            out |= set(slot_positions(s))
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
        exact = ("a lineup change is still measured exactly when it moves nobody who can start there" if
                 separable(self.unfilled) else "a lineup change is not measured exactly, since players the engines "
                                               "value may also start there")
        text = {
            "lineup": f"{slots}: no engine fills these slots, so your starters there are kept as they are, and the "
                      "submitted and recommended totals cover the other slots only and are marked partial."
                      + ("" if separable(self.unfilled) else " Players the engines value may also start there, so the "
                         "recommendation is not measured against the best full lineup."),
            "totals": f"{slots}: no engine fills these slots, so lineup totals cover the other starting slots only and "
                      f"are marked partial; {exact}.",
            "waivers": f"{slots}: no engine fills these slots, so roster capacity is unknown and add/drop moves are "
                       "not planned.",
            "bye": f"{slots}: who may fill these slots is not established, so weeks that need them are unconfirmed.",
            "league": f"{slots}: no engine fills these slots; projected lineups and totals leave them out.",
        }[kind]
        return [finding("unfilled_slots", PARTIAL, text, missing_input="an engine for these slots",
                        action="Read lineup totals as covering the other starting slots only.", value=self.unfilled)]

    def legality_finding(self) -> list[dict]:
        """trades.roster_legality (G4): legality is unverified only where the roster count is unknown. Rostered players
        missing from the player data are reported by missing_players_finding; a team without any synced roster has no
        count at all. When every rostered player is stored, the count is exact; only their lineup value is unmodelled."""
        slots = ", ".join(self.unfilled)
        teams, have = self.facts.get("teams") or 0, self.facts.get("teams_with_roster") or 0
        if teams and have < teams:          # named even when missing players are reported too (players_missing)
            return [finding("roster_legality", UNVERIFIED, f"{_plural(teams - have, 'team')} of {teams} have no synced "
                            "roster, so their players cannot be counted and roster legality is unverified for them.",
                            missing_input=f"rosters for {self.wk()}", action="Sync the league again.",
                            value=teams - have)]
        if self.facts.get("players_missing") or not self.unfilled:
            return []
        return [finding("roster_legality", HANDLED, f"Every rostered player is in the player data, so roster counts "
                        f"are exact; who may fill {slots} is not modelled, so players there are not valued.",
                        value=self.unfilled)]

    def missing_players_finding(self, capacity: bool) -> list[dict]:
        n = self.facts.get("players_missing") or 0
        if not n:
            return []
        if capacity:
            return [finding("players_missing", UNVERIFIED, f"{_plural(n, 'rostered player')} not in the player data "
                            "are left out of active roster counts, so an open spot may not exist and roster legality "
                            "is unverified.", missing_input="player data for those players",
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
            families = list(dict.fromkeys(FAMILY[p] for p in used))
            for code in families:
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
                    # the rule ids come from the league rules row (league_rules._abstains_on), never re-derived here
                    rules = row.get("unsupported_rules")
                    out.append(finding(code, UNSUPPORTED, row["detail"],
                                       missing_input=f"{FAMILY_TEXT[code]} projections in this league's scoring",
                                       provider_boundary=boundary,
                                       action="See League rules for the scoring rules the models do not implement.",
                                       value={"unsupported_rules": rules} if rules else None))
            # The missing run is reported for the families the models could project (an unsupported family already
            # says why it has none). With nothing shown at all it is as unsupported as an ESPN week without numbers.
            ready = [c for c in families if (self.support.get(c) or {}).get("status") in (HANDLED, PARTIAL)]
            if shown != "ours" and ready:
                none = shown == "none"
                names = ", ".join(FAMILY_TEXT[c] for c in ready)
                abstained = bool((f.get("independent") or {}).get("available"))
                reason = (f"A verified run of Fantasy Intel's own {names} projections (Ours) is stored for {self.wk()}, "
                          "but every one of its rows abstains, so there are no projections to use." if abstained else
                          f"No verified run of Fantasy Intel's own {names} projections (Ours) in this league's scoring is "
                          f"stored for {self.wk()}" + (", so there are no projections to use." if none else "."))
                out.append(finding("independent_run", UNSUPPORTED if none else UNVERIFIED, reason,
                                   missing_input=f"projections for {self.wk()}", provider_boundary=boundary,
                                   action="Check again after the next sync.", value=ready))
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
        if capacity:
            roster += self.legality_finding()
        # G6 and G7 rest on the league rules rows (roster_restrictions, ir_eligibility), never re-derived here.
        restrictions = self.support.get("roster_restrictions")
        if code == "waivers" and restrictions is not None:
            roster.append(finding("roster_restrictions", restrictions["status"], f"{self.name}'s synced settings carry "
                                  "no roster restrictions, so the waiver planner cannot establish whether a claim needs "
                                  "a drop.", missing_input="roster restrictions",
                                  provider_boundary=f"{self.name} league settings as synced.",
                                  action=f"Check your open roster spots in {self.name} before claiming."))
        ir = self.support.get("ir_eligibility")
        if code in ("waivers", "add_drop") and ir is not None:
            handled = ir["status"] == HANDLED
            roster.append(finding("ir_eligibility", ir["status"], ir["detail"],
                                  missing_input=None if handled else "the league's IR rules for some injury statuses",
                                  action=None if handled else "Check who may use an IR slot in your league settings.",
                                  value=ir.get("undecided_statuses") or None))
        if kind == "kdst":
            extra = {p: self.slots.get(p, 0) for p in ("K", "D/ST") if self.slots.get(p, 0) > 1}
            if extra:
                # kdst_streaming.plan_position: `slot_coverage` on each such position (issue #253 G9)
                roster.append(finding("streaming_slots", PARTIAL, f"This league starts "
                                      f"{', '.join(f'{n} {p}' for p, n in extra.items())}; the plan streams one of "
                                      "those slots, the one your weakest player there holds each week, and keeps your "
                                      "best players in the others, so it is not a plan for every slot.",
                                      missing_input="a plan for every K and D/ST slot", value=extra))
        if kind != "bye":
            scoring += self.scoring_findings(positions)
        if code == "waivers":
            refs = [c for c in ("ppr", "te_premium") if c in self.support]
            scoring.append(finding("player_value_basis", PARTIAL, "Waiver scores rank players by recent usage "
                                   "(expected points, full PPR) as a percentile within each position, not by this "
                                   "league's scoring.", missing_input="usage values in this league's scoring",
                                   action="Compare the projected points shown beside each claim.", value=refs or None))
        # Dual eligibility is supported by lineup assignment, but position leaderboards still classify by
        # primary position. This finding is emitted only when current synced rosters contain provider evidence.
        dual = self.facts.get("active_dual_position_players") or 0
        if dual and code == "rankings":
            feature.append(finding(
                "dual_position_rankings", PARTIAL,
                f"{_plural(dual, 'active rostered player')} may start at a provider-listed second position, "
                "but player ranks and position filters use their primary position only.",
                missing_input="dual-position-aware player ranks",
                action="Use the lineup or trade evaluator for their roster fit, not the primary-position rank alone.",
                value=dual,
            ))
        # feature
        if kind == "lineup" and self.unfilled:
            n = self.facts.get("starters_in_unfilled_slots") or 0
            feature.append(finding("gain_unfilled_slots", HANDLED,
                                   f"Starters in {', '.join(self.unfilled)} ({n} across the league) stay where they "
                                   "are in both lineups, so the projected gain compares the other slots exactly.",
                                   value=n))
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
            feature.append(finding("missing_value", PARTIAL, "A player without usage data has no value in the waiver "
                                   "lineup math: lineup gains, drop costs and needs that depend on him are marked "
                                   "partial, never counted as 0. Flex and superflex choices compare position "
                                   "percentiles, not points.", missing_input="usage data for every player"))
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
