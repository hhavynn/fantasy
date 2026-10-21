"""Sleeper scoring_settings -> ESPN-shaped items every scoring resolver reads (app/sleeper/scoring.py)."""

import json
from pathlib import Path

import pytest

from app.analytics import dst_store
from app.analytics.kicker_scoring import resolve_kicker_scoring
from app.analytics.league_scoring import UnsupportedScoring, points, points_sql, resolve_skill_scoring
from app.analytics.rescore import rescore_skill_rows
from app.sleeper.scoring import to_scoring_items

from synthetic_forecast import SCORING_ITEMS

FIXTURES = Path(__file__).resolve().parents[1] / "app" / "leagues" / "providers" / "fixture_data" / "sleeper" / "leagues"

# Sleeper's default redraft rules, half-PPR, plus a TE premium: what most Sleeper leagues use
DEFAULTS = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "bonus_rec_te": 0.5, "fum_lost": -2, "fum_rec_td": 6,
    "st_td": 6, "st_ff": 1, "st_fum_rec": 1, "fum": 0, "bonus_pass_yd_300": 0, "idp_tkl": 0,
    "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50p": 5, "fgmiss": -1, "xpm": 1, "xpmiss": -1,
    "sack": 1, "int": 2, "fum_rec": 2, "ff": 1, "safe": 2, "blk_kick": 2, "def_td": 6, "def_st_td": 6,
    "def_st_ff": 1, "def_st_fum_rec": 1, "pts_allow_0": 10, "pts_allow_1_6": 7, "pts_allow_7_13": 4,
    "pts_allow_14_20": 1, "pts_allow_21_27": 0, "pts_allow_28_34": -1, "pts_allow_35p": -4,
}


def test_sleepers_default_rules_are_supported_by_every_family():
    items, unsupported, notes = to_scoring_items(DEFAULTS)
    assert unsupported == []
    skill = resolve_skill_scoring(items)
    assert skill.supported and skill.weights["receptions"] == 0.5
    assert skill.position_weights == {"TE": {"receptions": 0.5}}
    assert skill.weights["special_teams_tds"] == 6
    assert resolve_kicker_scoring(items).supported
    weights, why = dst_store.scoring_weights(items)
    assert why is None and weights[1106] == 1 and weights[1089] == 10 and 1093 not in weights   # 21-27 is worth 0
    assert any("def_st_fum_rec" in n for n in notes) and not any("def_st_ff" in n for n in notes)
    assert any(n.startswith("st_ff") for n in notes)


def test_zero_valued_rules_are_ignored_and_unknown_rules_make_their_family_abstain():
    items, unsupported, _ = to_scoring_items({**DEFAULTS, "bonus_pass_yd_300": 1, "fgm_yds": 0.1, "tkl_loss": 1})
    assert sorted(unsupported) == ["bonus_pass_yd_300", "fgm_yds", "tkl_loss"]
    assert not resolve_skill_scoring(items).supported
    assert not resolve_kicker_scoring(items).supported
    assert dst_store.scoring_weights(items)[0] is None


def test_the_fixture_leagues_translate_and_an_unimplemented_bonus_is_stated():
    a = json.loads((FIXTURES / "1180000000000000001" / "league.json").read_text())["scoring_settings"]
    b = json.loads((FIXTURES / "1180000000000000002" / "league.json").read_text())["scoring_settings"]
    items, unsupported, _ = to_scoring_items(a)
    assert unsupported == [] and resolve_skill_scoring(items).supported
    items, unsupported, _ = to_scoring_items(b)
    assert unsupported == ["bonus_pass_yd_300"]
    skill = resolve_skill_scoring(items)
    assert not skill.supported and skill.position_weights == {"TE": {"receptions": 0.5}}


def test_a_te_premium_scores_tight_ends_only_and_changes_the_fingerprint():
    items, _, _ = to_scoring_items(DEFAULTS)
    te = resolve_skill_scoring(items)
    plain = resolve_skill_scoring(to_scoring_items({k: v for k, v in DEFAULTS.items() if k != "bonus_rec_te"})[0])
    line = {c: 0.0 for c in te.weights} | {"receptions": 5.0, "receiving_yards": 50.0}
    assert points(line, te, "TE") == points(line, plain, "TE") + 2.5
    assert points(line, te, "WR") == points(line, plain, "WR")
    assert te.fingerprint != plain.fingerprint
    with pytest.raises(UnsupportedScoring):
        points_sql(te, "s")
    assert "CASE WHEN s.position = 'TE'" in points_sql(te, "s", position_col="position")


def test_espn_fingerprints_are_unchanged():
    """The reference scoring (also Los Subies') hashes exactly as before position weights existed."""
    assert resolve_skill_scoring(SCORING_ITEMS).fingerprint == "e165b59686110c7d"


def test_a_te_premium_rescores_tight_ends_and_leaves_receivers_alone():
    ref = resolve_skill_scoring(SCORING_ITEMS)
    te_items = [*SCORING_ITEMS, {"statId": 53, "points": 0.0, "positionPoints": {"TE": 0.5}}]
    target = resolve_skill_scoring(te_items)
    mix = {"g1": {c: 0.0 for c in ref.weights} | {"receptions": 5.0, "receiving_yards": 50.0}}
    row = {"run_id": "r", "espn_player_id": 1, "gsis_id": "g1", "coverage_status": "projected", "projected_points": 10.0,
           "interval_low": 2.0, "interval_high": 18.0, "p_play": 1.0, "expected_points": 10.0, "flags": "[]",
           "explanation": "[]"}
    (te,) = rescore_skill_rows([{**row, "position": "TE"}], mix, ref, target)
    (wr,) = rescore_skill_rows([{**row, "position": "WR"}], mix, ref, target)
    assert te["projected_points"] == round(10.0 * 12.5 / 10.0, 2) and wr["projected_points"] == 10.0
