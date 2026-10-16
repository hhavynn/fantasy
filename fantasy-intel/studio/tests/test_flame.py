"""Flame / praise selection (stages/flame.py): who gets roasted, who gets hyped, and that every number re-derives."""

from __future__ import annotations

import copy

from studio.core import canonical_json, sha256_bytes
from studio.stages import flame
from studio.stages.facts import evaluate_claim
from studio.stages.script import BANNED, DIGIT, NUMBER_WORDS, PLACEHOLDER

from conftest import make_snapshot

QB_TEAMS = ["PHI", "DAL", "NYJ", "SEA", "BUF", "MIA", "KC", "LV", "DEN", "LAC"]
NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet"]


def _qb(gid: str, team: str, week: int, att: int, air: float, epa: float, yds: int) -> dict:
    return {"week": week, "gsis_id": gid, "team": team, "opponent": "XXX", "attempts": att, "completions": att // 2,
            "passing_yards": yds, "passing_air_yards": air, "passing_yards_after_catch": yds // 2,
            "passing_epa": epa, "sacks_suffered": 1}


def snapshot_with_qbs(latest_air: float = 40.0, latest_att: int = 30, latest_epa: float = -14.0,
                      latest_yds: int = 120) -> dict:
    """Ten starters with ordinary games in weeks 1-5; QB00's week 5 is the outlier under test. QB99 is a backup
    who only threw in week 5."""
    snap = make_snapshot()
    rows = []
    for i, team in enumerate(QB_TEAMS):
        gid = f"QB{i:02d}"
        for week in range(1, 6):
            air = 220 + 9 * ((i * 7 + week * 3) % 11)
            epa = -3 + ((i * 5 + week) % 9)
            rows.append(_qb(gid, team, week, 32, air, float(epa), 230 + 3 * ((i + week) % 7)))
    rows = [r for r in rows if not (r["gsis_id"] == "QB00" and r["week"] == 5)]
    rows.append(_qb("QB00", "PHI", 5, latest_att, latest_air, latest_epa, latest_yds))
    rows.append(_qb("QB99", "DAL", 5, 22, 30.0, -12.0, 90))
    snap["tables"]["qb_week_passing"] = rows
    snap["tables"]["nfl_players"] += [{"gsis_id": f"QB{i:02d}", "display_name": f"{NAMES[i]} Passer", "position": "QB"}
                                      for i in range(10)] + [{"gsis_id": "QB99", "display_name": "Backup Guy",
                                                              "position": "QB"}]
    snap["content_sha256"] = sha256_bytes(canonical_json(snap["tables"]).encode())
    return snap


def test_a_historically_short_passing_game_is_the_flame():
    snap = snapshot_with_qbs()
    flames = [c for c in flame.candidates(snap) if c["kind"] == "flame"]
    assert flames and flames[0]["gsis_id"] == "QB00"
    lead = flames[0]["lead"]
    assert lead["rank"] == 1 and lead["z_pos"] > 0 and lead["z_own"] > 0 and lead["season_extreme"]
    assert flames[0]["slug"] == "flame-alpha-passer"


def test_a_backup_without_an_established_role_is_never_roasted():
    # QB99's only game is bad, but he has no earlier games: no baseline of his own, no flame
    snap = snapshot_with_qbs()
    assert not any(c["gsis_id"] == "QB99" for c in flame.candidates(snap))


def test_the_volume_floor_keeps_out_a_game_he_barely_played():
    # 12 attempts is below the 20-attempt floor (an early exit, a mop-up): no case, and the game is not
    # counted in the position's distribution either
    snap = snapshot_with_qbs(latest_att=12)
    assert not any(c["gsis_id"] == "QB00" for c in flame.candidates(snap))
    dist = flame.distribution(snap, "air_yards", "QB", 5)
    assert not any(g == "QB00" and w == 5 for g, w, _ in dist)


def test_an_ordinary_game_is_neither():
    snap = snapshot_with_qbs(latest_air=250.0, latest_epa=1.0, latest_yds=232)
    assert not any(c["gsis_id"] == "QB00" for c in flame.candidates(snap))


def test_a_monster_game_is_the_praise_and_the_pick_spans_two_teams():
    snap = snapshot_with_qbs(latest_air=520.0, latest_epa=25.0, latest_yds=400)
    praises = [c for c in flame.candidates(snap) if c["kind"] == "praise"]
    assert praises and praises[0]["gsis_id"] == "QB00"
    picks = flame.pick(snap)
    assert [p["kind"] for p in picks] in (["praise"], ["flame", "praise"])
    assert len({p["team"] for p in picks}) == len(picks)
    assert flame.pick(snap, only="alpha-passer")[0]["order"] == 90 + flame.KINDS.index("praise")


def test_air_yards_per_attempt_can_flame_but_never_praise():
    assert flame.METRICS["adot"]["kinds"] == ("flame",)


def test_every_claim_re_derives_and_the_script_has_no_loose_numbers():
    snap = snapshot_with_qbs()
    c = flame.pick(snap, only="alpha-passer")[0]
    story = {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"]}
    facts = flame.build_facts(snap, story)
    for cl in facts["claims"]:
        assert evaluate_claim(snap, cl["source"]) == cl["value"], cl["label"]
    claims = {cl["id"]: cl for cl in facts["claims"]}
    beats, titles, structure = flame.compose(facts, claims, {})
    assert structure["format"] == "flame" and beats[-1]["scene"]["type"] == "decision"
    for text in [b["text_template"] for b in beats] + titles:
        bare = PLACEHOLDER.sub(" ", text)
        assert not DIGIT.search(bare), bare
        assert not NUMBER_WORDS.search(bare), bare
        assert not BANNED.search(bare), bare
    # every number drawn on screen is a listed claim
    for b in beats:
        assert all(cid in claims for cid in b["scene"].get("claims", []))


def test_a_changed_snapshot_fails_re_derivation():
    snap = snapshot_with_qbs()
    c = flame.pick(snap, only="alpha-passer")[0]
    facts = flame.build_facts(snap, {"gsis_id": c["gsis_id"], "name": c["name"], "role": c["kind"]})
    tampered = copy.deepcopy(snap)
    row = next(r for r in tampered["tables"]["qb_week_passing"] if r["gsis_id"] == "QB00" and r["week"] == 5)
    row |= {"passing_air_yards": 300.0, "passing_yards": 300, "passing_epa": 5.0}
    lead = facts["players"][0]["lead"]
    src = next(cl for cl in facts["claims"] if cl["id"] == lead["value"])["source"]
    assert evaluate_claim(tampered, src) != evaluate_claim(snap, src)
