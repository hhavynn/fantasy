"""Kicker projection engine (kfp-1.1.0): scoring, PBP tables, venues, weather, model, live runs, storage, consumers.

Synthetic, deterministic fixtures only (tests/synthetic_kicker.py); no test makes a network request: every weather
call goes through an injected http_get.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from app.analytics import kicker_backtest as kb
from app.analytics import kicker_live as kl
from app.analytics import kicker_model as km
from app.analytics import kicker_store as ks
from app.analytics import kicker_venues as kv
from app.analytics import kicker_weather as kw
from app.analytics.kicker_scoring import (box_score_kicks, kicking_points, reconcile_with_espn,
                                          resolve_kicker_scoring)
from app.analytics.league_scoring import resolve_skill_scoring
from app.nfl.kicking_pbp import derive_kicks, derive_team_drives, drive_reconciliation

import synthetic_kicker as sk

LEAGUE = [{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0}, {"statId": 198, "points": 5.0},
          {"statId": 201, "points": 6.0}, {"statId": 85, "points": -1.0}, {"statId": 86, "points": 1.0},
          {"statId": 206, "points": 2.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 89, "points": 0.0, "pointsOverrides": {"16": 5.0}}, {"statId": 53, "points": 1.0}]


# ----------------------------------------------------------------------------- scoring


def test_fg_distance_boundaries_misses_blocks_and_pats():
    s = resolve_kicker_scoring(LEAGUE).require_supported()
    assert [s.fg_points(d, "made") for d in (18, 39, 40, 49, 50, 59, 60, 66)] == [3, 3, 4, 4, 5, 5, 6, 6]
    assert s.fg_points(45, "missed") == -1 and s.fg_points(45, "blocked") == -1    # blocked counts as a miss
    assert s.pat_points("made") == 1 and s.pat_points("missed") == 0 and s.pat_points("blocked") == 0
    assert s.fingerprint.startswith("k:")
    # 206 carries a base value a K slot could technically earn, 53 is an offensive stat: listed, never projected
    assert {u["stat_id"] for u in s.unmodeled} == {206, 53}
    # a D/ST-only item (base 0) is ignored, never an unsupported rule
    assert not s.unsupported and any(i["stat_id"] == 89 for i in s.ignored)


def test_blocked_rule_changes_the_fingerprint():
    a = resolve_kicker_scoring(LEAGUE)
    b = resolve_kicker_scoring(LEAGUE, blocked_counts_as_miss=False)
    assert b.fg_points(45, "blocked") == 0 and a.fingerprint != b.fingerprint


def test_overlapping_items_are_additive_and_flagged():
    s = resolve_kicker_scoring(LEAGUE + [{"statId": 74, "points": 1.0}])
    assert s.fg_points(55, "made") == 6 and s.fg_points(61, "made") == 7 and s.fg_points(49, "made") == 4
    assert s.notes and "50+" in s.notes[0]


def test_attempt_items_apply_to_every_outcome_and_k_slot_override_wins():
    s = resolve_kicker_scoring([{"statId": 84, "points": 0.5}, {"statId": 83, "points": 3.0},
                                {"statId": 86, "points": 1.0, "pointsOverrides": {"17": 2.0}}])
    assert s.fg_points(30, "made") == 3.5 and s.fg_points(30, "missed") == 0.5 and s.fg_points(30, "blocked") == 0.5
    assert s.pat_points("made") == 2.0


def test_unknown_kicker_rule_makes_scoring_unsupported():
    s = resolve_kicker_scoring(LEAGUE + [{"statId": 214, "points": 0.1}])
    assert not s.supported
    with pytest.raises(ValueError):
        s.require_supported()
    assert not resolve_kicker_scoring(None).supported
    assert not resolve_kicker_scoring([{"statId": 86, "points": 1.0}]).supported     # no FG points at all


def test_kicker_scoring_never_changes_the_skill_fingerprint():
    base = [i for i in LEAGUE if i["statId"] not in (80, 77, 198, 201, 85, 86)]
    assert resolve_skill_scoring(base).fingerprint == resolve_skill_scoring(LEAGUE).fingerprint


def test_box_score_points_and_inconsistent_lists_are_unknown_not_zero():
    s = resolve_kicker_scoring(LEAGUE)
    row = {"fg_made": 2, "fg_missed": 1, "fg_blocked": 1, "fg_made_list": "38;52", "fg_missed_list": "47",
           "fg_blocked_list": "33", "pat_made": 3, "pat_att": 4, "pat_missed": 1, "pat_blocked": 0}
    assert kicking_points(row, s) == 3 + 5 - 1 - 1 + 3
    assert box_score_kicks({**row, "fg_made": 3}) is None and kicking_points({**row, "fg_made": 3}, s) is None
    assert kicking_points({"fg_made": 0, "fg_missed": 0, "fg_blocked": 0, "pat_att": 0}, s) == 0.0   # a measured zero


def test_reconciliation_against_espn(con):
    con.execute("INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period, scoring_items, "
                "synced_at) VALUES (99, 2026, 'S', 3, 17, ?, now())", [json.dumps(LEAGUE)])
    con.execute("CREATE TABLE nfl_weekly_stats (player_id VARCHAR, season INT, week INT, season_type VARCHAR, "
                "fg_made INT, fg_missed INT, fg_blocked INT, fg_made_list VARCHAR, fg_missed_list VARCHAR, "
                "fg_blocked_list VARCHAR, pat_made INT, pat_att INT, pat_missed INT, pat_blocked INT, receptions INT)")
    con.execute("INSERT INTO nfl_weekly_stats VALUES ('K-1', 2026, 1, 'REG', 2, 0, 1, '41;51', NULL, '30', 2, 2, 0, 0, 0)")
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position) VALUES (1, 'K', 'K')")
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (1, 'K-1', 'high')")
    con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, 1, 1, 10.0, 8.0, now())")
    s = resolve_kicker_scoring(LEAGUE)
    rep = reconcile_with_espn(con, 2026, s, LEAGUE)
    assert rep["status"] == "verified" and rep["n"] == 1 and rep["exact"] == 1
    alt = reconcile_with_espn(con, 2026, resolve_kicker_scoring(LEAGUE, blocked_counts_as_miss=False), LEAGUE)
    assert alt["status"] == "mismatch" and alt["mismatches"][0]["computed"] == 11.0


# ----------------------------------------------------------------------------- play-by-play tables


def _pbp() -> pl.DataFrame:
    """One game: ARI drive 1 (kickoff + 3 plays -> FG made 42), NO drive 2 (TD + PAT good), ARI drive 3
    (punt), NO drive 4 (blocked FG), plus a penalty-nullified FG try (no result) that must not count."""
    base = {"season_type": "REG", "game_id": "G1", "week": 1, "home_team": "NO", "away_team": "ARI", "location": "Home",
            "roof": "dome", "surface": "turf", "temp": None, "wind": None, "stadium_id": "NOR00", "epa": 0.1,
            "success": 1.0, "interception": 0, "fumble_lost": 0, "two_point_conv_result": None, "qtr": 1,
            "half_seconds_remaining": 900, "score_differential": 0, "kicker_player_name": None, "kick_distance": None,
            "field_goal_result": None, "extra_point_result": None, "kicker_player_id": None, "down": 1}
    rows = [
        dict(base, play_id=1, posteam="ARI", defteam="NO", play_type="kickoff", fixed_drive=1, yardline_100=None,
             kicker_player_id="K-NO", fixed_drive_result="Field goal", posteam_score_post=0),
        dict(base, play_id=2, posteam="ARI", defteam="NO", play_type="pass", fixed_drive=1, yardline_100=75,
             fixed_drive_result="Field goal", posteam_score_post=0),
        dict(base, play_id=3, posteam="ARI", defteam="NO", play_type="run", fixed_drive=1, yardline_100=30, down=4,
             fixed_drive_result="Field goal", posteam_score_post=0),
        dict(base, play_id=4, posteam="ARI", defteam="NO", play_type="no_play", fixed_drive=1, yardline_100=24,
             fixed_drive_result="Field goal", posteam_score_post=0, kicker_player_id="K-ARI", kick_distance=42.0),
        dict(base, play_id=5, posteam="ARI", defteam="NO", play_type="field_goal", fixed_drive=1, yardline_100=24,
             down=4, fixed_drive_result="Field goal", field_goal_result="made", kick_distance=42.0,
             kicker_player_id="K-ARI", posteam_score_post=3),
        dict(base, play_id=6, posteam="NO", defteam="ARI", play_type="run", fixed_drive=2, yardline_100=15,
             fixed_drive_result="Touchdown", posteam_score_post=6),
        dict(base, play_id=7, posteam="NO", defteam="ARI", play_type="extra_point", fixed_drive=2, yardline_100=15,
             fixed_drive_result="Touchdown", extra_point_result="good", kick_distance=33.0, kicker_player_id="K-NO",
             posteam_score_post=7),
        dict(base, play_id=8, posteam="ARI", defteam="NO", play_type="punt", fixed_drive=3, yardline_100=70, down=4,
             fixed_drive_result="Punt", posteam_score_post=3),
        dict(base, play_id=9, posteam="NO", defteam="ARI", play_type="field_goal", fixed_drive=4, yardline_100=33,
             down=4, fixed_drive_result="Missed field goal", field_goal_result="blocked", kick_distance=51.0,
             kicker_player_id="K-NO", posteam_score_post=7),
    ]
    return pl.DataFrame(rows, infer_schema_length=None)


def test_kick_rows_drop_nullified_tries_and_keep_blocks():
    kicks = derive_kicks(_pbp(), 2025)
    assert [(k["posteam"], k["kick_type"], k["result"], k["kick_distance"]) for k in kicks] == [
        ("ARI", "FG", "made", 42.0), ("NO", "PAT", "made", 33.0), ("NO", "FG", "blocked", 51.0)]


def test_drive_segmentation_and_opportunity_counts():
    drives = {r["team"]: r for r in derive_team_drives(_pbp(), 2025)}
    ari, no = drives["ARI"], drives["NO"]
    assert ari["drives"] == 2 and ari["fg_drives"] == 1 and ari["punt_drives"] == 1      # the kickoff is no drive
    assert ari["fg_range_drives"] == 1 and ari["rz_drives"] == 0 and ari["avg_start_yardline"] == (75 + 70) / 2
    assert no["td_drives"] == 1 and no["rz_td_drives"] == 1 and no["fg_att"] == 1 and no["pat_att"] == 1
    assert ari["fg_att"] == 1 and ari["fourth_go_in_range"] == 1 and ari["fourth_in_range"] == 2
    assert ari["kickoff_kicker_id"] is None and no["kickoff_kicker_id"] == "K-NO"    # kickoffs belong to the kicking team
    assert ari["game_kicker_id"] == "K-ARI" and no["game_kicker_id"] == "K-NO"
    rec = drive_reconciliation(derive_kicks(_pbp(), 2025), list(drives.values()))
    assert rec["agree_rate"] == 1.0


def test_game_kicker_falls_back_to_the_kickoff_kicker():
    p = _pbp().filter(pl.col("play_id") != 7).filter(pl.col("play_id") != 9)      # NO tried no FG or PAT
    no = next(r for r in derive_team_drives(p, 2025) if r["team"] == "NO")
    assert no["kicker_id"] is None and no["game_kicker_id"] == "K-NO" and no["fg_att"] == 0


# ----------------------------------------------------------------------------- venues


def test_venue_overrides_unknowns_and_roof_rules():
    reg = kv.registry()
    london = kv.resolve({"game_id": "2025_05_MIN_CLE", "season": 2025, "stadium_id": "CLE00", "roof": "outdoors",
                         "location": "Neutral"}, reg)
    assert london["venue_id"] == "LON02" and london["source"] == "override" and london["neutral"]
    paris = kv.resolve({"game_id": "2026_07_PIT_NO", "season": 2026, "stadium_id": "PAR00", "roof": "dome"}, reg)
    assert paris["roof_class"] == "open_air" and paris["outdoor"] and any("disagrees" in n for n in paris["notes"])
    atl = kv.resolve({"game_id": "x", "season": 2026, "stadium_id": "ATL97", "roof": "closed"}, reg)
    assert atl["roof_status"] == "retractable_unknown" and atl["outdoor"] is False
    assert kv.observed_roof_status(atl, "closed") == "retractable_closed"
    unknown = kv.resolve({"game_id": "y", "season": 2026, "stadium_id": "ZZZ99", "stadium": "Somewhere"}, reg)
    assert unknown["venue_id"] is None and unknown["outdoor"] is None and unknown["lat"] is None
    moved = kv.resolve({"game_id": "z", "season": 2021, "stadium_id": "OAK00"}, reg)     # Raiders left in 2020
    assert any("registered for seasons" in n for n in moved["notes"])
    denver = kv.resolve({"game_id": "d", "season": 2026, "stadium_id": "DEN00"}, reg)
    assert denver["elevation_m"] >= 1500


def test_every_registry_entry_is_well_formed():
    reg = kv.registry()
    for vid, v in reg["venues"].items():
        assert -90 <= v["lat"] <= 90 and -180 <= v["lon"] <= 180 and v["elevation_m"] >= 0, vid
        assert v["country"] and v["tz"], vid


# ----------------------------------------------------------------------------- weather


def _hourly(start: datetime, hours: int, wind="10 to 15 mph", temp=40):
    return {"properties": {"updateTime": "2026-10-20T10:00:00+00:00", "periods": [
        {"startTime": (start + timedelta(hours=h)).isoformat(), "endTime": (start + timedelta(hours=h + 1)).isoformat(),
         "temperature": temp, "temperatureUnit": "F", "windSpeed": wind, "windDirection": "NW",
         "probabilityOfPrecipitation": {"value": 20 + h}, "relativeHumidity": {"value": 60}, "shortForecast": "Breezy"}
        for h in range(hours)]}}


KICK = datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)


def test_window_sampling_and_units():
    hourly = _hourly(KICK - timedelta(hours=5), 12)
    grid = {"properties": {"windGust": {"values": [{"validTime": f"{(KICK - timedelta(hours=1)).isoformat()}/PT2H",
                                                    "value": 40.0}]},
                           "quantitativePrecipitation": {"values": [{"validTime": f"{KICK.isoformat()}/PT6H", "value": 6.0}]}}}
    v = kw.summarize(hourly, grid, KICK)
    assert v["periods"] == 4                                   # kickoff + 3.25 h only, never the whole day
    assert v["wind_mph_mean"] == 12.5 and v["wind_mph_max"] == 15
    assert v["gust_mph_max"] == round(40 * 0.621371, 1)        # km/h -> mph
    assert v["precip_mm_total"] == round(6.0 * 3.25 / 6, 1)    # prorated by the overlapping share
    assert v["precip_prob_max"] == 28 and v["temp_f_mean"] == 40.0
    with pytest.raises(kw.WeatherUnavailable):
        kw.summarize(_hourly(KICK + timedelta(days=2), 3), None, KICK)


OUTDOOR = {"venue_id": "GNB00", "roof_class": "open_air", "lat": 44.5, "lon": -88.06, "country": "US"}
GAME = {"game_id": "2026_07_A_GB"}


class FakeNWS:
    def __init__(self, fail=0, hourly=None):
        self.calls, self.fail, self.hourly = [], fail, hourly

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            self.fail -= 1
            raise ConnectionError("503")
        if "/points/" in url:
            return {"properties": {"forecastHourly": "https://nws/hourly", "forecastGridData": "https://nws/grid"}}
        if url.endswith("hourly"):
            return self.hourly or _hourly(KICK - timedelta(hours=2), 10)
        return {"properties": {}}


def test_forecast_fetch_cache_and_snapshot(con):
    now = KICK - timedelta(days=2)
    nws = FakeNWS()
    got = kw.game_forecast(con, GAME, OUTDOOR, KICK, now, http_get=nws, sleep=lambda s: None)
    assert got["status"] == "ok" and got["values"]["wind_mph_mean"] == 12.5 and len(nws.calls) == 3
    again = kw.game_forecast(con, GAME, OUTDOOR, KICK, now + timedelta(hours=1), http_get=nws, sleep=lambda s: None)
    assert again["status"] == "ok" and len(nws.calls) == 3 and again["age_hours"] == 1.0     # cached, not refetched
    row = con.execute("SELECT provider, status, kickoff_utc, window_end_utc, issued_at FROM weather_forecasts").fetchall()
    assert row == [("nws", "ok", KICK.replace(tzinfo=None), (KICK + timedelta(hours=3.25)).replace(tzinfo=None),
                    datetime(2026, 10, 20, 10, 0))]


def test_transient_errors_are_retried(con):
    nws = FakeNWS(fail=2)
    got = kw.game_forecast(con, GAME, OUTDOOR, KICK, KICK - timedelta(days=1), http_get=nws, sleep=lambda s: None)
    assert got["status"] == "ok"


def test_outage_returns_stale_forecast_or_unavailable_never_raises(con):
    now = KICK - timedelta(days=2)
    kw.game_forecast(con, GAME, OUTDOOR, KICK, now, http_get=FakeNWS(), sleep=lambda s: None)
    down = FakeNWS(fail=99)
    stale = kw.game_forecast(con, GAME, OUTDOOR, KICK, now + timedelta(hours=8), http_get=down, sleep=lambda s: None)
    assert stale["status"] == "stale" and stale["stale"] and stale["values"]["wind_mph_mean"] == 12.5
    gone = kw.game_forecast(con, {"game_id": "other"}, OUTDOOR, KICK, now, http_get=down, sleep=lambda s: None)
    assert gone["status"] == "unavailable" and gone["values"] is None
    statuses = [r[0] for r in con.execute("SELECT status FROM weather_forecasts ORDER BY retrieved_at").fetchall()]
    assert statuses.count("error") == 2 and statuses.count("ok") == 1       # failures are logged, nothing overwritten


def test_no_fetch_after_kickoff_indoors_abroad_or_beyond_range(con):
    def boom(url):
        raise AssertionError(f"unexpected request {url}")
    after = kw.game_forecast(con, GAME, OUTDOOR, KICK, KICK + timedelta(minutes=1), http_get=boom)
    assert after["status"] == "unavailable" and "kickoff has passed" in after["reason"]
    assert kw.game_forecast(con, GAME, {**OUTDOOR, "roof_class": "fixed"}, KICK, KICK - timedelta(days=1),
                            http_get=boom)["status"] == "not_applicable"
    assert kw.game_forecast(con, GAME, {**OUTDOOR, "roof_class": "retractable"}, KICK, KICK - timedelta(days=1),
                            http_get=boom)["status"] == "not_applicable"
    assert kw.game_forecast(con, GAME, {**OUTDOOR, "country": "GB"}, KICK, KICK - timedelta(days=1),
                            http_get=boom)["status"] == "unavailable"
    far = kw.game_forecast(con, GAME, OUTDOOR, KICK, KICK - timedelta(days=9), http_get=boom)
    assert far["status"] == "unavailable" and "forecast range" in far["reason"]
    assert con.execute("SELECT count(*) FROM weather_forecasts").fetchone()[0] == 0


def test_kickoff_times_are_eastern_including_overseas_games():
    from app.analytics.projection_backtest import _kickoff

    assert _kickoff({"gameday": "2026-10-25", "gametime": "09:30"}) == datetime(2026, 10, 25, 13, 30, tzinfo=timezone.utc)
    assert _kickoff({"gameday": "2026-11-15", "gametime": "09:30"}) == datetime(2026, 11, 15, 14, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- model


@pytest.fixture(scope="module")
def world():
    return sk.world()


@pytest.fixture(scope="module")
def scoring():
    return resolve_kicker_scoring(sk.scoring_items())


def test_conversion_is_smooth_and_monotone_in_distance(world, scoring):
    data = kb.build_dataset(world, scoring, kb.PRODUCTION)
    conv = km.fit_conversion(data["conv"].filter(pl.col("season") < 2026), kb.PRODUCTION)
    p = conv.p_make(np.arange(18, 66, dtype=float), {}, 0.0)
    assert np.all(np.diff(p) < 0) and np.max(np.abs(np.diff(p))) < 0.06        # no jump at 39/40 or 49/50
    assert conv.p_make(np.array([45.0]), {}, 0.5)[0] > conv.p_make(np.array([45.0]), {}, 0.0)[0]


def test_small_samples_shrink_toward_league_average(world, scoring):
    data = kb.build_dataset(world, scoring, kb.PRODUCTION)
    model = km.fit_conversion(data["conv"].filter(pl.col("season") < 2026), kb.PRODUCTION)
    hot = pl.DataFrame([{"kicker_player_id": "NEW", "season": 2026, "week": 1, "kick_type": "FG", "kick_distance": 55.0,
                         "y": 1.0, **{c: 0.0 for c in ("fixed", "retractable", "elev_x_dist", "wind_x_dist", "cold_x_dist")}}] * 2)
    vet = pl.DataFrame([{"kicker_player_id": "VET", "season": 2026, "week": 1, "kick_type": "FG", "kick_distance": 55.0,
                         "y": float(i < 30), **{c: 0.0 for c in ("fixed", "retractable", "elev_x_dist", "wind_x_dist",
                                                                 "cold_x_dist")}} for i in range(40)])
    skill = {r["kicker_player_id"]: r["k_skill"] for r in
             km.kicker_skill(pl.concat([hot, vet]), model, 2026, kb.PRODUCTION).filter(pl.col("week") == 2).iter_rows(named=True)}
    assert 0 < skill["NEW"] < skill["VET"] < 1.0      # 2/2 from 55 yards is not better than 30/40 from 55 yards


def test_point_in_time_features_ignore_the_target_week_and_later(world, scoring):
    """Rewriting week-10 (and later) 2025 results leaves every week-10 feature, kicker skill and prediction input
    unchanged."""
    cfg = kb.PRODUCTION
    a = kb.build_dataset(world, scoring, cfg)
    later = (pl.col("season") == 2025) & (pl.col("week") >= 10)
    teams = world["teams"].with_columns(pl.when(later).then(pl.col("pat_att") + 5).otherwise(pl.col("pat_att")).alias("pat_att"),
                                        pl.when(later).then(pl.col("fg_att") + 3).otherwise(pl.col("fg_att")).alias("fg_att"))
    kicks = world["kicks"].with_columns(pl.when(later).then(pl.lit("missed")).otherwise(pl.col("result")).alias("result"))
    b = kb.build_dataset({**world, "teams": teams, "kicks": kicks}, scoring, cfg)
    cols = [c for g in cfg.groups for c in km.LAMBDA_FEATURES[g]] + ["k_pts_season", "k_pts_l4"]
    pick = lambda d: d["rows"].filter((pl.col("season") == 2025) & (pl.col("week") == 10)).sort("team").select(cols)
    assert pick(a).equals(pick(b))
    model = km.fit_conversion(a["conv"].filter(pl.col("season") < 2025), cfg)
    sa = km.kicker_skill(a["conv"], model, 2025, cfg).filter(pl.col("week") == 10).sort("kicker_player_id")
    sb = km.kicker_skill(b["conv"], model, 2025, cfg).filter(pl.col("week") == 10).sort("kicker_player_id")
    assert sa.equals(sb)


def test_expected_points_and_stat_line_are_consistent(world, scoring):
    art = sk.artifact(world, scoring)
    zero = km.expected_kicking(art.conversion, scoring, 0.0, 0.0, 0.0, 0.2, {})
    assert zero["points"] == 0.0 and zero["stat_line"]["fg_att"] == 0
    e = km.expected_kicking(art.conversion, scoring, 2.0, 3.0, 0.0, 0.2, {})
    line = e["stat_line"]
    assert abs(sum(line["fg_att_by_distance"].values()) - 2.0) < 1e-2
    assert abs(sum(line["fg_made_by_distance"].values()) - line["fg_made"]) < 1e-2
    assert 0.8 < line["p_pat"] < 1.0 and e["points"] > 0
    more_tds = km.expected_kicking(art.conversion, scoring, 2.0, 4.0, 0.0, 0.2, {})["points"]
    assert more_tds - e["points"] == pytest.approx(line["points_per_pat_try"], abs=1e-3)


def test_artifact_roundtrip(world, scoring):
    art = sk.artifact(world, scoring)
    back = km.KickerArtifact.from_dict(json.loads(json.dumps(art.to_dict())))
    e1 = km.expected_kicking(art.conversion, scoring, 2.0, 2.5, 0.1, 0.25, {"fixed": 1.0})
    e2 = km.expected_kicking(back.conversion, scoring, 2.0, 2.5, 0.1, 0.25, {"fixed": 1.0})
    assert e1 == e2 and back.cfg == art.cfg
    with pytest.raises(ValueError):
        km.KickerArtifact.from_dict({**art.to_dict(), "schema": "other"})


# ----------------------------------------------------------------------------- live runs


@pytest.fixture
def live(con, world, scoring):
    sk.database(con, world)
    return con, world, sk.artifact(world, scoring)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)      # before week 4 (2026-09-14 synthetic dates are past:
                                                             # weather is never fetched for these games)


def _build(live, week=4, **kw_):
    con, w, art = live
    kw_.setdefault("http_get", lambda url: (_ for _ in ()).throw(AssertionError(f"network: {url}")))
    return kl.build_run(con, w, art, 2026, week, artifact_sha256="a" * 64, now=NOW, **kw_)


def test_coverage_statuses_and_starter_changes(live):
    meta, rows = _build(live)
    by = {r["espn_player_id"]: r for r in rows}
    assert by[101]["coverage_status"] == "projected" and by[101]["projected_points"] > 0
    assert by[102]["coverage_status"] == "withheld_unavailable"              # ESPN OUT
    assert by[104]["coverage_status"] == "no_nfl_team"                       # released: never given DDD's chances
    assert by[105]["coverage_status"] == "projected" and by[105]["nfl_team"] == "DDD"
    assert by[106]["coverage_status"] == "no_nfl_team"                       # free agent
    assert by[107]["coverage_status"] == "uncertain_id_mapping"
    assert by[103]["coverage_status"] == "no_nfl_team"                       # K-C released for week 4
    h = by[108]                                                              # K-H: CCC's new kicker, no kick yet
    assert h["coverage_status"] == "projected"
    assert any("new kicker" in f for f in h["flags"]) and any("cold start" in f for f in h["flags"])
    assert h["context"]["kicker"]["skill_logit"] == 0.0
    for r in rows:
        if r["coverage_status"] != "projected":
            assert r["projected_points"] is None and r["expected_points"] is None
        else:
            assert r["expected_points"] == round(r["p_play"] * r["projected_points"], 2)
            assert r["stat_line"]["fg_att"] > 0 and r["explanation"][0]["feature"] == "headline"
    assert meta["scoring_fingerprint"].startswith("k:") and meta["scoring"]["supported"]


def test_explanations_are_model_counterfactuals(live):
    con, w, art = live
    _, rows = _build(live)
    r = next(x for x in rows if x["espn_player_id"] == 101)
    venue = next(e for e in r["explanation"] if e["feature"] == "venue")
    assert "Ford Field" in venue["text"] and "indoors" in venue["text"]
    assert all(e["contribution"] is not None for e in r["explanation"][1:])
    assert not any(e["feature"] == "weather_forecast" for e in r["explanation"])     # no forecast fetched: no claim


def test_bye_and_rest_of_season_rows(live):
    meta, rows = kl.build_run(live[0], live[1], live[2], 2026, 5, artifact_sha256="a" * 64, now=NOW,
                              projected_from_week=4, http_get=lambda u: (_ for _ in ()).throw(AssertionError(u)))
    by = {r["espn_player_id"]: r for r in rows}
    assert by[105]["coverage_status"] == "bye" and by[108]["coverage_status"] == "bye"
    assert by[101]["coverage_status"] == "projected" and meta["forecast_horizon"] == 1
    assert any("rest-of-season kicker forecast" in f for f in by[101]["flags"])
    assert by[101]["context"]["weather"]["status"] == "not_applicable"


def test_unsupported_league_scoring_abstains_for_everyone(con, world, scoring):
    sk.database(con, world, items=sk.scoring_items([{"statId": 214, "points": 0.1}]))
    _, rows = kl.build_run(con, world, sk.artifact(world, scoring), 2026, 4, artifact_sha256="a" * 64, now=NOW)
    assert {r["coverage_status"] for r in rows} == {"unsupported_scoring"}
    assert all(r["projected_points"] is None for r in rows)


def test_runs_are_repeatable(live):
    m1, r1 = _build(live)
    m2, r2 = _build(live)
    assert m1["run_id"] != m2["run_id"] and ks.content_sha256(m1, r1) == ks.content_sha256(m2, r2)


def test_weather_outage_never_breaks_a_run_and_weather_is_not_a_model_input(live):
    con, w, art = live
    con.execute("UPDATE nfl_schedules SET gameday = '2026-09-22' WHERE season = 2026 AND week = 4")
    con.execute("UPDATE nfl_schedules SET stadium_id = 'GNB00' WHERE season = 2026 AND week = 4 AND home_team = 'AAA'")
    w = {**w, "schedule": w["schedule"].with_columns(
        pl.when((pl.col("season") == 2026) & (pl.col("week") == 4)).then(pl.lit("2026-09-22")).otherwise(pl.col("gameday"))
          .alias("gameday"),
        pl.when((pl.col("season") == 2026) & (pl.col("week") == 4) & (pl.col("home_team") == "AAA")).then(pl.lit("GNB00"))
          .otherwise(pl.col("stadium_id")).alias("stadium_id"))}
    live = (con, w, art)
    _, quiet = _build(live, fetch_weather=False)

    def down(url):
        raise ConnectionError("NWS down")
    _, broken = _build(live, http_get=down)
    nws = FakeNWS(hourly=_hourly(datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc), 10, wind="25 mph"))
    _, windy = _build(live, http_get=nws)
    pts = lambda rows: {r["espn_player_id"]: r["projected_points"] for r in rows}
    assert pts(quiet) == pts(broken) == pts(windy)         # kfp-1.1.0 does not use weather
    outdoor = [r for r in windy if r["coverage_status"] == "projected" and r["context"]["venue"]["roof"] == "open_air"]
    assert outdoor and all(r["context"]["weather"]["status"] in ("ok", "stale") for r in outdoor)
    for r in outdoor:
        assert r["context"]["weather"]["used_by_model"] is False
        assert any("not a model input" in e["text"] for e in r["explanation"] if e["feature"] == "weather_forecast")
    assert len(nws.calls) == 3                              # one game in range, fetched once, then cached


# ----------------------------------------------------------------------------- storage and consumers


def test_write_once_integrity_and_gate(live):
    con = live[0]
    meta, rows = _build(live)
    first = ks.write_run(con, meta, rows)
    assert first["created"] and ks.write_run(con, meta, rows)["created"] is False
    meta2, rows2 = _build(live)
    assert ks.write_run(con, meta2, rows2) == {**first, "created": False}      # identical rerun: the stored run
    with pytest.raises(ks.ImmutableRunError):
        ks.write_run(con, meta, [{**rows[0], "projected_points": 99.0}, *rows[1:]])
    sel = ks.select_run(con, 2026, 4)
    assert sel["run"]["run_id"] == meta["run_id"] and sel["run"]["artifact_check"] == "unverified"
    ks.register_artifact(con, model_version="kfp-1.1.0", artifact_sha256="b" * 64, target_season=2026,
                         training_cutoff_key=202599, artifact_created_at=None)
    sel = ks.select_run(con, 2026, 4)
    assert sel["run"] is None and "not registered" in sel["skipped"][0]["reason"]
    ks.register_artifact(con, model_version="kfp-1.1.0", artifact_sha256="a" * 64, target_season=2026,
                         training_cutoff_key=202599, artifact_created_at=None)
    assert ks.select_run(con, 2026, 4)["run"]["artifact_check"] == "verified"
    con.execute("UPDATE kicker_projection_rows SET projected_points = projected_points + 1 "
                "WHERE coverage_status = 'projected'")
    sel = ks.select_run(con, 2026, 4)
    assert sel["run"] is None and "content_sha256" in sel["skipped"][0]["reason"]
    with pytest.raises(ValueError):
        ks.register_artifact(con, model_version="ifp-2.0.0", artifact_sha256="c" * 64, target_season=2026,
                             training_cutoff_key=None, artifact_created_at=None)


def test_overlay_values_and_zero_statuses(live):
    con = live[0]
    meta, rows = _build(live)
    ks.write_run(con, meta, rows)
    o = ks.overlay(con, 2026, 4)
    assert o["origin"][101] == "ours" and o["values"][101] == next(r["expected_points"] for r in rows
                                                                   if r["espn_player_id"] == 101)
    assert o["origin"][102] == "ours_zero" and o["values"][102] == 0.0         # withheld
    assert 104 not in o["values"] and 107 not in o["values"]                   # no estimate: ESPN keeps them


def test_kicker_api(live):
    from app.api.kicker_projections import build_response

    con = live[0]
    assert build_response(con, 4, None)["available"] is False
    meta, rows = _build(live)
    ks.write_run(con, meta, rows)
    body = build_response(con, 4, None)
    assert body["available"] and body["run"]["run_id"] == meta["run_id"] and body["run"]["scoring"]["supported"]
    top = body["players"][0]
    assert top["independent"]["coverage_status"] == "projected" and top["independent"]["stat_line"]["fg_att"] > 0
    assert top["espn"]["projected_points"] is not None


def test_kicker_rows_never_touch_the_skill_projection_tables(live):
    con = live[0]
    meta, rows = _build(live)
    ks.write_run(con, meta, rows)
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0] == 0


def test_artifact_file_sha_is_the_run_identity(tmp_path, world, scoring):
    art = sk.artifact(world, scoring)
    p = tmp_path / "a.json"
    p.write_text(json.dumps(art.to_dict()))
    assert kl.artifact_sha(p) == hashlib.sha256(p.read_bytes()).hexdigest()
    assert kl.load_artifact(p).version == "kfp-1.1.0"


def test_backfill_script_replaces_only_its_seasons(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import sync_kicking_pbp as skp

    db = tmp_path / "h.duckdb"
    first = skp.run(db, [2025], loader=lambda s: _pbp())
    assert first["seasons"][2025] == {"kicks": 3, "team_games": 2,
                                      "drive_reconciliation": first["seasons"][2025]["drive_reconciliation"]}
    again = skp.run(db, [2025], loader=lambda s: _pbp())            # idempotent: the season is replaced, not appended
    from app.db.connection import connect
    c = connect(db, read_only=True)
    assert c.execute("SELECT count(*) FROM nfl_kick_attempts").fetchone()[0] == 3
    assert c.execute("SELECT count(*) FROM nfl_team_game_drives").fetchone()[0] == 2
    assert c.execute("SELECT status FROM sync_runs WHERE source = 'kicking_pbp' ORDER BY started_at DESC LIMIT 1"
                     ).fetchone()[0] == "ok"
    c.close()
    assert again["seasons"][2025]["kicks"] == 3


def test_register_command_approves_an_existing_artifact_idempotently(tmp_path, monkeypatch):
    """Codex review on #96: an artifact fitted without --register (or a database restored apart from the model
    directory) must be approvable without refitting; the learning cycle runs `register` when the file exists."""
    import argparse
    import importlib.util
    from pathlib import Path

    from app.db.connection import connect, init_db

    script = Path(__file__).resolve().parents[2] / "scripts" / "kicker_projections.py"
    monkeypatch.syspath_prepend(str(script.parent))                     # the CLI imports scripts/_bootstrap
    spec = importlib.util.spec_from_file_location("kicker_projections_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    db = tmp_path / "live.duckdb"
    c = connect(db)
    init_db(c)
    c.close()
    out = tmp_path / "kicker"
    out.mkdir()
    art = out / f"{km.MODEL_VERSION}_2026.json"
    art.write_text(json.dumps({"version": km.MODEL_VERSION, "created_at": "2026-09-01T00:00:00+00:00",
                               "data_cutoff_key": 202599}))
    args = argparse.Namespace(season=2026, live_db=db, out=out, artifact=None)
    first = cli.cmd_register(args)
    assert first["registered"] is True and first["sha256"] == hashlib.sha256(art.read_bytes()).hexdigest()
    assert cli.cmd_register(args)["registered"] is False                 # approving it again creates nothing
    c = connect(db, read_only=True)
    assert c.execute("SELECT model_version, artifact_sha256, target_season, training_cutoff_key FROM approved_artifacts"
                     ).fetchall() == [(km.MODEL_VERSION, first["sha256"], 2026, 202599)]
    c.close()
    art.write_text(json.dumps({"version": "ifp-2.0.0"}))
    with pytest.raises(SystemExit):
        cli.cmd_register(args)
    with pytest.raises(SystemExit):
        cli.cmd_register(argparse.Namespace(season=2027, live_db=db, out=out, artifact=None))


def test_line_table_splits_the_total_by_the_home_favored_spread():
    sched = pl.DataFrame({"game_id": ["g1", "g2"], "home_team": ["KC", "NYG"], "away_team": ["LV", "DAL"],
                          "spread_line": [7.0, None], "total_line": [45.0, 41.5]})
    t = {(r["game_id"], r["team"]): r["o_implied"] for r in km.line_table(sched).iter_rows(named=True)}
    assert t == {("g1", "KC"): 26.0, ("g1", "LV"): 19.0}           # no spread, no implied total
    assert km.line_table(sched.drop("spread_line")).height == 0      # a schedule without the line columns


def test_retired_kfp_runs_serve_weeks_without_a_current_run(live):
    from app.analytics import forward_ours as fo
    from app.api.kicker_projections import build_response

    con = live[0]
    assert fo.KICKER_VERSION == ks.MODEL_VERSION and ks.CONSUMER_VERSIONS == (ks.MODEL_VERSION, "kfp-1.0.0")
    meta, rows = _build(live)
    old = {**meta, "run_id": "kfp-old-1-0", "model_version": "kfp-1.0.0"}
    ks.write_run(con, old, rows)
    # only a kfp-1.0.0 run (2026 weeks 1-6): it is served, under its own version, not ESPN's fallback
    body = build_response(con, 4, None)
    assert body["run"]["run_id"] == "kfp-old-1-0" and body["governance"]["model_version"] == "kfp-1.0.0"
    assert {p["independent"]["model_version"] for p in body["players"]} == {"kfp-1.0.0"}
    assert ks.overlay(con, 2026, 4)["run"]["model_version"] == "kfp-1.0.0"
    # each version is gated by its own artifacts: a registered 1.0 artifact that is not this run's rejects it
    ks.register_artifact(con, model_version="kfp-1.0.0", artifact_sha256="e" * 64, target_season=2026,
                         training_cutoff_key=202599, artifact_created_at=None)
    sel = ks.select_run(con, 2026, 4)
    assert sel["run"] is None and "not registered" in sel["skipped"][0]["reason"]
    ks.register_artifact(con, model_version="kfp-1.0.0", artifact_sha256="a" * 64, target_season=2026,
                         training_cutoff_key=202599, artifact_created_at=None)
    assert ks.select_run(con, 2026, 4)["run"]["artifact_check"] == "verified"
    # both versions stored for the week: kfp-1.1.0 wins, even though the 1.0 run is newer
    ks.write_run(con, {**meta, "created_at": "2026-09-01 00:00:00"}, rows)
    sel = ks.select_run(con, 2026, 4)
    assert sel["run"]["model_version"] == ks.MODEL_VERSION and sel["run"]["run_id"] == meta["run_id"]
    assert build_response(con, 4, None)["governance"]["model_version"] == ks.MODEL_VERSION


def test_fit_re_evaluates_a_report_from_another_model_version(tmp_path, monkeypatch):
    import argparse
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "kicker_projections.py"
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location("kicker_projections_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def report(version, median, config=None):
        r = {"model_version": version, "generated_at": f"{version}-report", "league_median_points": median,
             "test": {"common_support": {}}, "prospective": {}, "ablations_test": {}}
        return {**r, "config": config} if config is not None else r

    out = tmp_path / "kicker"
    out.mkdir()
    path = out / "kfp_evaluation.json"
    path.write_text(json.dumps(report("kfp-1.0.0", 7.6)))           # left by the previous model (an upgrade)
    calls = []

    def evaluate(args):
        calls.append(args)
        path.write_text(json.dumps(report(km.MODEL_VERSION, 8.0, kb.cfg_dict(kb.PRODUCTION))))

    monkeypatch.setattr(cli, "cmd_evaluate", evaluate)
    args = argparse.Namespace(out=out)
    ev = cli.artifact_evaluation(args, kb.PRODUCTION)
    assert len(calls) == 1 and ev["league_median_points"] == 8.0 and ev["report_generated_at"] == "kfp-1.1.0-report"
    assert cli.artifact_evaluation(args, kb.PRODUCTION)["league_median_points"] == 8.0 and len(calls) == 1  # current
    # same version, different frozen config: also re-derived
    assert cli.report_is_stale(report(km.MODEL_VERSION, 8.0, {**kb.cfg_dict(kb.PRODUCTION), "team_k": 9.0}),
                               kb.PRODUCTION)
    # an evaluate that still leaves a stale report: refuse rather than embed it
    monkeypatch.setattr(cli, "cmd_evaluate", lambda a: path.write_text(json.dumps(report("kfp-1.0.0", 7.6))))
    path.write_text(json.dumps(report("kfp-1.0.0", 7.6)))
    with pytest.raises(SystemExit):
        cli.artifact_evaluation(args, kb.PRODUCTION)
