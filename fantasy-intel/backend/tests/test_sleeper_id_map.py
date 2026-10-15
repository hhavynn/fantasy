"""Sleeper player id -> gsis_id: ID-map parsing (int sleeper_id column), team defenses, conflicts,
and unmatched players reported rather than dropped."""

import json

import polars as pl
import pytest

from app.sleeper.id_map import ID_MAP_FILE, SleeperIdMap, load_id_map, map_players
from app.sleeper.league import rostered_player_ids
from app.sleeper.models import parse_players, parse_rosters
from sleeper_fixtures import FIXTURES, Clock, raw


@pytest.fixture
def frame():
    # pl.read_csv infers types like nflreadpy does: sleeper_id becomes Int64.
    return pl.read_csv(FIXTURES / "ff_playerids.csv")


@pytest.fixture
def report(frame):
    rosters = parse_rosters(raw("rosters.json"))
    return map_players(rostered_player_ids(rosters), parse_players(raw("players.json")),
                       SleeperIdMap.from_frame(frame), log_unmatched=False)


def test_inferred_int_sleeper_ids_become_string_keys(frame):
    assert frame.schema["sleeper_id"] == pl.Int64
    idmap = SleeperIdMap.from_frame(frame)
    assert all(isinstance(k, str) for k in idmap.by_sleeper)
    assert "4046" in idmap.by_sleeper and 4046 not in idmap.by_sleeper
    assert idmap.by_sleeper["4046"][0].team == "KC"                    # DynastyProcess KCC -> nflverse
    assert len(idmap) == 10                     # 12 rows with a sleeper_id, 2 duplicated; blank one skipped


def test_from_rows_accepts_ints_or_strings():
    idmap = SleeperIdMap.from_rows([{"sleeper_id": 96, "gsis_id": "00-1"}, {"sleeper_id": "97", "gsis_id": None}])
    assert set(idmap.by_sleeper) == {"96", "97"}


def test_methods(report):
    m = report.mappings
    assert (m["4046"].method, m["4046"].gsis_id, m["4046"].confidence) == ("ff_playerids", "00-0033873", "high")
    assert m["6794"].gsis_id == "00-0035700"                          # Sleeper has no gsis; map does
    assert m["7564"].notes is None                                    # padded Sleeper gsis agrees
    assert m["8130"].gsis_id == "00-0036100" and "differs" in m["8130"].notes
    assert m["6801"].method == "ff_playerids" and m["6801"].gsis_id == "00-0035800"   # duplicate rows agree
    assert (m["9509"].method, m["9509"].gsis_id, m["9509"].confidence) == ("sleeper_gsis", "00-0038000", "medium")


def test_team_defenses_map_to_teams(report):
    det, lar = report.mappings["DET"], report.mappings["LAR"]
    assert (det.method, det.nfl_team, det.gsis_id) == ("team_defense", "DET", None)
    assert (lar.method, lar.nfl_team) == ("team_defense", "LA")
    assert det.matched and lar.matched


def test_unmatched_players_are_reported_not_dropped(report):
    rostered = rostered_player_ids(parse_rosters(raw("rosters.json")))
    assert set(report.mappings) == set(rostered) and len(rostered) == 15
    unmatched = {m.sleeper_id: m for m in report.unmatched}
    assert set(unmatched) == {"11632", "10229", "2133", "99999"}
    assert unmatched["11632"].method == "unmatched" and "Not in the DynastyProcess" in unmatched["11632"].notes
    assert "without a gsis_id" in unmatched["10229"].notes
    assert unmatched["2133"].method == "conflict" and "00-0031000" in unmatched["2133"].notes
    assert unmatched["99999"].method == "unknown_player"
    assert unmatched["11632"].name == "Rookie Runner" and unmatched["11632"].team == "TB"


def test_match_rate_and_counts(report):
    assert report.match_rate == pytest.approx(11 / 15)
    assert report.by_method == {"ff_playerids": 8, "sleeper_gsis": 1, "team_defense": 2, "conflict": 1,
                                "unmatched": 2, "unknown_player": 1}
    assert report.gsis_ids()["4046"] == "00-0033873" and "DET" not in report.gsis_ids()


def test_unmatched_are_logged(frame, caplog):
    with caplog.at_level("WARNING", logger="sleeper.id_map"):
        map_players(["11632"], parse_players(raw("players.json")), SleeperIdMap.from_frame(frame))
    assert "UNMATCHED sleeper 11632" in caplog.text


def test_empty_input():
    assert map_players([], {}, SleeperIdMap()).match_rate is None


def test_load_id_map_cached_for_a_day(tmp_path, frame):
    clock, calls = Clock(), []

    def loader():
        calls.append(1)
        return frame

    a = load_id_map(tmp_path, loader=loader, clock=clock)
    clock.t += 24 * 3600 - 1
    b = load_id_map(tmp_path, loader=loader, clock=clock)
    assert len(calls) == 1 and a.by_sleeper == b.by_sleeper
    doc = json.loads((tmp_path / ID_MAP_FILE).read_text())
    assert all(isinstance(r["sleeper_id"], str) for r in doc["rows"])
    clock.t += 2
    load_id_map(tmp_path, loader=loader, clock=clock)
    assert len(calls) == 2
