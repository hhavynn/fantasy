"""Representative Trade Discovery 2.0 scenarios on synthetic leagues, printed as markdown.

Run from backend:  uv run python scripts/trade_scenarios.py > ../docs/trade_discovery_dual_value_scenarios.md

Every league is built in memory from invented players (tests/synthetic_v3 helpers). Every number printed comes
from the real functions: trades.evaluate_trade through trade_discovery._side_detail, then
trade_dual_value.for_evaluation, the same call the manual analyzer and Trade Discovery make. Nothing is typed by
hand, nothing is written to a database, nothing is an acceptance estimate.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from app.analytics import trade_dual_value as tdv  # noqa: E402
from app.analytics.trade_alternatives import FreeAgencyBoard  # noqa: E402
from app.analytics.trade_discovery import _side_detail  # noqa: E402
from app.analytics.trades import evaluate_trade  # noqa: E402
from synthetic_v3 import P, WEEKS, filler, league, market_league  # noqa: E402

REGEN = "cd fantasy-intel/backend && uv run python scripts/trade_scenarios.py > ../docs/trade_discovery_dual_value_scenarios.md"


def flat(v):
    return {w: float(v) for w in WEEKS}


def add_free_agent(st, pid, pos, pts):
    """A confirmed (ESPN FREEAGENT) free agent: only those count as an immediate alternative."""
    st.free_agents = [*st.free_agents, P(pid, pos)]
    st.projections[pid] = flat(pts)
    st.free_agent_status = {**st.free_agent_status, pid: "FREEAGENT"}
    return st


# ----------------------------------------------------------------------------------- leagues --

def mutual_swap():
    return market_league()


def lopsided():
    return market_league()


def fair_no_gain():
    st = market_league()                                   # Team 2 is stacked at WR/RB: a WR 13 cannot start there
    st.projections[205], st.projections[206] = flat(20), flat(19)
    return st


def consolidation():
    t1 = [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14), (104, "WR", 16), (105, "WR", 14), (106, "WR", 13),
          (107, "TE", 7), (108, "K", 8), (109, "D/ST", 7), (110, "RB", 3)]
    t2 = [(201, "QB", 18), (202, "RB", 13), (203, "RB", 12), (204, "WR", 19, {"name": "Ember"}), (205, "WR", 17),
          (206, "TE", 6), (207, "RB", 11), (208, "K", 7), (209, "D/ST", 6)]
    fas = [(901, "QB", 12), (902, "RB", 5), (903, "WR", 6), (904, "TE", 4), (905, "K", 7), (906, "D/ST", 6)]
    return league({1: t1, 2: t2, 3: filler(3, 300)}, fas, capacity=9)


def playoff_focus():
    bye = {10: 12, 11: 12, 12: 12, 13: 0}                  # Rusher is on a bye in the playoff week
    late = {10: 11, 11: 11, 12: 11, 13: 12}
    t1 = [(101, "QB", 20), (102, "RB", 15), (103, "RB", bye, {"name": "Rusher"}), (104, "WR", 16), (105, "WR", 15),
          (106, "TE", 7), (109, "WR", 13), (107, "WR", 11, {"name": "Wideout"}), (108, "K", 8), (110, "D/ST", 7)]
    t2 = [(201, "QB", 18), (202, "RB", 14), (203, "RB", 13), (204, "RB", late, {"name": "Closer"}), (205, "WR", 9),
          (206, "WR", 8), (207, "TE", 6), (208, "K", 7), (209, "D/ST", 6), (210, "RB", 12)]
    fas = [(901, "QB", 12), (902, "RB", 5), (903, "WR", 6), (904, "TE", 4), (905, "K", 7), (906, "D/ST", 6)]
    return league({1: t1, 2: t2, 3: filler(3, 300)}, fas, capacity=10)


def free_agent_covered():
    st = market_league()
    add_free_agent(st, 907, "TE", 11)                       # a free-agent TE almost as good as Bowers (13)
    return add_free_agent(st, 908, "WR", 12)                # and a free-agent WR that sets the WR replacement level


SCENARIOS = [
    ("1. A sensible mutual positional-need swap",
     "Team 1 has a surplus at WR and is thin at RB; Team 3 is the reverse. A WR for an RB helps both starting lineups "
     "and the asset packages are comparable.", mutual_swap, 3, [106], [302], "ros"),
    ("2. Both lineups improve, but the deal is lopsided",
     "Both starting lineups gain, yet Team 2 surrenders two strong assets (a starting RB and a TE who barely starts "
     "for it) for one WR. The numbers are positive for both; the asset check says it is unlikely to be mutually "
     "attractive.", lopsided, 2, [105], [204, 209], "ros"),
    ("3. Asset-fair, but one lineup does not improve",
     "The packages are close in points above replacement, and Team 1 starts the TE it receives. Team 2 is stacked "
     "at WR and RB, so the WR it receives starts nowhere and its lineup does not move.",
     fair_no_gain, 2, [108], [209], "ros"),
    ("4. A 2-for-1 consolidation that forces a drop",
     "Team 1 turns two starting WRs into one star, which does not pay: both lineups lose. Team 2 receives two players "
     "for one on a full roster and must drop its cheapest lineup piece, a bench RB that costs no lineup points but is a "
     "starter-caliber asset, so the depth check fails even though the lineup cost is zero.",
     consolidation, 2, [105, 106], [204], "ros"),
    ("5. A playoff-focused trade",
     "Team 1's RB2 is on a bye in the playoff week. Closer sits behind him all regular season (delta 0) and "
     "starts in the playoffs. Assessed with the playoff phase, as the playoff lens does.",
     playoff_focus, 2, [107], [204], "playoff"),
    ("6. A deal whose gain is mostly available from free agency",
     "Team 1 swaps a WR for Bowers, who would start at TE for it, and Team 2 starts the WR. But a free-agent TE (11) is "
     "nearly as good as Bowers (13), so most of the gain needs no trade.", free_agent_covered, 2, [108], [209], "ros"),
]


# ------------------------------------------------------------------------------------ report --

def money(x):
    return "unknown" if x is None else f"{x:+.1f}"


def pts(x):
    return "unknown" if x is None else f"{x:.1f}"


def names(state):
    return {p.espn_player_id: f"{p.name} ({p.position}, #{p.espn_player_id})" for ps in state.rosters.values() for p in ps}


def players(ids, nm):
    return ", ".join(nm[i] for i in ids)


def side_line(lin):
    d, b, a = lin["delta"], lin["before"], lin["after"]
    return (f"{lin['label']} ({lin['team_name']}): ROS {pts(b['ros'])} -> {pts(a['ros'])} (delta {money(d['ros'])}); "
            f"regular season {money(d['regular'])}; playoffs {money(d['playoff'])}; weeks improved/worsened/unchanged "
            f"{lin['weeks_improved']}/{lin['weeks_worsened']}/{lin['weeks_unchanged']}")


def run(title, blurb, build, opp, sends, receives, phase):
    st = build()
    res = evaluate_trade(st, 1, opp, sends, receives, scenarios=False)
    window = st.window()
    fa = FreeAgencyBoard(st, window) if st.free_agents else None
    dv = tdv.for_evaluation(st, window, 1, sends, receives, _side_detail(res["team_a"]), _side_detail(res["team_b"]),
                            phase=phase, fa_board=fa, labels=("You", st.team_names[opp]))
    nm, asset, lin, pl = names(st), dv["asset"], dv["lineup"], dv["plausibility"]
    out = [f"## {title}", "", blurb, ""]
    out += [f"- **You send:** {players(sends, nm)}", f"- **You receive:** {players(receives, nm)} (from {st.team_names[opp]})",
            f"- **{st.team_names[opp]} sends:** {players(receives, nm)}; **receives:** {players(sends, nm)}",
            f"- **Phase assessed:** {phase}; weeks {window.weeks[0]}-{window.weeks[-1]} "
            f"(regular season through {window.last_regular}, playoffs after)", ""]
    out += ["**Asset value** (ESPN points above replacement, all remaining weeks)", ""]
    out += [f"- You send {pts(asset['you_send_par'])}; they send {pts(asset['they_send_par'])}; "
            f"ratio {asset['ratio'] if asset['ratio'] is not None else 'unknown'}; label `{asset['label']}` "
            f"({asset['text']}); imbalance {asset['imbalance']}" + (f", favours {asset['favours']}" if asset["favours"] else "")]
    for row in asset["you_send"] + asset["you_receive"]:
        out.append(f"  - {row['name'] or row['espn_player_id']} ({row['position']}) {row['from_side']} -> {row['to_side']}: "
                   f"PAR {pts(row['par'])}, starter-caliber {row['starter_caliber']}, starts {row['starts_before']} of "
                   f"{row['weeks']} weeks before / {row['starts_after']} after, bench asset {row['bench_asset']}")
    for who in ("you", "them"):
        dep = asset["depth"][who]
        extra = "".join(f"; forced drop {d['name']} (PAR {pts(d['par'])}, starter-caliber {d['starter_caliber']}, lineup cost "
                        f"{money(dep['forced_drop_lineup_cost'])})" for d in dep["forced_drops"])
        out.append(f"  - depth, {lin[who]['label']}: {dep['kind']} (net {dep['net_players']:+d} players){extra}")
    out += ["", "**Lineup benefit** (starters only; bench points never count)", ""]
    out += [f"- {side_line(lin['you'])}", f"- {side_line(lin['them'])}",
            f"- Key change, {lin['you']['label']}: {lin['you']['key_change']}",
            f"- Key change, {lin['them']['label']}: {lin['them']['key_change']}", ""]
    out += ["**Checks**", "", "| criterion | status | reason |", "|---|---|---|"]
    out += [f"| `{c['criterion']}` | {c['status']} | {c['text']} |" for c in pl["checks"]]
    out += ["", f"**Verdict:** `{pl['verdict']}`. {pl['verdict_text']}", "",
            f"**Main concern:** {pl['main_concern'] or 'none'}", "",
            f"Rank axes (never summed): lineup gain of the smaller side {money(dv['rank_axes']['lineup_min_side'])}, "
            f"asset ratio {dv['rank_axes']['asset_ratio']}, certainty {dv['rank_axes']['certainty']}.", ""]
    return out


def main():
    print("# Trade Discovery 2.0: representative scenarios\n")
    print("**Synthetic.** Every league, team and player below is invented; no real league data is used and nothing "
          "here says anything about a real roster. Every number is produced by the real functions "
          "(`trades.evaluate_trade` -> `trade_dual_value.for_evaluation`); none is typed by hand.\n")
    print("Regenerate with:\n")
    print(f"```\n{REGEN}\n```\n")
    print("See `trade_discovery_dual_value.md` for the contract. Nothing here estimates whether a manager would "
          "accept a trade.\n")
    for sc in SCENARIOS:
        print("\n".join(run(*sc)))


if __name__ == "__main__":
    main()
