"""League-agnostic trade market (app/analytics/trade_market.py) on controlled synthetic usage.

Values are invented test inputs, not real NFL outcomes, chosen so the expected buy-low / sell-high
calls are known in advance."""

from __future__ import annotations

import pytest

from app.analytics import trade_market as tm
from app.analytics.trade_assets import BALANCED_RATIO, SIGNIFICANT_RATIO

TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
WEEKS = [1, 2, 3, 4, 5]

# Base role per slot: (offense_pct, target_share, air_yards_share, backfield_share, rz_carries, rz_targets,
#                      rec_yards, rush_yards, tds_per_game)
BASE = {
    "RB1": (0.65, 0.10, 0.03, 0.70, 3, 0, 20, 60, 0.4),
    "RB2": (0.35, 0.05, 0.01, 0.30, 1, 0, 8, 25, 0.2),
    "WR1": (0.90, 0.24, 0.33, None, 0, 2, 75, 0, 0.5),
    "WR2": (0.85, 0.18, 0.25, None, 0, 1, 55, 0, 0.3),
    "WR3": (0.70, 0.12, 0.15, None, 0, 1, 35, 0, 0.2),
    "TE1": (0.80, 0.15, 0.12, None, 0, 2, 40, 0, 0.3),
}
POS = {"RB1": "RB", "RB2": "RB", "WR1": "WR", "WR2": "WR", "WR3": "WR", "TE1": "TE"}


def _row(gsis, team, slot, week, scale=1.0, **over):
    snap, ts, ays, bf, rzc, rzt, recy, rushy, tdpg = BASE[slot]
    # Spread values across teams so ranks are strict, deterministic and not all tied.
    k = scale * (1 + 0.02 * TEAMS.index(team))
    targets = round(ts * k * 34)
    carries = round((bf or 0) * k * 22)
    tds = 1 if (week * tdpg) % 1 + tdpg >= 1 else 0
    r = {
        "season": 2026, "week": week, "gsis_id": gsis, "team": team, "opponent": None, "position": POS[slot],
        "offense_snaps": round(snap * 65), "offense_pct": min(1.0, snap * k), "targets": targets,
        "receptions": round(targets * 0.65), "receiving_yards": recy * k, "receiving_tds": tds if POS[slot] != "RB" else 0,
        "target_share": ts * k, "air_yards_share": ays * k, "wopr": 1.5 * ts * k + 0.7 * ays * k,
        "carries": carries, "rushing_yards": rushy * k, "rushing_tds": tds if POS[slot] == "RB" else 0,
        "rush_attempts": carries, "designed_rushes": carries,
        "backfield_rush_share": (bf * k) if bf is not None else None,
        "explosive_runs": round(carries * 0.1), "rz_carries": round(rzc * k), "rz_targets": round(rzt * k),
        "opportunities": targets + carries, "team_rz_plays": 12, "fantasy_points_ppr": None, "xfp": None,
    }
    r.update(over)
    return r


def make_inputs(overrides=None, injuries=(), roster_status=()):
    """8 teams x 6 slots x 5 weeks. `overrides[(team, slot)] = fn(week, row) -> row | None`."""
    overrides = overrides or {}
    usage, names = [], {}
    for team in TEAMS:
        for slot in BASE:
            gsis = f"{team}-{slot}"
            names[gsis] = f"{team} {slot}"
            for w in WEEKS:
                row = _row(gsis, team, slot, w)
                fn = overrides.get((team, slot))
                row = fn(w, row) if fn else row
                if row is not None:
                    usage.append(row)
    team_week = [{"season": 2026, "week": w, "team": t, "team_rz_carries": 6, "team_rz_targets": 6,
                  "neutral_pass_rate": 0.5 + 0.01 * i, "neutral_plays": 40} for w in WEEKS for i, t in enumerate(TEAMS)]
    lines = []
    for w in range(1, 10):
        for i, t in enumerate(TEAMS):
            opp = TEAMS[(i + w) % len(TEAMS)] if (i + w) % len(TEAMS) != i else TEAMS[(i + 1) % len(TEAMS)]
            lines.append({"season": 2026, "week": w, "team": t, "opponent": opp,
                          "implied_total": 20 + i if w <= 6 else None})
    defense = [{"defteam": t, "games": 5, "ranks": {m: 1 + 4 * i for m in (
        "pass_epa_allowed", "rush_epa_allowed", "rb_target_share_allowed", "te_target_share_allowed")}}
        for i, t in enumerate(TEAMS)]
    return tm.MarketInputs(season=2026, usage=usage, team_week=team_week, game_lines=lines, defense=defense,
                           injuries=list(injuries), roster_status=list(roster_status), names=names)


# Buy-low: AAA WR2 has a WR1-sized role (target, air-yard and red-zone share) but a WR3 box score.
def _buy_wr(w, r):
    r.update(target_share=0.30, air_yards_share=0.40, wopr=1.5 * 0.30 + 0.7 * 0.40, rz_targets=4,
             targets=10, receiving_yards=30.0, receiving_tds=0, offense_pct=0.95, opportunities=10)
    return r


# Sell-high: BBB WR3 has a small role but a huge box score from touchdowns and long plays.
def _sell_wr(w, r):
    r.update(receiving_yards=120.0, receiving_tds=2, rz_targets=0)
    return r


# Elite and hot: CCC WR1 has the biggest role and a big box score. Never a sell.
def _elite_wr(w, r):
    r.update(target_share=0.33, air_yards_share=0.45, wopr=1.5 * 0.33 + 0.7 * 0.45, rz_targets=5,
             receiving_yards=140.0, receiving_tds=2, offense_pct=0.97)
    return r


# Partial game: DDD RB1 leaves week 5 after 10% of snaps.
def _partial_rb(w, r):
    if w == 5:
        r.update(offense_pct=0.10, backfield_rush_share=0.10, carries=2, designed_rushes=2, rushing_yards=4.0,
                 targets=0, opportunities=2, rz_carries=0)
    return r


OVERRIDES = {("AAA", "WR2"): _buy_wr, ("BBB", "WR3"): _sell_wr, ("CCC", "WR1"): _elite_wr,
             ("DDD", "RB1"): _partial_rb}


@pytest.fixture
def market():
    return tm.build_market(make_inputs(OVERRIDES))


def _rows(inputs, gsis):
    return [r for r in inputs.usage if r["gsis_id"] == gsis]


def test_derive_metric_definitions():
    rows = [
        {"week": 1, "offense_snaps": 40, "offense_pct": 0.8, "target_share": 0.2, "rz_carries": 1, "rz_targets": 2,
         "team_rz_plays": 10, "rushing_tds": 1, "receiving_tds": 0, "rushing_yards": 50, "receiving_yards": 20,
         "opportunities": 12, "targets": 4},
        {"week": 2, "offense_snaps": 30, "offense_pct": 0.6, "target_share": 0.1, "rz_carries": 0, "rz_targets": 1,
         "team_rz_plays": 5, "rushing_tds": 0, "receiving_tds": 1, "rushing_yards": 10, "receiving_yards": 40,
         "opportunities": 8, "targets": 6},
    ]
    assert tm.derive_metric(rows, "offense_pct") == pytest.approx(0.7)            # mean of game shares
    assert tm.derive_metric(rows, "rz_opp_share") == pytest.approx(4 / 15)         # ratio of sums
    assert tm.derive_metric(rows, "touchdowns") == 2
    assert tm.derive_metric(rows, "scrimmage_yards") == 120
    assert tm.derive_metric(rows, "yards_pg") == 60
    assert tm.derive_metric(rows, "yards_per_target") == pytest.approx(6.0)
    assert tm.derive_metric(rows, "target_share", [2]) == pytest.approx(0.1)
    assert tm.derive_metric(rows, "target_share", [9]) is None
    with pytest.raises(KeyError):
        tm.derive_metric(rows, "fantasy_points_ppr")      # points are not a market metric


def test_partial_games_are_excluded_from_the_role():
    rows = [{"week": w, "offense_snaps": 1, "offense_pct": p} for w, p in [(1, 0.7), (2, 0.72), (3, 0.1), (4, 0.68)]]
    assert [r["week"] for r in tm.full_games(rows)] == [1, 2, 4]
    # A part-time player's low snap games are his role, not partial games.
    low = [{"week": w, "offense_snaps": 1, "offense_pct": p} for w, p in [(1, 0.3), (2, 0.1), (3, 0.35)]]
    assert len(tm.full_games(low)) == 3


def test_partial_last_game_is_flagged_and_blocks_a_buy(market):
    p = market["DDD-RB1"]
    assert p.partial_weeks == (5,)
    assert "LEFT_LAST_GAME_EARLY" in p.flags
    assert "ROLE_DOWN" not in p.flags          # one partial game is not a lost role
    assert tm.buy_low_score(p) is None


def test_buy_low_and_sell_high_calls(market):
    buy = market["AAA-WR2"]
    assert buy.usage_value > buy.market_value
    assert [c["gsis_id"] for c in tm.candidates(market, "buy")][0] == "AAA-WR2"
    sells = [c["gsis_id"] for c in tm.candidates(market, "sell")]
    assert sells[0] == "BBB-WR3"
    assert "TD_HEAVY" in market["BBB-WR3"].flags
    assert "CCC-WR1" not in sells                              # elite role + hot production = hold
    assert market["CCC-WR1"].usage_rank == 1


def test_usage_value_never_reads_fantasy_points():
    base = tm.build_market(make_inputs(OVERRIDES))
    inputs = make_inputs(OVERRIDES)
    for r in inputs.usage:
        r["fantasy_points_ppr"], r["xfp"], r["league_points"] = 99.0, 99.0, 99.0
    changed = tm.build_market(inputs)
    assert {g: (p.usage_value, p.market_value) for g, p in base.items()} == \
        {g: (p.usage_value, p.market_value) for g, p in changed.items()}


def test_injured_players_are_discounted_and_never_offered():
    inputs = make_inputs(OVERRIDES, injuries=[{"gsis_id": "EEE-WR1", "report_status": "Out"}])
    m = tm.build_market(inputs)
    assert not m["EEE-WR1"].available
    for g in m:
        o = tm.generate_offer(m, g, "buy")
        for off in [o["opening"], *o["alternatives"]]:
            if off:
                assert "EEE-WR1" not in off["you_give"] + off["you_get"] or g == "EEE-WR1"


def test_vacated_teammate_role_is_context_and_risk():
    inputs = make_inputs(OVERRIDES, roster_status=[{"gsis_id": "FFF-RB1", "status": "RES"}])
    m = tm.build_market(inputs)
    p = m["FFF-RB2"]
    assert "VACATED_ROLE" in p.flags
    assert p.vacated[0]["gsis_id"] == "FFF-RB1"
    assert any(e.gsis_id == "FFF-RB1" and e.metric == "backfield_rush_share" for e in p.evidence)


@pytest.mark.parametrize("ratio,label", [
    (0.91, None), (0.92, "opening offer"), (0.99, "opening offer"), (1.0, "fair range"),
    (1.09, "fair range"), (1.10, "likely high price"), (1.22, "likely high price"), (1.23, None)])
def test_fairness_bands(ratio, label):
    assert tm.fairness(ratio) == label


def test_package_value_discounts_depth():
    assert tm.package_value([40, 60]) == 60 + tm.DEPTH_DISCOUNT * 40
    assert tm.package_value([]) == 0


def _check_offer(m, target, mode, off):
    give, get = [m[g] for g in off["you_give"]], [m[g] for g in off["you_get"]]
    they_get, they_give = (give, get)
    ratio = tm.package_value([p.market_value for p in they_get]) / tm.package_value([p.market_value for p in they_give])
    assert ratio == pytest.approx(off["market_ratio"], abs=1e-3)
    assert tm.fairness(ratio) == off["fairness"]                        # never lopsided either way
    mine_in = tm.package_value([p.usage_value for p in get])
    mine_out = tm.package_value([p.usage_value for p in give])
    assert mine_in > mine_out                                           # we win on usage
    assert (target in off["you_get"]) == (mode == "buy")
    pieces = give if mode == "buy" else get
    assert target not in [p.gsis_id for p in pieces]
    assert all(p.available for p in give + get)
    if len(pieces) == 2:
        t = m[target]
        assert max(p.market_value for p in pieces) >= BALANCED_RATIO * t.market_value    # starter-caliber anchor
        assert min(p.market_value for p in pieces) >= SIGNIFICANT_RATIO * t.market_value  # no bench scraps
        assert t.market_value >= tm.MIN_2FOR1_MARKET
    assert off["incentive"] and off["position_check"] and off["balance_check"]
    assert "probab" not in str(off).lower()


def test_offers_respect_every_guard(market):
    inputs = make_inputs(OVERRIDES)
    seen = 0
    for kind in ("buy", "sell"):
        for c in tm.candidates(market, kind, limit=20):
            o = tm.generate_offer(market, c["gsis_id"], kind)
            offers = [x for x in [o["opening"], *o["alternatives"]] if x]
            assert len(o["alternatives"]) <= 2
            for off in offers:
                seen += 1
                _check_offer(market, c["gsis_id"], kind, off)
                assert off["shape"] in ("1-for-1", "2-for-1")
                for e in off["evidence"]:                                  # every number re-derives
                    v = tm.derive_metric(_rows(inputs, e["gsis_id"]), e["metric"], e["weeks"])
                    assert e["value"] == (None if v is None else pytest.approx(round(v, 4)))
    assert seen > 0


def test_sell_mode_never_returns_an_elite_role(market):
    elite = {g for g, p in market.items() if p.usage_rank and p.usage_rank <= tm.ELITE_SHARE * tm.STARTERS[p.position]}
    for c in tm.candidates(market, "sell", limit=20):
        o = tm.generate_offer(market, c["gsis_id"], "sell")
        for off in [o["opening"], *o["alternatives"]]:
            if off:
                assert not set(off["you_get"]) & elite


def test_generate_offer_rejects_unknown_mode(market):
    with pytest.raises(ValueError):
        tm.generate_offer(market, "AAA-WR2", "steal")


def test_deterministic(market):
    again = tm.build_market(make_inputs(OVERRIDES))
    assert tm.generate_offer(market, "AAA-WR2", "buy") == tm.generate_offer(again, "AAA-WR2", "buy")


def test_adp_ranks_replace_the_box_score_half():
    inputs = make_inputs(OVERRIDES)
    wrs = sorted(g for g in {r["gsis_id"] for r in inputs.usage} if "WR" in g)
    adp = {g: i for i, g in enumerate(reversed(wrs), start=1)}
    m = tm.build_market(inputs, adp_ranks=adp)
    assert m[wrs[-1]].market_rank == 1


def test_sleeper_market_ranks():
    players = {"1": {"position": "WR", "search_rank": 30}, "2": {"position": "WR", "search_rank": 5},
               "3": {"position": "RB", "search_rank": 7}, "4": {"position": "WR", "search_rank": 9999999},
               "5": {"position": "QB", "search_rank": 1}, "6": {"position": "TE", "search_rank": 12}}
    ids = {"1": "g1", "2": "g2", "3": "g3", "4": "g4", "5": "g5"}
    assert tm.sleeper_market_ranks(players, ids) == {"g2": 1, "g1": 2, "g3": 1}


def test_loader_reads_only_nflverse_tables(con):
    """The DB loader works on a database with no ESPN league synced at all."""
    inputs = tm.load_market_inputs(con, 2026)
    assert inputs.usage == [] and tm.build_market(inputs) == {}


def test_only_the_current_week_report_sets_availability():
    """Contract section 8: last week's Out is risk context, not unavailability."""
    inputs = make_inputs(OVERRIDES, injuries=[{"gsis_id": "EEE-WR1", "week": 5, "report_status": "Out"},
                                              {"gsis_id": "GGG-WR1", "week": 6, "report_status": "Doubtful"}])
    m = tm.build_market(inputs)
    assert m["EEE-WR1"].available and "LISTED_OUT_LAST_WEEK" in m["EEE-WR1"].flags
    assert not m["GGG-WR1"].available and m["GGG-WR1"].status == "Doubtful"
