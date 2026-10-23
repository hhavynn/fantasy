"""Slot legality goes through one place (issue #253 G11, PR 1 of 3: plumbing, no behaviour change).

positions_of reads a player's eligible positions from provider evidence only; eligible() answers every "may this player
start here"; no engine reads FLEX_ELIGIBILITY itself. Dual eligibility stays off (lineup.DUAL_ELIGIBILITY) until the
all-slot solver lands, so every lineup is exactly what it was.
"""

import ast
from pathlib import Path

import pytest

from app.analytics import lineup
from app.analytics.lineup import FLEX_ELIGIBILITY, FLEX_SLOTS, eligible, is_flex, positions_of, slot_positions

APP = Path(__file__).resolve().parents[1] / "app"

RB = [2, 3, 23, 7, 20, 21]                 # ESPN eligibleSlots for a pure RB (RB, RB/WR, FLEX, OP, bench, IR)
RB_WR = [2, 3, 4, 5, 23, 7, 20, 21]        # an RB the provider also lists at WR


def test_the_slot_helpers_are_the_table():
    assert FLEX_SLOTS == tuple(FLEX_ELIGIBILITY)                        # narrowest first, the order lineup_slots uses
    assert all(is_flex(s) for s in FLEX_SLOTS) and not any(is_flex(s) for s in lineup.FIXED_SLOTS)
    assert slot_positions("FLEX") == ("RB", "WR", "TE") and slot_positions("WR") == ("WR",)
    assert slot_positions("LB") == ("LB",)                               # an unsupported slot is only its own label


@pytest.mark.parametrize("player, expected", [
    ({"position": "RB", "eligible_slots": RB}, {"RB"}),                  # flex ids name slots, not positions
    ({"position": "RB", "eligible_slots": RB_WR}, {"RB", "WR"}),
    ({"position": "RB"}, {"RB"}),                                        # no evidence: primary only
    ({"position": "RB", "eligible_slots": None}, {"RB"}),
    ({"position": "RB", "eligible_slots": "[2, 4]"}, {"RB"}),            # malformed: primary only, never parsed
    ({"position": "RB", "eligible_slots": [4.0, True, "4"]}, {"RB"}),    # only real int slot ids count
    ({"position": "RB", "eligible_slots": [20, 21, 23, 3, 5, 7]}, {"RB"}),
    ({"position": None, "eligible_slots": [4]}, {"WR"}),
    ({"position": None}, set()),
])
def test_positions_come_from_provider_evidence_only(player, expected):
    assert positions_of(player) == frozenset(expected)


def test_objects_with_a_position_work_like_dicts():
    class P:
        position = "RB"
        eligible_slots = RB_WR
    assert positions_of(P()) == {"RB", "WR"}
    assert eligible("RB", P()) and not eligible("TE", P())


def test_dual_eligibility_is_off_so_every_player_is_primary_only():
    dual = {"position": "RB", "eligible_slots": RB_WR}
    assert lineup.DUAL_ELIGIBILITY is False
    assert eligible("RB", dual) and eligible("RB/WR", dual) and not eligible("WR", dual)
    for slot in (*lineup.FIXED_SLOTS, *FLEX_SLOTS, "LB"):                # a player answers exactly like his position
        for pos in ("QB", "RB", "WR", "TE", "K", "D/ST", None):
            assert eligible(slot, {"position": pos, "eligible_slots": RB_WR}) == eligible(slot, pos), (slot, pos)


def test_with_dual_eligibility_on_a_listed_position_opens_its_slots(monkeypatch):
    monkeypatch.setattr(lineup, "DUAL_ELIGIBILITY", True)
    assert eligible("WR", {"position": "RB", "eligible_slots": RB_WR})
    assert not eligible("WR", {"position": "RB", "eligible_slots": RB})
    assert not eligible("WR", {"position": "RB"})
    assert not eligible("TE", {"position": "RB", "eligible_slots": RB_WR})


def test_unknown_is_never_eligible():
    assert not any(eligible(s, None) for s in (*lineup.FIXED_SLOTS, *FLEX_SLOTS))
    assert not any(eligible(s, {"position": None}) for s in (*lineup.FIXED_SLOTS, *FLEX_SLOTS))


def _names(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            out |= {a.name for a in node.names}
    return out


def test_no_engine_reads_the_flex_table_itself():
    """Slot legality has one source: lineup.eligible / slot_positions / is_flex / FLEX_SLOTS. A direct read would miss
    dual eligibility when it is turned on."""
    readers = sorted(rel for f in APP.rglob("*.py")
                     if (rel := f.relative_to(APP).as_posix()) != "analytics/lineup.py"
                     and "FLEX_ELIGIBILITY" in _names(ast.parse(f.read_text())))
    assert readers == [], readers
