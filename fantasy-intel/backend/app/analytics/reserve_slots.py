"""Roster slots that keep a player rostered without making him startable or using an active roster spot.

League files store ESPN's numeric lineup slot ids (lineup_snapshots.lineup_slot_id, roster_history.lineup_slot_id):
IR is 21, bench 20. Sleeper's dynasty taxi squad has no ESPN equivalent. app/sleeper/write.py stores a taxi player
with the bench id (20), so the numeric schema and every older league file stay as they are, and marks him with the
slot label "TAXI" (lineup_snapshots.lineup_slot). That label is the taxi identity every lineup and capacity consumer
reads through is_taxi(): a taxi player stays owned and visible (rosters, trade assets, never a free agent), but he
never enters an optimal or recommended starting lineup and does not use an active roster spot (Sleeper's taxi slots
are outside the active roster, so leagues.roster_slots["TAXI"] is not active capacity either).

Compatibility: Sleeper league files have carried the label on their current-week rows since Sleeper support landed,
so the rule applies to older snapshots without a re-sync. Played weeks come from Sleeper's matchups payload, which
does not say who was on the taxi squad that week, and roster_history keeps only the numeric id: there a taxi player
cannot be told from a bench player and is treated as one (the behaviour before this rule). ESPN has no taxi slot, so
nothing here changes an ESPN league.
"""

from __future__ import annotations

from ..espn.constants import IR_SLOT

TAXI = "TAXI"                          # lineup_snapshots.lineup_slot label and leagues.roster_slots key
INACTIVE_ROSTER_SLOTS = ("IR", TAXI)   # leagues.roster_slots keys outside the active roster


def is_taxi(row: dict) -> bool:
    """A lineup-snapshot row (or a roster dict carrying `on_taxi`) for a taxi-squad player."""
    return bool(row.get("on_taxi")) or row.get("lineup_slot") == TAXI


def is_reserved(row: dict) -> bool:
    """In an IR or taxi slot: rostered, never an active starter, and no active roster spot."""
    return row.get("lineup_slot_id") == IR_SLOT or is_taxi(row)


def active_capacity(roster_slots: dict | None) -> int:
    """Active roster capacity: every starting and bench slot; IR and taxi slots hold players outside it."""
    return sum(int(v) for k, v in (roster_slots or {}).items() if k not in INACTIVE_ROSTER_SLOTS)
