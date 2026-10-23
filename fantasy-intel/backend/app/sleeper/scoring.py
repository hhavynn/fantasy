"""Sleeper `scoring_settings` -> ESPN-shaped scoring items (`{statId, points, pointsOverrides}`), the shape every
scoring resolver reads (skill players, kickers, D/ST). Stored in `leagues.scoring_items` of a Sleeper league file,
so fingerprints, Fantasy Intel's own projections and every consumer work unchanged.

Rules:
  * a key with a value of 0 is ignored (Sleeper lists many rules at 0);
  * offensive rules use ESPN's statIds (or the skill resolver's synthetic ids for first downs, attempts and
    completions); positional reception bonuses (`bonus_rec_te|rb|wr`) become `positionPoints` on statId 53;
  * kicker rules are base points on kicker statIds (the skill resolver lists them as kicker-only) and
    D/ST rules D/ST-slot overrides (slot 16) with base points 0, exactly as ESPN expresses them, so no resolver ever
    scores them for the wrong position;
  * the D/ST's special-teams fumbles (`def_st_ff`, `def_st_fum_rec`) are inside ESPN's D/ST counts (106, 96): a key
    worth the same as its defensive key is covered by it; a different value is approximated by the defensive key's
    and returned in `notes`. A player's special-teams fumble events (`st_ff`, `st_fum_rec`) are not projected
    (notes);
  * Sleeper's finer FG buckets and its points-allowed tiers use the synthetic ids kicker_scoring and dst_research
    define for them (never ESPN ids);
  * a non-zero key no family implements is returned in `unsupported`, never dropped: the family it belongs to then
    resolves as unsupported (see SKILL_UNSUPPORTED / KICKER_UNSUPPORTED / DST_UNSUPPORTED), or, for a key that
    belongs to no family we project (IDP), it is listed and ignored.
"""

from __future__ import annotations

from typing import Mapping

DST = "16"

# offensive: Sleeper key -> ESPN (or EXTRA_DIRECT) statIds that each take the key's points
SKILL: dict[str, tuple[int, ...]] = {
    "pass_yd": (3,), "pass_td": (4,), "pass_2pt": (19,), "pass_int": (20,),
    "rush_yd": (24,), "rush_td": (25,), "rush_2pt": (26,),
    "rec_yd": (42,), "rec_td": (43,), "rec_2pt": (44,), "rec": (53,),
    "fum_rec_td": (63,), "fum_lost": (72,),
    "st_td": (101, 102),          # a player's kick or punt return TD
    "pass_fd": (2001,), "rush_fd": (2002,), "rec_fd": (2003,),
    "pass_att": (2004,), "pass_cmp": (2005,), "rush_att": (2006,),
}
POSITION_BONUS = {"bonus_rec_te": "TE", "bonus_rec_rb": "RB", "bonus_rec_wr": "WR"}
# kicker: Sleeper key -> statIds (kicker_scoring.FG_ITEMS / PAT_ITEMS, synthetic 1080-1085 for the short buckets;
# KICKER_UNSUPPORTED_ID is an unimplemented kicker rule)
KICKER: dict[str, tuple[int, ...]] = {
    "fgm_0_19": (1080,), "fgm_20_29": (1081,), "fgm_30_39": (1082,), "fgm_40_49": (77,), "fgm_50p": (74,),
    "fgm_50_59": (198,), "fgm_60p": (201,), "fgm": (83,),
    "fgmiss_0_19": (1083,), "fgmiss_20_29": (1084,), "fgmiss_30_39": (1085,), "fgmiss_40_49": (79,),
    "fgmiss_50p": (76,), "fgmiss_50_59": (200,), "fgmiss_60p": (203,), "fgmiss": (85,), "fga": (84,),
    "xpm": (86,), "xpmiss": (88,), "xpa": (87,),
}
# D/ST: Sleeper key -> statIds (dst_research: ESPN's, plus synthetic 1089-1095 for Sleeper's points-allowed tiers)
DEFENSE: dict[str, tuple[int, ...]] = {
    "sack": (99,), "def_sack": (99,), "int": (95,), "fum_rec": (96,), "ff": (1106,), "safe": (98,), "blk_kick": (97,),
    "def_td": (103, 104),                 # interception and fumble return TDs
    "def_st_td": (101, 102, 93),          # the D/ST's kick, punt and blocked-kick return TDs
    "def_kr_td": (101,), "def_pr_td": (102,), "def_2pt": (206,),
    "pts_allow_0": (1089,), "pts_allow_1_6": (1090,), "pts_allow_7_13": (1091,), "pts_allow_14_20": (1092,),
    "pts_allow_21_27": (1093,), "pts_allow_28_34": (1094,), "pts_allow_35p": (1095,),
    "yds_allow_0_100": (128,), "yds_allow_100_199": (129,), "yds_allow_200_299": (130,),
    "yds_allow_300_349": (131,), "yds_allow_350_399": (132,), "yds_allow_400_449": (133,),
    "yds_allow_450_499": (134,), "yds_allow_500_549": (135,), "yds_allow_550p": (136,),
}
# Rules the resolvers must see as "awards points but is not implemented", so the family abstains rather than
# silently scoring without them: one statId per key, outside every implemented set.
SKILL_UNSUPPORTED_ID, KICKER_UNSUPPORTED_ID, DST_UNSUPPORTED_ID = 2999, 1099, 137
DEFENSE_PREFIXES = ("def_", "pts_allow", "yds_allow", "tkl", "qb_hit", "blk_kick_ret")
KICKER_PREFIXES = ("fgm", "fgmiss", "fga", "xp")
IDP_PREFIXES = ("idp_",)


ST_FUMBLES = {"def_st_ff": "ff", "def_st_fum_rec": "fum_rec"}     # ESPN's D/ST counts include special teams
PLAYER_ST = ("st_ff", "st_fum_rec")


def unsupported_family(key: str) -> str:
    """The family an unsupported Sleeper key makes abstain, by the same rule `to_scoring_items` applies: "idp" (listed
    and ignored), "kicker", "dst" or "skill". Lets a family's diagnostic name only its own keys (#254)."""
    if key.startswith(IDP_PREFIXES):
        return "idp"
    if key.startswith(KICKER_PREFIXES):
        return "kicker"
    if key.startswith(DEFENSE_PREFIXES):
        return "dst"
    return "skill"


def to_scoring_items(settings: Mapping[str, float]) -> tuple[list[dict], list[str], list[str]]:
    """(ESPN-shaped items, the non-zero keys no family implements, notes on approximations). See the module
    docstring."""
    items: list[dict] = []
    unsupported: list[str] = []
    notes: list[str] = []

    def add(sid: int, points: float, slot: str | None = None, position: dict | None = None) -> None:
        item = {"statId": sid, "points": 0.0 if slot else points, "pointsOverrides": {slot: points} if slot else {}}
        if position:
            item["positionPoints"] = position
        items.append(item)

    for key, raw in sorted(settings.items()):
        value = float(raw or 0.0)
        if not value:
            continue
        if key in SKILL:
            for sid in SKILL[key]:
                add(sid, value)
        elif key in POSITION_BONUS:
            add(53, 0.0, position={POSITION_BONUS[key]: value})
        elif key in KICKER:
            for sid in KICKER[key]:
                add(sid, value)
        elif key in ST_FUMBLES:
            base = float(settings.get(ST_FUMBLES[key]) or 0.0)
            if base != value:
                notes.append(f"{key} ({value:g}) is approximated by {ST_FUMBLES[key]} ({base:g}): the D/ST statistics "
                             "count special-teams and defensive fumbles together")
        elif key in PLAYER_ST:
            notes.append(f"{key}: a player's special-teams fumble events are not projected")
        elif key in DEFENSE:
            for sid in DEFENSE[key]:
                add(sid, value, DST)
            if key == "ff":
                notes.append("ff: the D/ST's forced fumbles are estimated from its expected fumble recoveries "
                             "(not a validated part of the D/ST model)")
        elif key.startswith(IDP_PREFIXES):
            unsupported.append(key)           # individual defensive players: not a lineup slot we project
        else:
            unsupported.append(key)
            if key.startswith(KICKER_PREFIXES):
                add(KICKER_UNSUPPORTED_ID, value)
            elif key.startswith(DEFENSE_PREFIXES):
                add(DST_UNSUPPORTED_ID, value, DST)
            else:
                add(SKILL_UNSUPPORTED_ID, value)
    return items, unsupported, notes
