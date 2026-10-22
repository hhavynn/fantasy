"""League rules, read-only (contract league-rules/v1): roster slots and who may fill them, scoring, lineup lock,
waivers, trades and playoffs, read from the league's synced `leagues` row. Never a live provider call.

Both providers are read from the shape every league file shares (app/espn/parse.parse_league, app/sleeper/write.py):
`roster_slots` (internal slot labels -> counts), `scoring_items` (ESPN-shaped items; a Sleeper league's are translated
by app/sleeper/scoring.py) and `settings_raw` (ESPN: the league's settings object, member ids scrubbed; Sleeper: its
`settings` plus the verbatim `scoring_settings`, marked `provider: sleeper`).

Honest data:
  * a value the snapshot does not hold is None with a reason, never 0 and never a provider default;
  * a provider rule this module cannot read (an unrecognised slot, a scoring key without a label, a settings key it does
    not interpret) is listed under `unmapped`, never dropped;
  * `engine_support` states, for each rule present in this league, whether the analysis engines apply it. The statuses
    restate what the engines do (cited per entry below); this module never changes an engine.

No league ids, member ids or credentials are returned: only the settings the page shows.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..espn.constants import DEFAULT_POSITION_MAP
from .dst_store import DST_SLOT, DST_UNIVERSE, MODELED as DST_MODELED
from .kicker_scoring import FG_ITEMS, PAT_ITEMS, resolve_kicker_scoring
from .league_scoring import DIRECT, EXTRA_DIRECT, resolve_skill_scoring

CONTRACT = "league-rules/v1"
NOT_SYNCED = "No league has been synced yet, so there are no league rules to show."
PROVIDERS = {"espn": "ESPN", "sleeper": "Sleeper"}

HANDLED, PARTIAL, UNSUPPORTED, UNVERIFIED = "handled", "partial", "unsupported", "unverified"

# D/ST tiers and the D/ST scoring check, numpy-free: the app never imports app/analytics/dst_research.py (it needs numpy;
# tests/test_dst_research.py enforces it). Same values and rules as dst_research; tests/test_league_rules.py checks equality.
PA_TIERS: dict[int, tuple[float | None, float | None]] = {
    89: (None, 0), 90: (1, 6), 91: (7, 13), 92: (14, 17), 121: (18, 21), 122: (22, 27), 123: (28, 34),
    124: (35, 45), 125: (46, None)}
YA_TIERS: dict[int, tuple[float | None, float | None]] = {
    128: (None, 99), 129: (100, 199), 130: (200, 299), 131: (300, 349), 132: (350, 399), 133: (400, 449),
    134: (450, 499), 135: (500, 549), 136: (550, None)}
SLEEPER_PA_TIERS: dict[int, tuple[float | None, float | None]] = {
    1089: (None, 0), 1090: (1, 6), 1091: (7, 13), 1092: (14, 20), 1093: (21, 27), 1094: (28, 34), 1095: (35, None)}


def dst_unmodeled(items: list[dict] | None) -> tuple[bool, list[dict]]:
    """(whether the D/ST model scores this league, the D/ST rules it does not implement), as
    dst_research.resolve_dst_scoring decides `supported` and `unmodeled`."""
    if not items:
        return False, [{"stat_id": None, "reason": "no scoring settings synced"}]
    weighted, unmodeled = False, []
    for item in items:
        overrides = {str(k): v for k, v in (item.get("pointsOverrides") or {}).items()}
        sid = item.get("statId")
        value = float(overrides[DST_SLOT] if DST_SLOT in overrides else (item.get("points") or 0.0))
        if not value or sid not in DST_UNIVERSE:
            continue
        if item.get("isReverseItem"):
            unmodeled.append({"stat_id": sid, "reason": "reverse scoring item"})
            continue
        weighted = True
        if sid not in DST_MODELED:
            unmodeled.append({"stat_id": sid, "reason": "D/ST statistic the model does not project"})
    return weighted and not unmodeled, unmodeled



def fact(value: Any, display: str | None = None, reason: str | None = None) -> dict:
    """One setting: its value and readable text, or None with the reason it is missing (never 0, never a guess)."""
    if value is None:
        if not reason:
            raise ValueError("a missing value needs a reason")
        return {"value": None, "display": None, "reason": reason}
    return {"value": value, "display": display if display is not None else str(value), "reason": None}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _g(n: float) -> str:
    return f"{n:g}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---- roster slots -------------------------------------------------------------------------------------------------

# Internal slot label (app/espn/constants.SLOT_MAP, app/sleeper/constants.SLOT_MAP) -> (kind, eligible positions, name).
_SLOTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "QB": ("starter", ("QB",), "Quarterback"),
    "RB": ("starter", ("RB",), "Running back"),
    "WR": ("starter", ("WR",), "Wide receiver"),
    "TE": ("starter", ("TE",), "Tight end"),
    "RB/WR": ("starter", ("RB", "WR"), "RB/WR flex"),
    "WR/TE": ("starter", ("WR", "TE"), "WR/TE flex"),
    "FLEX": ("starter", ("RB", "WR", "TE"), "Flex"),
    "OP": ("starter", ("QB", "RB", "WR", "TE"), "Superflex (OP)"),
    "K": ("starter", ("K",), "Kicker"),
    "D/ST": ("starter", ("D/ST",), "Team defense and special teams"),
    "BE": ("bench", (), "Bench"),
    "IR": ("ir", (), "Injured reserve"),
    "TAXI": ("taxi", (), "Taxi squad"),
}
# Individual defensive players. The two providers name positions differently (ESPN: DT, DE, LB, CB, S; Sleeper: DL, LB,
# DB), so the same label can accept different positions.
_IDP = {
    "espn": {"DT": ("DT",), "DE": ("DE",), "LB": ("LB",), "DL": ("DT", "DE"), "CB": ("CB",), "S": ("S",),
             "DB": ("CB", "S"), "DP": ("DT", "DE", "LB", "CB", "S")},
    "sleeper": {"DL": ("DL",), "LB": ("LB",), "DB": ("DB",), "DP": ("DL", "LB", "DB")},
}
_IDP_NAMES = {"DT": "Defensive tackle", "DE": "Defensive end", "LB": "Linebacker", "DL": "Defensive line",
              "CB": "Cornerback", "S": "Safety", "DB": "Defensive back", "DP": "IDP flex"}
_ESPN_OTHER = {"P": ("starter", ("P",), "Punter"), "HC": ("starter", ("HC",), "Head coach")}
FIXED = ("QB", "RB", "WR", "TE", "K", "D/ST")          # app/analytics/lineup.FIXED_SLOTS
FLEX = ("RB/WR", "WR/TE", "FLEX", "OP")                # app/analytics/lineup.FLEX_ELIGIBILITY
_ORDER = ("QB", "RB", "WR", "TE", "RB/WR", "WR/TE", "FLEX", "OP", "K", "D/ST", "DT", "DE", "DL", "LB", "CB", "S", "DB",
          "DP", "P", "HC")
_TAIL = ("BE", "IR", "TAXI")


def _slot_info(label: str, provider: str) -> tuple[str, tuple[str, ...] | None, str]:
    if label in _SLOTS:
        return _SLOTS[label]
    if label in _IDP[provider]:
        return "starter", _IDP[provider][label], _IDP_NAMES[label]
    if provider == "espn" and label in _ESPN_OTHER:
        return _ESPN_OTHER[label]
    return "unknown", None, label or "Unlabelled slot"


def _slot_engine(label: str, kind: str) -> dict:
    """What the lineup engines do with this slot (app/analytics/lineup.py `lineup_slots`, start_sit.build_slot_instances,
    team_lineups, waivers, trade_data): only FIXED and FLEX labels are ever filled."""
    if label in FIXED:
        return {"status": HANDLED, "note": "Filled by every projected lineup."}
    if label in FLEX:
        return {"status": HANDLED, "note": "Filled by an exact search over every eligible player, so mixed flex slots "
                                           "are never filled in the wrong order."}
    if kind == "bench":
        return {"status": HANDLED, "note": "Bench players are candidates for every starting slot they are eligible for."}
    if kind == "ir":
        return {"status": HANDLED, "note": "Players on injured reserve never start in a projected lineup and do not "
                                           "use an active roster spot."}
    if kind == "taxi":
        return {"status": HANDLED, "note": "Taxi players stay on the roster but never start in a projected lineup and "
                                           "do not use an active roster spot."}
    return {"status": UNSUPPORTED, "note": "Not applied: no engine fills this slot, so projected lineups and lineup "
                                           "totals leave it out."}


def roster_section(roster_slots: Any, settings: dict, provider: str) -> tuple[dict, list[dict]]:
    slots_raw = roster_slots if isinstance(roster_slots, dict) else {}
    unmapped: list[dict] = []
    rows = []
    for label, count in slots_raw.items():
        n = _int(count)
        if n is None or n <= 0:
            continue
        label = str(label)
        kind, eligible, name = _slot_info(label, provider)
        row = {"code": label or "?", "name": name, "count": n, "kind": kind,
               "eligible": list(eligible) if eligible is not None else None,
               "eligible_reason": None if eligible is not None else "This provider slot is not mapped to positions.",
               "engine": _slot_engine(label, kind)}
        rows.append(row)
        if kind == "unknown":
            unmapped.append({"area": "Roster slot", "key": label or "(blank label)", "value": n,
                             "reason": "Slot not recognised: its eligible positions are unknown."})
    order = {code: i for i, code in enumerate(_ORDER)}
    rows.sort(key=lambda r: (r["code"] in _TAIL, _TAIL.index(r["code"]) if r["code"] in _TAIL else 0,
                             order.get(r["code"], len(order))))
    by_kind = {k: sum(r["count"] for r in rows if r["kind"] == k) for k in ("starter", "bench", "ir", "taxi", "unknown")}
    flex_types = [r["code"] for r in rows if r["code"] in FLEX]
    out = {
        "slots": rows,
        "starters": _starters(by_kind) if rows else fact(None, reason="The snapshot holds no roster slot counts."),
        "bench": by_kind["bench"], "injured_reserve": by_kind["ir"], "taxi": by_kind["taxi"],
        "superflex": sum(r["count"] for r in rows if r["code"] == "OP") > 0,
        "flex_types": flex_types,
        "position_limits": _position_limits(settings, provider, unmapped),
    }
    if not rows:
        unmapped.append({"area": "Roster slot", "key": "roster_slots", "value": None,
                         "reason": "No slot counts were synced."})
    return out, unmapped


def _starters(by_kind: dict) -> dict:
    """Every slot except bench, IR and taxi is a starting slot for both providers (the adapters' `starters()` agree), so
    an unrecognised slot still counts; the text says how many of them are unrecognised."""
    total = by_kind["starter"] + by_kind["unknown"]
    unknown = by_kind["unknown"]
    return fact(total, f"{total} ({unknown} unrecognised)" if unknown else str(total))


def _position_limits(settings: dict, provider: str, unmapped: list[dict]) -> dict:
    if provider != "espn":
        return fact(None, reason="Per-position roster limits are not in this provider's synced settings.")
    rs = settings.get("rosterSettings")
    limits = rs.get("positionLimits") if isinstance(rs, dict) else None
    if not isinstance(limits, dict):
        return fact(None, reason="The synced settings do not include per-position roster limits.")
    out = {}
    for key, val in limits.items():
        n = _int(val)
        if n is None or n <= 0:              # ESPN writes 0 for "no limit" (app/analytics/waiver_roster.py)
            continue
        pos = DEFAULT_POSITION_MAP.get(int(key)) if str(key).isdigit() else None
        if pos is None:
            unmapped.append({"area": "Roster limit", "key": f"position {key}", "value": n,
                             "reason": "Position id not recognised."})
            continue
        out[pos] = n
    if not out:
        return fact({}, "No per-position limits")
    return fact(out, ", ".join(f"{p} at most {n}" for p, n in out.items()))


# ---- scoring ------------------------------------------------------------------------------------------------------

_BUCKET_TEXT = {"0_39": "0-39 yards", "40_49": "40-49 yards", "50_59": "50-59 yards", "60_plus": "60+ yards",
                "50_plus": "50+ yards", "any": "any distance", "0_19": "0-19 yards", "20_29": "20-29 yards",
                "30_39": "30-39 yards"}
_FG_OUTCOME = {"made": "Field goal made", "attempted": "Field goal attempted", "missed": "Field goal missed"}


def _tier_text(lo, hi, unit: str) -> str:
    if lo is None:
        return f"{unit} {hi} or fewer" if hi else f"{unit} 0"
    if hi is None:
        return f"{unit} {lo}+"
    return f"{unit} {lo}-{hi}"


# ESPN statId -> (group, label). Ids follow espn-api's PLAYER_STATS_MAP and the engines' own tables; an id missing here
# is listed as unmapped. 41 is also "receptions" in that map but only 53 is the id the engines read, so 41 stays
# unmapped rather than guessed.
ESPN_STATS: dict[int, tuple[str, str]] = {
    3: ("Passing", "Passing yards"), 4: ("Passing", "Passing TD"), 19: ("Passing", "Passing 2-pt conversion"),
    20: ("Passing", "Interception thrown"), 64: ("Passing", "Sacked"),
    24: ("Rushing", "Rushing yards"), 25: ("Rushing", "Rushing TD"), 26: ("Rushing", "Rushing 2-pt conversion"),
    42: ("Receiving", "Receiving yards"), 43: ("Receiving", "Receiving TD"),
    44: ("Receiving", "Receiving 2-pt conversion"), 53: ("Receiving", "Reception"),
    63: ("Miscellaneous", "Fumble recovered for TD"), 72: ("Miscellaneous", "Fumble lost"),
    101: ("Miscellaneous", "Kickoff return TD"), 102: ("Miscellaneous", "Punt return TD"),
    **{sid: ("Kicking", f"{_FG_OUTCOME[o]}, {_BUCKET_TEXT[b]}") for sid, (o, b) in FG_ITEMS.items() if sid < 1000},
    **{sid: ("Kicking", f"Extra point {o}") for sid, o in PAT_ITEMS.items()},
    93: ("Team defense", "Blocked kick return TD"), 95: ("Team defense", "Interception"),
    96: ("Team defense", "Fumble recovery"), 97: ("Team defense", "Blocked kick"), 98: ("Team defense", "Safety"),
    99: ("Team defense", "Sack"), 103: ("Team defense", "Interception return TD"),
    104: ("Team defense", "Fumble return TD"), 106: ("Team defense", "Forced fumble"),
    120: ("Team defense", "Points allowed (per point)"), 127: ("Team defense", "Yards allowed (per yard)"),
    206: ("Team defense", "Defensive 2-pt return"), 209: ("Team defense", "1-pt safety"),
    **{sid: ("Team defense", _tier_text(lo, hi, "Points allowed")) for sid, (lo, hi) in PA_TIERS.items()},
    **{sid: ("Team defense", _tier_text(lo, hi, "Yards allowed")) for sid, (lo, hi) in YA_TIERS.items()},
}
# Synthetic ids of translated provider items (never ESPN's): only named in engine notes.
_SYNTHETIC = {**{sid: label for sid, (_, label) in EXTRA_DIRECT.items()},
              **{sid: f"{_FG_OUTCOME[o]}, {_BUCKET_TEXT[b]}" for sid, (o, b) in FG_ITEMS.items() if sid >= 1000},
              **{sid: _tier_text(lo, hi, "Points allowed") for sid, (lo, hi) in SLEEPER_PA_TIERS.items()},
              1106: "Forced fumble"}
_ = DIRECT        # the engine's own table these labels restate (kept imported so a rename fails here)

_SLEEPER_KEYS: dict[str, tuple[str, str]] = {
    "pass_yd": ("Passing", "Passing yards"), "pass_td": ("Passing", "Passing TD"),
    "pass_int": ("Passing", "Interception thrown"), "pass_2pt": ("Passing", "Passing 2-pt conversion"),
    "pass_sack": ("Passing", "Sacked"), "pass_att": ("Passing", "Pass attempt"),
    "pass_cmp": ("Passing", "Pass completion"), "pass_inc": ("Passing", "Incomplete pass"),
    "pass_fd": ("Passing", "Passing first down"), "pass_int_td": ("Passing", "Interception returned for TD"),
    "rush_yd": ("Rushing", "Rushing yards"), "rush_td": ("Rushing", "Rushing TD"),
    "rush_2pt": ("Rushing", "Rushing 2-pt conversion"), "rush_att": ("Rushing", "Rush attempt"),
    "rush_fd": ("Rushing", "Rushing first down"),
    "rec": ("Receiving", "Reception"), "rec_yd": ("Receiving", "Receiving yards"), "rec_td": ("Receiving", "Receiving TD"),
    "rec_2pt": ("Receiving", "Receiving 2-pt conversion"), "rec_fd": ("Receiving", "Receiving first down"),
    "bonus_rec_te": ("Receiving", "TE reception bonus"), "bonus_rec_rb": ("Receiving", "RB reception bonus"),
    "bonus_rec_wr": ("Receiving", "WR reception bonus"),
    "fum": ("Miscellaneous", "Fumble"), "fum_lost": ("Miscellaneous", "Fumble lost"),
    "fum_rec_td": ("Miscellaneous", "Fumble recovered for TD"), "st_td": ("Miscellaneous", "Return TD (player)"),
    "st_ff": ("Miscellaneous", "Special-teams forced fumble (player)"),
    "st_fum_rec": ("Miscellaneous", "Special-teams fumble recovery (player)"),
    "fgm": ("Kicking", "Field goal made"), "fgmiss": ("Kicking", "Field goal missed"),
    "fga": ("Kicking", "Field goal attempted"), "xpm": ("Kicking", "Extra point made"),
    "xpmiss": ("Kicking", "Extra point missed"), "xpa": ("Kicking", "Extra point attempted"),
    "sack": ("Team defense", "Sack"), "def_sack": ("Team defense", "Sack"), "int": ("Team defense", "Interception"),
    "fum_rec": ("Team defense", "Fumble recovery"), "ff": ("Team defense", "Forced fumble"),
    "safe": ("Team defense", "Safety"), "blk_kick": ("Team defense", "Blocked kick"),
    "def_td": ("Team defense", "Defensive TD"), "def_st_td": ("Team defense", "Special-teams TD"),
    "def_kr_td": ("Team defense", "Kickoff return TD"), "def_pr_td": ("Team defense", "Punt return TD"),
    "def_2pt": ("Team defense", "Defensive 2-pt return"),
    "def_st_ff": ("Team defense", "Special-teams forced fumble"),
    "def_st_fum_rec": ("Team defense", "Special-teams fumble recovery"),
}
_FG_KEY = re.compile(r"^(fgm|fgmiss)_(\d+)(?:_(\d+)|(p))$")
_TIER_KEY = re.compile(r"^(pts|yds)_allow_(\d+)(?:_(\d+)|(p))?$")
_BONUS_KEY = re.compile(r"^bonus_(pass|rush|rec)_yd_(\d+)$")


def sleeper_label(key: str) -> tuple[str, str] | None:
    if key in _SLEEPER_KEYS:
        return _SLEEPER_KEYS[key]
    if m := _FG_KEY.match(key):
        what = "Field goal made" if m.group(1) == "fgm" else "Field goal missed"
        rng = f"{m.group(2)}+" if m.group(4) else f"{m.group(2)}-{m.group(3)}"
        return "Kicking", f"{what}, {rng} yards"
    if m := _TIER_KEY.match(key):
        unit = "Points allowed" if m.group(1) == "pts" else "Yards allowed"
        lo = m.group(2)
        text = f"{unit} {lo}+" if m.group(4) else f"{unit} {lo}-{m.group(3)}" if m.group(3) else f"{unit} {lo}"
        return "Team defense", text
    if m := _BONUS_KEY.match(key):
        what = {"pass": "passing", "rush": "rushing", "rec": "receiving"}[m.group(1)]
        return "Bonus", f"{m.group(2)}+ {what} yards in a game"
    return None


_SLOT_NAMES = {"0": "QB", "2": "RB", "4": "WR", "6": "TE", "16": "D/ST", "17": "K", "23": "FLEX", "7": "OP"}


def _espn_rules(items: list, unmapped: list[dict]) -> tuple[list[dict], int]:
    rows, zero = [], 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sid, base = item.get("statId"), _num(item.get("points"))
        overrides = {str(k): _num(v) for k, v in (item.get("pointsOverrides") or {}).items() if _num(v) is not None}
        by_pos = {str(k): _num(v) for k, v in (item.get("positionPoints") or {}).items() if _num(v)}
        if not base and not any(overrides.values()) and not by_pos:
            zero += 1
            continue
        known = ESPN_STATS.get(sid) if isinstance(sid, int) else None
        row = {"key": str(sid), "group": known[0] if known else "Unmapped",
               "label": known[1] if known else f"Stat {sid}", "points": base if base is not None else 0.0,
               "by_slot": [{"slot": _SLOT_NAMES.get(k, f"slot {k}"), "points": v} for k, v in overrides.items()],
               "by_position": [{"position": p, "points": v} for p, v in by_pos.items()],
               "reverse": bool(item.get("isReverseItem")), "mapped": known is not None}
        rows.append(row)
        if known is None:
            unmapped.append({"area": "Scoring rule", "key": f"stat {sid}", "value": base,
                             "reason": "No readable label for this provider statistic."})
    return rows, zero


def _sleeper_rules(scoring_settings: dict, unmapped: list[dict]) -> tuple[list[dict], int]:
    rows, zero = [], 0
    for key, raw in sorted(scoring_settings.items()):
        value = _num(raw)
        if value is None:
            unmapped.append({"area": "Scoring rule", "key": str(key), "value": None,
                             "reason": "Not a points value."})
            continue
        if not value:
            zero += 1
            continue
        known = sleeper_label(str(key))
        rows.append({"key": str(key), "group": known[0] if known else "Unmapped",
                     "label": known[1] if known else str(key), "points": value, "by_slot": [], "by_position": [],
                     "reverse": False, "mapped": known is not None})
        if known is None:
            unmapped.append({"area": "Scoring rule", "key": str(key), "value": value,
                             "reason": "No readable label for this provider rule."})
    return rows, zero


_FORMATS = {1.0: ("ppr", "Full PPR"), 0.5: ("half_ppr", "Half PPR"), 0.0: ("standard", "Standard (no points per catch)")}


def _reception(items: list) -> tuple[float | None, float | None, str | None]:
    """(points per reception, TE premium per reception, how the premium is expressed) from ESPN-shaped items."""
    rec = [i for i in items if isinstance(i, dict) and i.get("statId") == 53]
    if not rec:
        return None, None, None
    base = sum(_num(i.get("points")) or 0.0 for i in rec)
    te = 0.0
    how = None
    for i in rec:
        pos = _num((i.get("positionPoints") or {}).get("TE"))
        if pos:
            te += pos
            how = "position"
        slot = _num({str(k): v for k, v in (i.get("pointsOverrides") or {}).items()}.get("6"))
        if slot is not None and slot != (_num(i.get("points")) or 0.0):
            te += slot - (_num(i.get("points")) or 0.0)
            how = "slot"
    return base, (te or None), how


def _per_unit(points: float | None, unit: str) -> str | None:
    if points is None:
        return None
    if 0 < abs(points) < 1 and _g(1 / abs(points)).isdigit():
        return f"{_g(points)} per {unit} (1 point per {_g(1 / abs(points))} {unit}s)" if points > 0 else \
            f"{_g(points)} per {unit}"
    return f"{_g(points)} per {unit}"


# (ESPN statId, Sleeper key, label, unit)
_HIGHLIGHTS = ((4, "pass_td", "Passing TD", "TD"), (3, "pass_yd", "Passing yards", "yard"),
               (20, "pass_int", "Interception thrown", "interception"), (25, "rush_td", "Rushing TD", "TD"),
               (24, "rush_yd", "Rushing yards", "yard"), (43, "rec_td", "Receiving TD", "TD"),
               (42, "rec_yd", "Receiving yards", "yard"), (72, "fum_lost", "Fumble lost", "fumble"))


def scoring_section(lg: dict, items: list, settings: dict, provider: str) -> tuple[dict, list[dict]]:
    unmapped: list[dict] = []
    verbatim = settings.get("scoring_settings") if provider == "sleeper" else None
    if isinstance(verbatim, dict):
        rules, zero = _sleeper_rules(verbatim, unmapped)
        source = "Sleeper's scoring settings"
        captured = bool(verbatim)
        # Verbatim values: a rule worth 0 is a measured 0 here (the translated items drop zero rules).
        rp, te = _num(verbatim.get("rec")), _num(verbatim.get("bonus_rec_te")) or None
        how = "position" if te else None
        values = {k: _num(v) for k, v in verbatim.items() if _num(v) is not None}
        lookup = lambda sid, key: values.get(key)          # noqa: E731
    else:
        rules, zero = _espn_rules(items, unmapped)
        source = "ESPN's scoring items" if provider == "espn" else "Translated scoring items"
        captured = bool(items)
        rp, te, how = _reception(items)
        by_id: dict[int, float] = {}
        for i in items:
            if isinstance(i.get("statId"), int) and _num(i.get("points")) is not None:
                by_id[i["statId"]] = by_id.get(i["statId"], 0.0) + float(i["points"])
        lookup = lambda sid, key: by_id.get(sid)           # noqa: E731
    if not captured:
        fmt = fact(None, reason="No scoring rules were synced for this league.")
        ppr = fact(None, reason="No scoring rules were synced for this league.")
    elif rp is None:
        fmt = fact(None, reason="The synced rules have no per-reception rule, so the format is not labelled.")
        ppr = fact(None, reason="No per-reception rule in the synced scoring.")
    else:
        code, label = _FORMATS.get(round(rp, 4), ("custom", f"{_g(rp)} points per reception"))
        fmt = fact(code, label)
        ppr = fact(rp, _per_unit(rp, "reception"))
    if not captured:
        te_fact = fact(None, reason="No scoring rules were synced for this league.")
    elif te is None:
        te_fact = fact(0.0, "None")
    else:
        where = "for a TE in the TE slot" if how == "slot" else "for every TE"
        te_fact = fact(te, f"+{_g(te)} per reception {where}")
    stype_raw = (settings.get("scoringSettings") or {}).get("scoringType") if provider == "espn" else None
    if provider != "espn":
        stype = fact(None, reason="This provider does not report a scoring type.")
    elif stype_raw == "H2H_POINTS":
        stype = fact("h2h_points", "Head-to-head points")
    elif isinstance(stype_raw, str) and stype_raw:
        stype = fact(stype_raw, stype_raw)
        unmapped.append({"area": "Scoring type", "key": "scoringSettings.scoringType", "value": stype_raw,
                         "reason": "Scoring type not recognised."})
    else:
        stype = fact(None, reason="The synced settings do not include a scoring type.")
    highlights = []
    for sid, key, label, unit in _HIGHLIGHTS:
        pts = lookup(sid, key)
        highlights.append({"label": label, **(fact(pts, _per_unit(pts, unit)) if pts is not None else fact(
            None, reason="No rule for this statistic in the synced scoring." if captured else
            "No scoring rules were synced for this league."))})
    groups = ("Passing", "Rushing", "Receiving", "Bonus", "Miscellaneous", "Kicking", "Team defense", "Unmapped")
    rules.sort(key=lambda r: (groups.index(r["group"]) if r["group"] in groups else len(groups), r["label"]))
    return {"format": fmt, "points_per_reception": ppr, "te_premium": te_fact, "te_premium_scope": how,
            "scoring_type": stype,
            "highlights": highlights, "rules": rules, "zero_rules": zero, "rules_source": source}, unmapped


# ---- lineup lock, waivers, trades, playoffs ----------------------------------------------------------------------

_LOCKS = {"INDIVIDUAL_GAME": ("individual_game", "Each player locks at his own game's kickoff")}


def lineup_lock_section(settings: dict, provider: str) -> tuple[dict, list[dict]]:
    if provider != "espn":
        return fact(None, reason="The synced league settings do not include a lineup-lock rule."), []
    rs = settings.get("rosterSettings") if isinstance(settings.get("rosterSettings"), dict) else {}
    raw = rs.get("lineupLocktimeType")
    if raw in _LOCKS:
        return fact(*_LOCKS[raw]), []
    if isinstance(raw, str) and raw:
        return fact(raw, raw), [{"area": "Lineup lock", "key": "rosterSettings.lineupLocktimeType", "value": raw,
                                 "reason": "Lock rule not recognised."}]
    return fact(None, reason="The synced settings do not include a lineup-lock rule."), []


_ESPN_ORDER = {"ROLLING": "rolling", "MOVE_TO_LAST_AFTER_CLAIM": "rolling",
               "MOVE_TO_LAST_AFTER_SUCCESSFUL_CLAIM": "rolling", "INVERSE_STANDINGS": "reverse_standings"}
_WAIVER_TEXT = {"faab": "FAAB bidding", "rolling": "Rolling waiver order (a successful claim moves to the back)",
                "reverse_standings": "Reverse standings order"}
_DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")


def waivers_section(lg: dict, settings: dict, provider: str) -> tuple[dict, list[dict]]:
    unmapped: list[dict] = []
    if provider == "sleeper":
        wtype, budget, clear = settings.get("waiver_type"), settings.get("waiver_budget"), settings.get("waiver_clear_days")
        kind = {0: "rolling", 1: "reverse_standings", 2: "faab"}.get(wtype) if _int(wtype) is not None else None
        if kind is None and wtype is not None:
            unmapped.append({"area": "Waivers", "key": "waiver_type", "value": wtype, "reason": "Waiver type not recognised."})
        type_fact = fact(kind, _WAIVER_TEXT.get(kind)) if kind else fact(
            None, reason="The synced settings do not say how waiver claims are decided.")
        budget_fact = (fact(_int(budget), f"${_int(budget)}") if kind == "faab" and _int(budget) is not None else
                       fact(None, reason="Not used: claims are not bid with FAAB." if kind and kind != "faab" else
                            "The synced settings do not include a FAAB budget."))
        period = (fact(_int(clear), f"Dropped players stay on waivers for {_plural(_int(clear), 'day')}")
                  if _int(clear) is not None else fact(None, reason="The synced settings do not include the waiver period."))
        process = fact(None, reason="Sleeper's waiver processing day is not interpreted here (listed under unmapped).")
        return {"type": type_fact, "faab_budget": budget_fact, "period": period, "process_days": process}, unmapped
    acq = settings.get("acquisitionSettings") if isinstance(settings.get("acquisitionSettings"), dict) else {}
    is_faab = lg.get("is_faab") if isinstance(lg.get("is_faab"), bool) else acq.get("isUsingAcquisitionBudget")
    order_raw = acq.get("waiverOrderType")
    if is_faab is True:
        kind = "faab"
    else:
        kind = _ESPN_ORDER.get(str(order_raw).upper()) if order_raw else None
        if order_raw and kind is None:
            unmapped.append({"area": "Waivers", "key": "acquisitionSettings.waiverOrderType", "value": order_raw,
                             "reason": "Waiver order rule not recognised."})
    type_fact = fact(kind, _WAIVER_TEXT[kind]) if kind else fact(
        None, reason="Not FAAB, and the synced settings do not say how the waiver order works." if is_faab is False
        else "The synced settings do not say how waiver claims are decided.")
    budget = _int(lg.get("faab_budget"))
    budget = budget if budget is not None else _int(acq.get("acquisitionBudget"))
    budget_fact = (fact(budget, f"${budget}") if kind == "faab" and budget is not None else
                   fact(None, reason="Not used: claims are not bid with FAAB." if is_faab is False else
                        "The synced settings do not include a FAAB budget."))
    hours = _int(acq.get("waiverHours"))
    period = (fact(hours, f"Dropped players stay on waivers for {_plural(hours, 'hour')}") if hours is not None else
              fact(None, reason="The synced settings do not include the waiver period."))
    days_raw = _json(lg.get("waiver_process_days"))
    days_raw = days_raw if isinstance(days_raw, list) else acq.get("waiverProcessDays")
    days = [d for d in days_raw if isinstance(d, str)] if isinstance(days_raw, list) else None
    if days:
        known = [d.title() for d in days if d.upper() in _DAYS]
        for d in days:
            if d.upper() not in _DAYS:
                unmapped.append({"area": "Waivers", "key": "waiverProcessDays", "value": d, "reason": "Day not recognised."})
        process = fact(known, "Claims process on " + ", ".join(known)) if known else fact(
            None, reason="The synced processing days were not recognised.")
    else:
        process = fact(None, reason="The synced settings do not include the waiver processing days.")
    return {"type": type_fact, "faab_budget": budget_fact, "period": period, "process_days": process}, unmapped


def _date_text(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b %Y}"


def trades_section(lg: dict, settings: dict, provider: str) -> dict:
    if provider == "sleeper":
        week = _int(settings.get("trade_deadline_week"))
        week = week if week is not None else _int(settings.get("trade_deadline"))
        deadline = (fact(week, f"Week {week}") if week and week > 0 else
                    fact(None, reason="The synced settings do not include a trade deadline."))
        days = _int(settings.get("trade_review_days"))
        review = (fact(days, "No review period" if days == 0 else f"{_plural(days, 'day')} before a trade is processed")
                  if days is not None else fact(None, reason="The synced settings do not include a trade review period."))
        return {"deadline": deadline, "review": review}
    dt = lg.get("trade_deadline")
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            dt = None
    deadline = (fact(dt.isoformat(), f"{_date_text(dt)} (UTC)") if isinstance(dt, datetime) else
                fact(None, reason="The synced settings do not include a trade deadline."))
    ts = settings.get("tradeSettings") if isinstance(settings.get("tradeSettings"), dict) else {}
    hours = _int(ts.get("reviewPeriodHours"))
    review = (fact(hours, "No review period" if hours == 0 else f"{_plural(hours, 'hour')} before a trade is processed")
              if hours is not None else fact(None, reason="The synced settings do not include a trade review period."))
    return {"deadline": deadline, "review": review}


_ROUNDS = {0: "One week per round", 1: "One week per round, two-week championship", 2: "Two weeks per round"}


def playoffs_section(lg: dict, settings: dict, provider: str) -> dict:
    teams = _int(lg.get("playoff_teams"))
    final = _int(lg.get("final_scoring_period"))
    out = {"teams": fact(teams, str(teams)) if teams else fact(None, reason="The synced settings do not include the "
                                                                            "number of playoff teams."),
           "final_week": fact(final, f"Week {final}") if final else fact(None, reason="The synced settings do not "
                                                                                      "include the final week.")}
    if provider == "sleeper":
        last = _int(settings.get("last_regular_season_week"))
        start = _int(settings.get("playoff_week_start"))
        rtype = _int(settings.get("playoff_round_type"))
        out["regular_season"] = (fact(last, f"Through week {last}") if last else
                                 fact(None, reason="The synced settings do not include the regular-season length."))
        out["start"] = (fact(start, f"Week {start}") if start else
                        fact(None, reason="The synced settings do not include the playoff start week."))
        out["round_length"] = (fact(rtype, _ROUNDS[rtype]) if rtype in _ROUNDS else
                               fact(None, reason="The synced settings do not include the playoff round length."))
        return out
    periods = _int(lg.get("reg_season_matchups"))
    sched = settings.get("scheduleSettings") if isinstance(settings.get("scheduleSettings"), dict) else {}
    length = _int(sched.get("playoffMatchupPeriodLength"))
    out["regular_season"] = (fact(periods, f"{_plural(periods, 'matchup period')}") if periods else
                             fact(None, reason="The synced settings do not include the regular-season length."))
    out["start"] = (fact(periods + 1, f"Matchup period {periods + 1}") if periods else
                    fact(None, reason="The synced settings do not include the regular-season length."))
    out["round_length"] = (fact(length, f"{_plural(length, 'week')} per round") if length else
                           fact(None, reason="The synced settings do not include the playoff round length."))
    return out


# ---- settings this module does not interpret ------------------------------------------------------------------------

_ESPN_READ = {
    "": {"name", "size", "rosterSettings", "scoringSettings", "acquisitionSettings", "tradeSettings",
         "scheduleSettings"},
    "rosterSettings": {"lineupSlotCounts", "positionLimits", "lineupLocktimeType", "isUsingUndroppableList"},
    "scoringSettings": {"scoringType", "scoringItems"},
    "acquisitionSettings": {"isUsingAcquisitionBudget", "acquisitionBudget", "waiverOrderType", "waiverHours",
                            "waiverProcessDays"},
    "tradeSettings": {"deadlineDate", "reviewPeriodHours"},
    "scheduleSettings": {"matchupPeriodCount", "playoffTeamCount", "playoffMatchupPeriodLength"},
}
_SLEEPER_READ = {"type", "playoff_teams", "playoff_week_start", "playoff_round_type", "waiver_type", "waiver_budget",
                 "waiver_clear_days", "reserve_slots", "taxi_slots", "trade_deadline", "trade_review_days", "num_teams",
                 # written by app/sleeper/write.py, not provider settings (the league id is never returned)
                 "provider", "sleeper_league_id", "scoring_settings", "scoring_unsupported", "scoring_notes",
                 "trade_deadline_week", "last_regular_season_week"}


def _shown(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and len(value) <= 60:
        return value
    return None


def other_settings(settings: dict, provider: str) -> list[dict]:
    """Provider settings this page does not interpret: listed (scalar values only), never dropped."""
    out = []
    reason = "Provider setting Fantasy Intel does not interpret."
    if provider == "sleeper":
        for key in sorted(settings):
            if key not in _SLEEPER_READ:
                out.append({"area": "Setting", "key": key, "value": _shown(settings[key]), "reason": reason})
        return out
    for key in sorted(settings):
        if key not in _ESPN_READ[""]:
            out.append({"area": "Setting", "key": key, "value": _shown(settings[key]), "reason": reason})
        elif key in _ESPN_READ and isinstance(settings[key], dict):
            for sub in sorted(settings[key]):
                if sub not in _ESPN_READ[key]:
                    out.append({"area": "Setting", "key": f"{key}.{sub}", "value": _shown(settings[key][sub]),
                                "reason": reason})
    return out


# ---- engine support ------------------------------------------------------------------------------------------------

def _stat_name(sid: Any, sleeper_unsupported: list[str]) -> str:
    if isinstance(sid, list):
        return " and ".join(_stat_name(s, sleeper_unsupported) for s in sid)
    if sid in (2999, 1099, 137):            # app/sleeper/scoring.py: a provider rule no family implements
        return ", ".join(sleeper_unsupported) or "an unimplemented provider rule"
    if isinstance(sid, int):
        if sid in ESPN_STATS:
            return ESPN_STATS[sid][1].lower()
        if sid in _SYNTHETIC:
            return _SYNTHETIC[sid].lower()
    return f"stat {sid}" if sid is not None else "the scoring rules"


def _reasons(entries: list[dict], sleeper_unsupported: list[str]) -> str:
    seen = []
    for e in entries:
        sid = e.get("stat_id")
        name = _stat_name(sid, sleeper_unsupported) if sid is not None else str(e.get("reason") or "no rules")
        if name not in seen:
            seen.append(name)
    return "; ".join(seen)


def engine_support(provider: str, roster: dict, scoring: dict, items: list, lock: dict, settings: dict) -> list[dict]:
    rows: list[dict] = []
    name = PROVIDERS[provider]
    sl_unsupported = [str(k) for k in settings.get("scoring_unsupported") or []] if provider == "sleeper" else []
    slots = roster["slots"]

    # Superflex and flex slots (app/analytics/lineup.py, start_sit.build_slot_instances, trade_discovery._Totals).
    if roster["superflex"]:
        rows.append({"rule": "Superflex (OP) slot", "status": PARTIAL,
                     "detail": "Projected lineups, Start/Sit, Matchup, Waivers and the trade analyzer start a QB in "
                               "the OP slot when he projects highest. Trade discovery's starter-caliber labels and "
                               "replacement levels count only the fixed QB slots, so they understate QB demand."})
    flex = roster["flex_types"]
    if len(flex) > 1:
        rows.append({"rule": "Several flex slot types", "status": HANDLED,
                     "detail": f"{', '.join(flex)}: every lineup engine searches all flex combinations exactly, so a "
                               "player is never placed in a narrower slot when a wider one fits better."})
    elif flex == ["FLEX"]:
        rows.append({"rule": "Flex slot", "status": HANDLED, "detail": "Filled by every projected lineup."})
    skipped = [s for s in slots if s["kind"] in ("starter", "unknown") and s["engine"]["status"] == UNSUPPORTED]
    if skipped:
        rows.append({"rule": "Starting slots no engine fills", "status": UNSUPPORTED,
                     "detail": f"{', '.join(s['code'] for s in skipped)}: left out of every projected lineup, lineup "
                               "total and trade value; waiver roster planning reports the roster capacity as unknown."})
    if any(s["kind"] == "taxi" for s in slots):
        rows.append({"rule": "Taxi squad", "status": HANDLED,
                     "detail": "Taxi players stay owned and visible but never start in projected, start/sit or waiver "
                               "lineups, and taxi slots are left out of active roster capacity in the trade analyzer and "
                               "waiver planning. A traded taxi player arrives in an active spot."})

    # Projections in the league's scoring.
    skill = resolve_skill_scoring(items or None)
    kicker = resolve_kicker_scoring(items or None)
    dst_ok, dst_unmodeled_rules = dst_unmodeled(items or None)
    if provider == "espn":
        rows.append({"rule": "Projections in this league's scoring", "status": HANDLED,
                     "detail": "ESPN projects every player in this league's own scoring; those numbers are shown "
                               "unmodified by default."})
        ours = "Ours projections (QB, RB, WR, TE)"
    else:
        ours = "Projections (Ours, QB, RB, WR, TE)"
    rows.append({"rule": ours, "status": HANDLED if skill.supported else UNSUPPORTED,
                 "detail": ("Fantasy Intel's own projections are scored with this league's rules."
                            + (" The TE reception bonus is applied." if skill.position_weights.get("TE") else "")
                            if skill.supported else
                            "Fantasy Intel's own projections abstain: the league scores "
                            f"{_reasons(skill.unsupported, sl_unsupported)}, which the model does not implement.")})
    rows.append({"rule": "Kicker projections", "status": HANDLED if kicker.supported else UNSUPPORTED,
                 "detail": "Field goals and extra points are scored with this league's distance rules."
                 if kicker.supported else
                 f"Kicker projections abstain: {_reasons(kicker.unsupported, sl_unsupported)}."})
    rows.append({"rule": "D/ST projections", "status": HANDLED if dst_ok else UNSUPPORTED,
                 "detail": "Sacks, takeaways, touchdowns and points-allowed tiers are scored with this league's rules."
                 if dst_ok else
                 f"D/ST projections abstain: {_reasons(dst_unmodeled_rules, sl_unsupported) or 'the league scores no D/ST statistics'}."})

    te = scoring["te_premium"]["value"]
    if te:
        espn_slot = scoring["te_premium_scope"] == "slot"
        rows.append({"rule": "TE premium", "status": PARTIAL,
                     "detail": ("ESPN's projections include it; " if provider == "espn" else "")
                     + ("Ours does not (ESPN expresses it per lineup slot). " if espn_slot else
                        "Ours applies it. " if skill.supported else "")
                     + "Player ratings and expected points (xFP) are full PPR and ignore it."})
    rp = scoring["points_per_reception"]["value"]
    if rp is not None and rp != 1.0:
        rows.append({"rule": "Points per reception", "status": PARTIAL,
                     "detail": f"League projections use {_g(rp)} per catch. Player ratings and expected points "
                               "(xFP) are full PPR, not this league's scoring."})

    # Lineup lock (app/analytics/start_sit.py: game locks at each team's kickoff).
    if lock["value"] == "individual_game":
        rows.append({"rule": "Lineup lock", "status": HANDLED,
                     "detail": "Start/Sit keeps players whose game has kicked off where they are, as this league does."})
    elif lock["value"] is not None:
        rows.append({"rule": "Lineup lock", "status": UNSUPPORTED,
                     "detail": f"Start/Sit locks each player at his own game's kickoff; this league's rule "
                               f"({lock['display']}) is not applied."})
    else:
        rows.append({"rule": "Lineup lock", "status": UNVERIFIED,
                     "detail": f"Start/Sit locks each player at his own game's kickoff. {name}'s rule for this league is "
                               "not in the synced settings, so that assumption is not verified."})

    # Roster rules (app/analytics/trade_data._position_limits, waiver_roster.roster_constraints).
    limits = roster["position_limits"]["value"]
    if limits:
        rows.append({"rule": "Per-position roster limits", "status": HANDLED,
                     "detail": "Waiver and trade suggestions respect them."})
    rs = settings.get("rosterSettings") if provider == "espn" and isinstance(settings.get("rosterSettings"), dict) else {}
    if rs.get("isUsingUndroppableList"):
        rows.append({"rule": "Undroppable list", "status": UNSUPPORTED,
                     "detail": "Which players are undroppable is not synced, so a suggested drop may be one of them."})
    return rows


# ---- the response -------------------------------------------------------------------------------------------------

def provider_of(settings: dict) -> str:
    return "sleeper" if settings.get("provider") == "sleeper" else "espn"


def build_league_rules(lg: dict) -> dict:
    """The rules page payload from one `leagues` row (all columns)."""
    settings = _json(lg.get("settings_raw"))
    settings = settings if isinstance(settings, dict) else {}
    items = _json(lg.get("scoring_items"))
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    provider = provider_of(settings)
    roster, unmapped = roster_section(_json(lg.get("roster_slots")), settings, provider)
    scoring, more = scoring_section(lg, items, settings, provider)
    unmapped += more
    lock, more = lineup_lock_section(settings, provider)
    unmapped += more
    waivers, more = waivers_section(lg, settings, provider)
    unmapped += more
    unmapped += other_settings(settings, provider)
    synced = lg.get("synced_at")
    size = _int(lg.get("size"))
    ltype = settings.get("type") if provider == "sleeper" else None
    ltype = {0: "Redraft", 1: "Keeper", 2: "Dynasty"}.get(ltype) if _int(ltype) is not None else None
    return {
        "contract": CONTRACT,
        "source": {
            "provider": provider, "provider_label": PROVIDERS[provider],
            "captured_at": synced.isoformat() if isinstance(synced, datetime) else synced,
            "note": f"Read from the {PROVIDERS[provider]} settings saved by the last sync. Nothing on this page is "
                    "fetched live, and nothing here can change the league's settings.",
        },
        "league": {
            "name": lg.get("name") if isinstance(lg.get("name"), str) else None,
            "season": _int(lg.get("season")),
            "team_count": fact(size, str(size)) if size else fact(None, reason="The synced settings do not include "
                                                                              "the number of teams."),
            "league_type": fact(ltype, ltype) if ltype else fact(
                None, reason="Redraft, keeper or dynasty is not in the synced settings."),
        },
        "roster": roster,
        "scoring": scoring,
        "lineup_lock": lock,
        "waivers": waivers,
        "trades": trades_section(lg, settings, provider),
        "playoffs": playoffs_section(lg, settings, provider),
        "engine_support": engine_support(provider, roster, scoring, items, lock, settings),
        "unmapped": unmapped,
    }
