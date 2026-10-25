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


# ---- who may occupy an IR slot ----------------------------------------------------------------------------------------
#
# While an IR occupant's injury status is not IR-eligible, the provider blocks adds until he is moved
# (waiver_roster.plan_roster_move, add_drop). Statuses are the stored vocabulary (ESPN's; app/sleeper/write.INJURY folds
# Sleeper's statuses into it).

ESPN_IR_STATUSES = ("IR", "INJURY_RESERVE", "OUT")
HEALTHY_STATUSES = ("", "ACTIVE", "NORMAL")
# Sleeper: its own IR designation (ir; pup and nfi are folded into INJURY_RESERVE by the import) is always admitted, and
# the league settings `reserve_allow_<status>` (1 allows, 0 does not) decide the rest. The import folds out, na, cov and
# dnr into OUT, so OUT is decided only when those four settings agree. Which of these keys real Sleeper leagues carry is
# UNVERIFIED (only synthetic fixtures carry them); a missing key leaves its status undecided, never assumed.
SLEEPER_IR_ALWAYS = ("IR", "INJURY_RESERVE")
SLEEPER_RESERVE_ALLOW = (
    ("OUT", ("reserve_allow_out", "reserve_allow_na", "reserve_allow_cov", "reserve_allow_dnr")),
    ("DOUBTFUL", ("reserve_allow_doubtful",)),
    ("SUSPENSION", ("reserve_allow_sus",)),
)


def _allows(value) -> bool | None:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    return None


def sleeper_reserve_keys(settings: dict | None) -> list[str]:
    """The `reserve_allow_*` keys present in a Sleeper league's synced settings."""
    return sorted(k for k in (settings or {}) if isinstance(k, str) and k.startswith("reserve_allow_"))


def ir_rules(settings: dict | None) -> dict | None:
    """A league's IR-slot rules from its synced settings (`leagues.settings_raw`): None for ESPN (its fixed
    ESPN_IR_STATUSES); for a Sleeper league, injury status -> True (admitted), False (not admitted) or None (the synced
    settings do not decide it). Statuses no setting covers are absent, so they read as undecided."""
    settings = settings if isinstance(settings, dict) else {}
    if settings.get("provider") != "sleeper":
        return None
    out: dict[str, bool | None] = dict.fromkeys(SLEEPER_IR_ALWAYS, True)
    for status, keys in SLEEPER_RESERVE_ALLOW:
        votes = {_allows(settings.get(k)) for k in keys}
        out[status] = votes.pop() if len(votes) == 1 else None
    return out


def ir_slot_eligible(injury_status: str | None, rules: dict | None = None) -> bool | None:
    """Whether a player with this injury status may stay in an IR slot without blocking adds: True, False, or None when
    the league's synced rules do not decide it (never assumed either way). `rules` from ir_rules (None: ESPN's)."""
    if rules is None:
        return injury_status in ESPN_IR_STATUSES
    status = (injury_status or "").upper()
    if status in HEALTHY_STATUSES:
        return False
    return rules.get(status)
