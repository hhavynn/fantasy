"""ESPN -> nflverse player id mapping."""

from app.nfl.mapping import NflCandidate, NflIndex, map_player, normalize_name


def make_index():
    idx = NflIndex()
    idx.add_candidate(NflCandidate("00-001", "Kenneth Walker III", "RB", "SEA"))
    idx.add_candidate(NflCandidate("00-002", "Mike Williams", "WR", "NYJ"))
    idx.add_candidate(NflCandidate("00-003", "Mike Williams", "WR", "PIT"))
    idx.add_candidate(NflCandidate("00-004", "Josh Allen", "QB", "BUF"))
    idx.add_candidate(NflCandidate("00-005", "Josh Allen", "LB", "JAX"))
    idx.add_candidate(NflCandidate("00-006", "Amon-Ra St. Brown", "WR", "DET"))
    idx.add_crosswalk("4567", "00-004", "rosters_weekly")
    return idx


def test_normalize_name_strips_suffixes_punctuation_accents():
    assert normalize_name("Kenneth Walker III") == "kenneth walker"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert normalize_name("José  Ramírez Jr.") == "jose ramirez"


def test_espn_id_crosswalk_wins_and_is_high_confidence():
    m = map_player({"espn_player_id": 4567, "full_name": "Josh Allen", "position": "QB", "pro_team": "BUF"}, make_index())
    assert m["gsis_id"] == "00-004" and m["match_method"] == "espn_id:rosters_weekly"
    assert m["match_confidence"] == "high"


def test_name_fallback_requires_compatible_position():
    m = map_player({"espn_player_id": 1, "full_name": "Kenneth Walker", "position": "RB", "pro_team": "SEA"}, make_index())
    assert m["gsis_id"] == "00-001" and m["match_confidence"] == "medium"
    # Same name, wrong position family -> no match (the LB Josh Allen is never used for a QB)
    m = map_player({"espn_player_id": 2, "full_name": "Josh Allen", "position": "TE", "pro_team": "BUF"}, make_index())
    assert m["match_method"] == "unmatched" and m["gsis_id"] is None


def test_ambiguous_names_resolved_by_team_or_left_unmatched():
    idx = make_index()
    m = map_player({"espn_player_id": 3, "full_name": "Mike Williams", "position": "WR", "pro_team": "PIT"}, idx)
    assert m["gsis_id"] == "00-003"
    m = map_player({"espn_player_id": 4, "full_name": "Mike Williams", "position": "WR", "pro_team": None}, idx)
    assert m["match_method"] == "unmatched" and "Ambiguous" in m["notes"]


def test_team_defense_is_not_a_player():
    m = map_player({"espn_player_id": -16012, "full_name": "Chiefs D/ST", "position": "D/ST", "pro_team": "KC"}, make_index())
    assert m["match_method"] == "team_defense" and m["gsis_id"] is None and m["nfl_team"] == "KC"


def test_conflicting_crosswalks_are_noted_not_hidden():
    idx = make_index()
    idx.add_crosswalk(4567, "00-999", "ff_playerids")
    m = map_player({"espn_player_id": 4567, "full_name": "Josh Allen", "position": "QB", "pro_team": "BUF"}, idx)
    assert m["gsis_id"] == "00-004" and "Conflicting" in m["notes"]


def test_unmatched_rows_are_returned_not_dropped():
    m = map_player({"espn_player_id": 77, "full_name": "Nobody Atall", "position": "WR", "pro_team": "KC"}, make_index())
    assert m["espn_player_id"] == 77 and m["match_method"] == "unmatched"
