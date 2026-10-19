"""Research only: penalty-erased targets and carries (`ifp-pe`) on top of ifp-2.

Nothing in the app imports this module (tests/test_penalty_erased.py). ifp-1.0.0, ifp-2.0.0, the
model registry, production runs, ledgers, Start/Sit, trades and waivers are untouched, and official
fantasy scoring is never changed: erased plays only enter pregame *features*. See
docs/research/penalty_erased_opportunities.md.

Why. When a penalty nullifies a snap ("- No Play"), the target or carry disappears from the official
stat line (nfl_weekly_stats) and from our play-by-play aggregates (sync.classify_plays requires
play_type 'pass'/'run'), although the play still shows who the offense was trying to get the ball
to. A deep target that draws defensive pass interference (DPI) is the clearest case: the offense
gains the yardage but the receiver is never credited with a target.

Identification. nflverse leaves receiver_player_id / rusher_player_id NULL on no_play rows (only
two-point tries keep them), so the intended receiver or ball carrier is parsed from `desc`
("17-J.Allen pass incomplete deep left to 14-S.Diggs. PENALTY on ...") and resolved by the jersey
number on the offense (`posteam`), with two independent sources:
  * roster: nflverse weekly roster (season, week, team, jersey) -> gsis_id;
  * game:   the same game's official plays, which carry *_player_id and *_jersey_number together.
A play is `confident` when at least one source resolves the jersey, the abbreviated name in `desc`
agrees with the resolved player's surname, and the two sources do not disagree. Anything else is
kept with its failure reason (no_posteam, no_jersey_match, name_mismatch, source_conflict) and never
enters a feature. Only the text before the first "PENALTY on" and after the last replay "REVERSED."
is parsed: that is the play as finally ruled.

Scope (matches classify_plays, so erased and official counts are the same kind of event):
  * erased target: REG, play_type 'no_play', not a two-point try, a pass to a named receiver
    (complete, incomplete or intercepted). A pass with no receiver ("thrown away") is not a target.
  * erased carry: REG no_play designed run or scramble with a named rusher; kneels, spikes,
    aborted snaps and sacks are not carries.
  * DPI is flagged per play (`dpi`); its penalty yards (spot of the foul) stand in for air yards.

Features (games strictly before (S, W); the player's last RECENT_GAMES games, the same window
forecast_features uses, so the adjusted and official quantities differ ONLY by erased plays):
  pe_tgt_pg, pe_car_pg, pe_air_pg, pe_dpi_pg    erased volume per game
  pe_adj_targets_pg / pe_adj_carries_pg / pe_adj_air_yards_pg   official + erased per game
  pe_adj_target_share   (targets + erased) / (team targets + team erased targets)
  pe_adj_rush_share     same for carries (team rush attempts incl. scrambles, as the panel's)
  air_share / pe_adj_air_share   receiving air yards over team air yards, official / adjusted
  pe_rate               erased / (official + erased) targets + carries
A player-game with no erased play is a measured 0 (the game's play-by-play was parsed); a team-week
with no parsed play-by-play is null.

Air yards of an erased non-DPI target are unknown (nflverse leaves air_yards NULL on no_play rows).
They are imputed with fixed constants measured on 2019 official targets (short 4.37, deep 25.11,
no depth given 8.25 air yards). 2019 precedes every training and test week, so the constants carry
no leakage.
"""

from __future__ import annotations

from collections import defaultdict
import re

import numpy as np
import polars as pl

from .forecast_features import POSITIONS, RECENT_GAMES, PRIOR_SEASONS, key

PE_VERSION = "pe-0.1.0"
PE_CHALLENGER_VERSION = "ifp-pe-0.1.0"
PE_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}

AIR_PROXY = {"short": 4.37, "deep": 25.11, None: 8.25}   # 2019 official targets, mean air yards by pass_length
DPI = "Defensive Pass Interference"
SUFFIX = re.compile(r"\s+(?:Jr\.?|Sr\.?|II|III|IV|V)$")
_NAME = r"[A-Z][A-Za-z']*\.\s?[A-Z][A-Za-z'\-]*(?:\. [A-Z][A-Za-z'\-]*)?(?: (?:Jr\.|Sr\.|II|III|IV|V)(?=[ .]))?"
PASS_RE = re.compile(rf"(\d{{1,2}})-({_NAME}) pass(?: (incomplete))?(?: (short|deep))?(?: (left|middle|right))?"
                     rf"(?: (?:to|intended for) (\d{{1,2}})-({_NAME}))?")
RUN_RE = re.compile(rf"(\d{{1,2}})-({_NAME}) (scrambles )?(left end|left tackle|left guard|up the middle|right guard|"
                    rf"right tackle|right end|rushes)")
NOT_A_PLAY = re.compile(r"\b(?:sacked|kneels|spiked)\b")

PLAY_SCHEMA = {
    "game_id": pl.String, "play_id": pl.Float64, "season": pl.Int64, "week": pl.Int64, "team": pl.String,
    "kind": pl.String, "jersey": pl.Int64, "name": pl.String, "scramble": pl.Int8, "depth": pl.String,
    "dpi": pl.Int8, "penalty_side": pl.String, "penalty_type": pl.String, "air_proxy": pl.Float64,
    "gsis_roster": pl.String, "gsis_game": pl.String, "gsis_id": pl.String, "status": pl.String,
    "desc": pl.String,
}
PW_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
             "pe_targets": pl.Int64, "pe_carries": pl.Int64, "pe_air": pl.Float64, "pe_dpi": pl.Int64}
TW_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "team": pl.String, "team_pe_targets": pl.Int64,
             "team_pe_carries": pl.Int64, "team_pe_air": pl.Float64, "team_air": pl.Float64}


# ----------------------------------------------------------------------------- parsing


def final_segment(desc: str) -> str:
    """The play as finally ruled: text after the last replay reversal, up to the first penalty."""
    d = desc or ""
    if "REVERSED." in d:
        d = d.rsplit("REVERSED.", 1)[1]
    return re.split(r"\b(?:PENALTY|Penalty) on\b", d, maxsplit=1)[0]


def parse_desc(desc: str) -> dict | None:
    """The intended ball recipient of a nullified snap, or None when no target/carry was attempted.
    Returns kind 'target' (a pass to a named receiver), 'pass_no_receiver' or 'carry'."""
    seg = final_segment(desc)
    m = PASS_RE.search(seg)
    if m:
        if m.group(6) is None:
            return {"kind": "pass_no_receiver", "jersey": None, "name": None, "scramble": 0, "depth": m.group(4)}
        return {"kind": "target", "jersey": int(m.group(6)), "name": m.group(7), "scramble": 0, "depth": m.group(4)}
    if NOT_A_PLAY.search(seg):
        return None
    m = RUN_RE.search(seg)
    if m:
        return {"kind": "carry", "jersey": int(m.group(1)), "name": m.group(2), "scramble": int(bool(m.group(3))),
                "depth": None}
    return None


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def surname_token(name: str | None) -> str:
    """'J.Smith-Njigba' -> 'smithnjigba', 'A.St. Brown' -> 'st', 'Mi.Wilson' -> 'wilson'."""
    if not name:
        return ""
    return _norm(SUFFIX.sub("", name).split(".", 1)[-1].split(".")[0])


def name_matches(name: str | None, last_name: str | None) -> bool:
    """Abbreviated desc name vs a full surname (prefix rule of app.nfl.pbp_exits, both directions)."""
    s, last = surname_token(name), _norm(SUFFIX.sub("", last_name or ""))
    if not s or not last:
        return False
    return last.startswith(s[:4]) or s.startswith(last[:4])


# ----------------------------------------------------------------------------- derivation


def game_jersey_map(pbp: pl.DataFrame) -> dict[tuple, set[tuple[str, str]]]:
    """(game_id, team, jersey) -> {(gsis_id, abbreviated name)} from the game's official plays."""
    out: dict[tuple, set] = defaultdict(set)
    for role in ("receiver", "rusher", "passer"):
        idc, jc, nc = f"{role}_player_id", f"{role}_jersey_number", f"{role}_player_name"
        if not {idc, jc, nc} <= set(pbp.columns):
            continue
        rows = (pbp.filter(pl.col(idc).is_not_null() & pl.col(jc).is_not_null() & pl.col("posteam").is_not_null())
                .select("game_id", "posteam", pl.col(jc).cast(pl.Int64), idc, nc).unique())
        for g, t, j, i, n in rows.iter_rows():
            out[(g, t, int(j))].add((i, n))
    return out


def roster_map(rosters: pl.DataFrame) -> dict[tuple, list[tuple[str, str]]]:
    """(season, week, team, jersey) -> [(gsis_id, last_name)] from nflverse weekly rosters."""
    out: dict[tuple, list] = defaultdict(list)
    r = rosters.drop_nulls(["jersey_number", "gsis_id", "team"]).select(
        "season", "week", "team", pl.col("jersey_number").cast(pl.Int64), "gsis_id", "last_name").unique()
    for s, w, t, j, g, last in r.iter_rows():
        out[(int(s), int(w), t, int(j))].append((g, last))
    return out


def _resolve(name, roster_hits, game_hits) -> tuple[str | None, str | None, str | None, str]:
    r_ok = [g for g, last in roster_hits if name_matches(name, last)]
    g_ok = sorted({i for i, n in game_hits if name_matches(name, (n or "").split(".", 1)[-1])})
    gr = r_ok[0] if len(set(r_ok)) == 1 else None
    gg = g_ok[0] if len(g_ok) == 1 else None
    if not roster_hits and not game_hits:
        return None, None, None, "no_jersey_match"
    if gr is None and gg is None:
        return None, None, None, "name_mismatch" if (roster_hits or game_hits) else "no_jersey_match"
    if gr is not None and gg is not None and gr != gg:
        return gr, gg, None, "source_conflict"
    return gr, gg, gr or gg, "confident"


def derive(pbp: pl.DataFrame, rosters: pl.DataFrame) -> pl.DataFrame:
    """One row per REG no_play snap with an attempted target, receiver-less pass or carry, with the
    resolved player (null unless `status` == 'confident'). `rosters`: nflverse weekly rosters with
    season, week, team, jersey_number, gsis_id, last_name."""
    if pbp.height == 0:
        return pl.DataFrame(schema=PLAY_SCHEMA)
    p = pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp
    gmap = game_jersey_map(p)
    rmap = roster_map(rosters)
    # nflverse does not always set two_point_attempt on a nullified try: the desc header is checked too
    two = pl.col("desc").fill_null("").str.contains("TWO-POINT CONVERSION ATTEMPT", literal=True)
    if "two_point_attempt" in p.columns:
        two = two | (pl.col("two_point_attempt").fill_null(0) == 1)
    cand = p.filter((pl.col("play_type") == "no_play") & ~two).select(
        "game_id", "play_id", "season", "week", "posteam", "penalty_team", "penalty_type", "penalty_yards", "desc")
    rows = []
    for g, pid, s, w, team, pteam, ptype, pyds, desc in cand.iter_rows():
        parsed = parse_desc(desc)
        if parsed is None:
            continue
        dpi = int(DPI in (desc or ""))
        side = ("offsetting" if "offsetting" in (desc or "") else
                None if pteam is None or team is None else "offense" if pteam == team else "defense")
        rec = {"game_id": g, "play_id": float(pid), "season": int(s), "week": int(w), "team": team,
               "kind": parsed["kind"], "jersey": parsed["jersey"], "name": parsed["name"],
               "scramble": parsed["scramble"], "depth": parsed["depth"], "dpi": dpi, "penalty_side": side,
               "penalty_type": ptype, "air_proxy": None, "gsis_roster": None, "gsis_game": None, "gsis_id": None,
               "status": "not_an_opportunity" if parsed["kind"] == "pass_no_receiver" else None, "desc": desc}
        if parsed["kind"] == "target":
            rec["air_proxy"] = float(pyds) if dpi and ptype == DPI and pyds is not None else AIR_PROXY[parsed["depth"]]
        if rec["status"] is None:
            if team is None:
                rec["status"] = "no_posteam"
            else:
                gr, gg, gid, st = _resolve(parsed["name"], rmap.get((int(s), int(w), team, parsed["jersey"]), []),
                                           gmap.get((g, team, parsed["jersey"]), set()))
                rec.update(gsis_roster=gr, gsis_game=gg, gsis_id=gid if st == "confident" else None, status=st)
        rows.append(rec)
    return pl.DataFrame(rows, schema=PLAY_SCHEMA, orient="row")


def team_air(pbp: pl.DataFrame) -> pl.DataFrame:
    """Official air yards per team-week: targets as classify_plays defines them (REG pass plays with a
    receiver, no sack, no two-point try)."""
    p = pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp
    two = pl.col("two_point_attempt").fill_null(0) == 1
    t = p.filter((pl.col("play_type") == "pass") & (pl.col("pass_attempt").fill_null(0) == 1)
                 & (pl.col("sack").fill_null(0) == 0) & ~two & pl.col("receiver_player_id").is_not_null())
    return (p.filter(pl.col("posteam").is_not_null() & pl.col("play_type").is_in(["pass", "run"]))
            .select("season", "week", pl.col("posteam").alias("team")).unique()
            .join(t.group_by("season", "week", pl.col("posteam").alias("team"))
                  .agg(pl.col("air_yards").fill_null(0).sum().alias("team_air")),
                  on=["season", "week", "team"], how="left")
            .with_columns(pl.col("team_air").fill_null(0.0), pl.col("season").cast(pl.Int64),
                          pl.col("week").cast(pl.Int64)))


def aggregate(plays: pl.DataFrame, team_air_df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Player-week erased volume (confident plays only) and team-week erased volume. Team totals
    count every erased target/carry the offense attempted, resolved or not, so the denominators of
    the adjusted shares are complete; the numerators use only confidently identified plays."""
    opp = plays.filter(pl.col("kind").is_in(["target", "carry"]) & pl.col("team").is_not_null())
    tgt, car = pl.col("kind") == "target", pl.col("kind") == "carry"
    pw = (opp.filter(pl.col("status") == "confident").group_by("season", "week", "gsis_id", "team").agg(
        tgt.sum().cast(pl.Int64).alias("pe_targets"), car.sum().cast(pl.Int64).alias("pe_carries"),
        pl.col("air_proxy").filter(tgt).sum().alias("pe_air"), (tgt & (pl.col("dpi") == 1)).sum().cast(pl.Int64)
        .alias("pe_dpi")))
    tw = opp.group_by("season", "week", "team").agg(
        tgt.sum().cast(pl.Int64).alias("team_pe_targets"), car.sum().cast(pl.Int64).alias("team_pe_carries"),
        pl.col("air_proxy").filter(tgt).sum().alias("team_pe_air"))
    tw = (team_air_df.join(tw, on=["season", "week", "team"], how="left")
          .with_columns(pl.col("team_pe_targets").fill_null(0), pl.col("team_pe_carries").fill_null(0),
                        pl.col("team_pe_air").fill_null(0.0)))
    return pw.cast(PW_SCHEMA).select(list(PW_SCHEMA)), tw.cast(TW_SCHEMA).select(list(TW_SCHEMA))


def validate_on_official(pbp: pl.DataFrame, rosters: pl.DataFrame, max_examples: int = 8) -> dict:
    """Ground truth for the identification step. On official (not nullified) targets and carries
    nflverse supplies the receiver / rusher id, so the same desc parser + resolver is scored against
    it. The game source is leave-one-out: the play's own (id, name) observation is removed from the
    game map, as it is absent for a no_play row. Plays with any penalty, sacks, kneels, spikes and
    two-point tries are excluded (as for erased plays)."""
    p = pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp
    rmap = roster_map(rosters)
    seen: dict[tuple, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))   # (game, team, jersey) -> obs counts
    for role in ("receiver", "rusher", "passer"):
        idc, jc, nc = f"{role}_player_id", f"{role}_jersey_number", f"{role}_player_name"
        for g, t, j, i, n in (p.filter(pl.col(idc).is_not_null() & pl.col(jc).is_not_null()
                                       & pl.col("posteam").is_not_null())
                              .select("game_id", "posteam", pl.col(jc).cast(pl.Int64), idc, nc).iter_rows()):
            seen[(g, t, int(j))][(i, n)] += 1

    def ok(c):
        return pl.col(c).fill_null(0) == 0 if c in p.columns else pl.lit(True)

    off = p.filter(pl.col("play_type").is_in(["pass", "run"]) & ok("two_point_attempt") & ok("penalty")
                   & ok("sack") & ok("qb_kneel") & ok("qb_spike"))
    tally: dict[str, int] = defaultdict(int)
    examples: list[dict] = []
    for g, team, s, w, desc, pt, rid, uid, rj, uj, rn, un in off.select(
            "game_id", "posteam", "season", "week", "desc", "play_type", "receiver_player_id", "rusher_player_id",
            "receiver_jersey_number", "rusher_jersey_number", "receiver_player_name", "rusher_player_name").iter_rows():
        truth, tj, tn = (rid, rj, rn) if pt == "pass" else (uid, uj, un)
        if truth is None:
            continue
        tally["n"] += 1
        parsed = parse_desc(desc)
        if parsed is None or parsed["kind"] not in ("target", "carry"):
            tally["not_parsed"] += 1
            if len(examples) < max_examples:
                examples.append({"result": "not_parsed", "desc": desc[:200]})
            continue
        j = parsed["jersey"]
        own = (truth, tn) if tj is not None and int(tj) == j else None
        game_hits = {obs for obs, c in seen.get((g, team, j), {}).items() if c - (obs == own) > 0}
        gr, gg, gid, st = _resolve(parsed["name"], rmap.get((int(s), int(w), team, j), []), game_hits)
        for src, val in (("roster", gr), ("game", gg)):
            tally[f"{src}_{'unresolved' if val is None else 'correct' if val == truth else 'wrong'}"] += 1
        res = st if st != "confident" else "correct" if gid == truth else "wrong"
        tally[f"combined_{res}"] += 1
        if res != "correct" and len(examples) < max_examples:
            examples.append({"result": res, "desc": desc[:200], "resolved": gid, "truth": truth})
    resolved = tally["combined_correct"] + tally["combined_wrong"]
    out: dict = dict(sorted(tally.items()))
    out["combined_precision"] = round(tally["combined_correct"] / resolved, 5) if resolved else None
    out["combined_recall"] = round(tally["combined_correct"] / tally["n"], 5) if tally["n"] else None
    out["examples"] = examples
    return out


def identification_report(plays: pl.DataFrame) -> dict:
    """How many erased plays get a confident player id, by season and kind, and why the rest fail."""
    opp = plays.filter(pl.col("kind").is_in(["target", "carry"]))
    out = {"plays": plays.height, "opportunities": opp.height,
           "confident_rate": round(float((opp["status"] == "confident").mean()), 4) if opp.height else None,
           "by_status": dict(opp.group_by("status").len().sort("status").iter_rows()),
           "both_sources_agree": int(opp.filter((pl.col("status") == "confident") & pl.col("gsis_roster").is_not_null()
                                                & pl.col("gsis_game").is_not_null()).height),
           "roster_only": int(opp.filter((pl.col("status") == "confident") & pl.col("gsis_game").is_null()).height),
           "game_only": int(opp.filter((pl.col("status") == "confident") & pl.col("gsis_roster").is_null()).height),
           "by_season": {}}
    for (s,), g in opp.group_by("season", maintain_order=True):
        out["by_season"][int(s)] = {
            k: {"n": int((g["kind"] == k).sum()),
                "confident": int(((g["kind"] == k) & (g["status"] == "confident")).sum())}
            for k in ("target", "carry")} | {
            "dpi_targets": int(((g["kind"] == "target") & (g["dpi"] == 1)).sum()),
            "pass_no_receiver": int(plays.filter((pl.col("season") == s) & (pl.col("kind") == "pass_no_receiver")).height)}
    out["by_penalty_side"] = dict(opp.group_by(pl.col("penalty_side").fill_null("unknown")).len().sort("penalty_side")
                                  .iter_rows())
    return out


# ----------------------------------------------------------------------------- features

FEATURE_DOCS: dict[str, str] = {
    "pe_tgt_pg": "Penalty-erased targets per game over the player's last <=5 games (confident ids)",
    "pe_car_pg": "Penalty-erased carries per game (designed runs + scrambles), same window",
    "pe_air_pg": "Imputed air yards of erased targets per game (DPI: penalty yards; else 2019 depth-bucket mean)",
    "pe_dpi_pg": "Erased targets that drew defensive pass interference, per game",
    "pe_adj_targets_pg": "targets_pg + pe_tgt_pg",
    "pe_adj_carries_pg": "carries_pg + pe_car_pg",
    "pe_adj_air_yards_pg": "air_yards_pg + pe_air_pg",
    "pe_adj_target_share": "(targets + erased targets) / (team targets + team erased targets), window games with team data",
    "pe_adj_rush_share": "(carries + erased carries) / (team rush attempts + team erased carries), same games",
    "air_share": "Official receiving air yards / team air yards, same games (reference, not in ifp-2)",
    "pe_adj_air_share": "(air yards + erased air) / (team air + team erased air), same games",
    "pe_rate": "Erased / (official + erased) targets + carries over the window (null without opportunities)",
    "pe_window_opps": "Erased targets + carries over the window (cohort flag, not a feature)",
}
FEATURE_COLUMNS = tuple(FEATURE_DOCS)


def features_for_week(panel: pl.DataFrame, pw: pl.DataFrame, tw: pl.DataFrame, season: int, week: int,
                      targets: pl.DataFrame) -> pl.DataFrame:
    """Penalty-erased features for `targets` (gsis_id) in week (S, W), from games strictly before
    (S, W) in the current and previous season: the same last-RECENT_GAMES window as
    forecast_features.features_for_week. Null when the player has no window game, or (shares) when
    no window game has team data; erased volume is null when no window game was parsed."""
    cutoff = key(season, week)
    p = panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - PRIOR_SEASONS))
    recent = p.sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
    j = (recent.join(pw.drop("team"), on=["season", "week", "gsis_id"], how="left")
         .join(tw, on=["season", "week", "team"], how="left"))
    parsed = pl.col("team_pe_targets").is_not_null()     # the team-game's play-by-play was parsed
    j = j.with_columns([pl.when(parsed).then(pl.col(c).fill_null(0)).otherwise(None).alias(c)
                        for c in ("pe_targets", "pe_carries", "pe_air", "pe_dpi")])
    tg_ok = pl.col("team_targets").is_not_null() & parsed
    ru_ok = pl.col("team_rush_attempts").is_not_null() & parsed
    agg = j.group_by("gsis_id").agg(
        pl.len().alias("_n"), parsed.sum().alias("_np"),
        pl.col("pe_targets").sum().alias("_pt"), pl.col("pe_carries").sum().alias("_pc"),
        pl.col("pe_air").sum().alias("_pa"), pl.col("pe_dpi").sum().alias("_pd"),
        pl.col("targets").mean().alias("_targets_pg"), pl.col("carries").mean().alias("_carries_pg"),
        pl.col("air_yards").mean().alias("_air_pg"),
        pl.col("targets").filter(tg_ok).sum().alias("_tg"), pl.col("pe_targets").filter(tg_ok).sum().alias("_tg_pe"),
        pl.col("team_targets").filter(tg_ok).sum().alias("_ttg"),
        pl.col("team_pe_targets").filter(tg_ok).sum().alias("_ttg_pe"),
        pl.col("carries").filter(ru_ok).sum().alias("_ca"), pl.col("pe_carries").filter(ru_ok).sum().alias("_ca_pe"),
        pl.col("team_rush_attempts").filter(ru_ok).sum().alias("_tru"),
        pl.col("team_pe_carries").filter(ru_ok).sum().alias("_tru_pe"),
        pl.col("air_yards").filter(parsed).sum().alias("_air"), pl.col("pe_air").filter(parsed).sum().alias("_air_pe"),
        pl.col("team_air").filter(parsed).sum().alias("_tair"),
        pl.col("team_pe_air").filter(parsed).sum().alias("_tair_pe"),
        (pl.col("targets") + pl.col("carries")).filter(parsed).sum().alias("_opp"),
    )
    np_ = pl.col("_np")
    has = np_ > 0

    def ratio(num, den):
        return pl.when(has & (den > 0)).then(num / den).otherwise(None)

    feats = agg.with_columns(
        pl.when(has).then(pl.col("_pt") / np_).otherwise(None).alias("pe_tgt_pg"),
        pl.when(has).then(pl.col("_pc") / np_).otherwise(None).alias("pe_car_pg"),
        pl.when(has).then(pl.col("_pa") / np_).otherwise(None).alias("pe_air_pg"),
        pl.when(has).then(pl.col("_pd") / np_).otherwise(None).alias("pe_dpi_pg"),
        ratio(pl.col("_tg") + pl.col("_tg_pe"), pl.col("_ttg") + pl.col("_ttg_pe")).alias("pe_adj_target_share"),
        ratio(pl.col("_ca") + pl.col("_ca_pe"), pl.col("_tru") + pl.col("_tru_pe")).alias("pe_adj_rush_share"),
        ratio(pl.col("_air"), pl.col("_tair")).alias("air_share"),
        ratio(pl.col("_air") + pl.col("_air_pe"), pl.col("_tair") + pl.col("_tair_pe")).alias("pe_adj_air_share"),
        ratio(pl.col("_pt") + pl.col("_pc"), pl.col("_opp") + pl.col("_pt") + pl.col("_pc")).alias("pe_rate"),
        pl.when(has).then(pl.col("_pt") + pl.col("_pc")).otherwise(None).alias("pe_window_opps"),
    ).with_columns(
        # adjusted per-game volume = the official per-game mean (over every window game, as targets_pg)
        # plus erased per game (over the parsed window games)
        (pl.col("_targets_pg") + pl.col("pe_tgt_pg")).alias("pe_adj_targets_pg"),
        (pl.col("_carries_pg") + pl.col("pe_car_pg")).alias("pe_adj_carries_pg"),
        (pl.col("_air_pg") + pl.col("pe_air_pg")).alias("pe_adj_air_yards_pg"),
    )
    out = targets.select("gsis_id").join(feats.select("gsis_id", *FEATURE_COLUMNS), on="gsis_id", how="left")
    return out.with_columns([pl.col(c).cast(pl.Float64).round(10) for c in FEATURE_COLUMNS])


# ----------------------------------------------------------------------------- candidates

# Pre-registered candidates (fixed before any points backtest; constants are never tuned).
#   A ("adjusted"): ifp-2's features with every official opportunity measure replaced by its
#     penalty-adjusted counterpart. No new free parameter: erased plays count exactly like credited
#     ones. This is the direct test of "an erased target is an opportunity".
#   V ("volume"): ifp-2's features plus erased volume, so the ridge may weigh an erased play
#     differently from a credited one (e.g. DPI-heavy receivers).
REPLACE: dict[str, dict[str, str]] = {
    "QB": {},
    "RB": {"targets_pg": "pe_adj_targets_pg", "carries_pg": "pe_adj_carries_pg",
           "target_share": "pe_adj_target_share", "rush_share": "pe_adj_rush_share"},
    "WR": {"targets_pg": "pe_adj_targets_pg", "target_share": "pe_adj_target_share",
           "air_yards_pg": "pe_adj_air_yards_pg"},
    "TE": {"targets_pg": "pe_adj_targets_pg", "target_share": "pe_adj_target_share",
           "air_yards_pg": "pe_adj_air_yards_pg"},
}
ADD: dict[str, list[str]] = {
    "QB": [],
    "RB": ["pe_car_pg", "pe_tgt_pg", "pe_rate"],
    "WR": ["pe_tgt_pg", "pe_air_pg", "pe_rate"],
    "TE": ["pe_tgt_pg", "pe_air_pg", "pe_rate"],
}
CANDIDATES = ("A", "V")
EVAL_POSITIONS = ("RB", "WR", "TE")       # QB keeps ifp-2: the question is about receivers and ball carriers

DECISION_RULES = {
    "selection": "development 2021-2022 only, per position (RB, WR, TE): a candidate qualifies if MAE(cand) - "
                 "MAE(ifp2) <= -0.010 with a week-clustered bootstrap 95% CI upper bound < 0 and |bias| not worse "
                 "by more than 0.15. Among qualifying candidates the lower development MAE is adopted; with none the "
                 "position keeps ifp2. QB always keeps ifp2. The composite of the per-position choices is 'pe'.",
    "confirmation": "2023-2024, run once, frozen composite vs ifp2 on identical rows, adopted positions only: (1) pooled "
                    "MAE diff < 0 with CI upper < 0; (2) no adopted position significantly worse (CI lower > 0); "
                    "(3) pooled RMSE not worse by > 0.5%; (4) |bias| not worse by > 0.15; (5) starters-pool MAE not "
                    "worse by > 1%; (6) Start/Sit pair accuracy not lower by > 0.5 pp; (7) mechanism: the MAE "
                    "difference is < 0 on each adopted position's rows with >= 2 erased opportunities in the window "
                    "(pe_heavy).",
    "outcome": "all pass -> 'adopt_candidate' (a recommendation to pre-register a frozen pregame shadow ledger; it "
               "changes nothing by itself); any fail, or nothing adopted -> 'retain_baseline'.",
    "exploratory": "2025-2026 replay is reported only (already inspected by earlier ifp-2 work).",
    "descriptive": "trailing baselines A1 (last-5 average) and A2 (season average), and ESPN on rows with a pregame "
                   "ESPN snapshot (2026 only), are reported on the same rows; they select nothing.",
    "never": "no promotion, no registry change, no change to official scoring; historical replays never establish "
             "promotion readiness",
}
SELECTION_MAX_DIFF = -0.010
BIAS_TOLERANCE = 0.15
HEAVY_OPPS = 2
MECHANISM_COHORT = "pe_heavy"


def candidate_features(base: list[str], position: str, candidate: str | None) -> list[str]:
    """ifp-2's features for the position, transformed by candidate 'A' (replace) or 'V' (add)."""
    feats = list(base)
    if candidate == "A":
        feats = [REPLACE[position].get(f, f) for f in feats]
    elif candidate == "V":
        feats += [f for f in ADD[position] if f not in feats]
    elif candidate is not None:
        raise ValueError(candidate)
    return feats


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg,
                 per_position: dict[str, str] | None = None) -> pl.DataFrame:
    """pred_ifp2, pred_A and pred_V per store row of `weeks` (null = abstention: no recent game or
    fewer than MIN_TRAIN_ROWS training rows). `per_position` adds pred_pe (the composite). Training
    rows strictly earlier (asserted). ifp-2 itself is fitted with challenger.fit_linear on its frozen
    spec, so pred_ifp2 equals challenger.challenger_predictions (tested)."""
    from .challenger import fit_linear, train_rows
    from .independent_projections import MIN_TRAIN_ROWS

    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            spec = cfg.positions[pos]
            fitted: dict[str, np.ndarray] = {}
            for tag in ("ifp2", *CANDIDATES):
                cand = None if tag == "ifp2" or pos not in EVAL_POSITIONS else tag
                if tag != "ifp2" and cand is None:
                    fitted[tag] = fitted["ifp2"]
                    continue
                if train.height < MIN_TRAIN_ROWS:
                    fitted[tag] = np.full(rows.height, np.nan)
                    continue
                m = fit_linear(train, pos, candidate_features(cfg.features(pos), pos, cand), spec["lam"],
                               half_life_weeks=spec.get("half_life_weeks"), target_key=k, name=f"pe_{tag}")
                fitted[tag] = np.where(ok, m.predict(rows), np.nan)
            cols = {f"pred_{t}": fitted[t] for t in ("ifp2", *CANDIDATES)}
            if per_position is not None:
                cols["pred_pe"] = fitted[per_position.get(pos, "ifp2")]
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def cohorts() -> dict[str, pl.Expr]:
    """Pregame cohorts: how much erased volume the player's window had, plus the reference cohorts."""
    from .forecast_backtest import starters_pool_expr

    opps = pl.col("pe_window_opps")
    return {
        "pe_none": opps == 0,
        "pe_any": opps >= 1,
        "pe_heavy": opps >= HEAVY_OPPS,
        "pe_rate_10": pl.col("pe_rate") >= 0.10,    # descriptive only
        "pe_dpi": pl.col("pe_dpi_pg") > 0,
        "pe_share_up_2pp": (pl.col("pe_adj_target_share") - pl.col("target_share")) >= 0.02,
        "starters_pool": starters_pool_expr(),
        "early_or_low_sample": (pl.col("week") <= 4) | (pl.col("games_recent") < 3),
    }


__all__ = ["PE_VERSION", "PE_CHALLENGER_VERSION", "PE_STATUS", "FEATURE_DOCS", "DECISION_RULES", "REPLACE", "ADD",
           "parse_desc", "derive", "team_air", "aggregate", "identification_report", "features_for_week",
           "candidate_features", "walk_forward", "cohorts"]
