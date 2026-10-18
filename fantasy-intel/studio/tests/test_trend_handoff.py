"""Social trend handoff: `make-episode --trend <package>` (package written by `trends select`)."""

import json

from studio import cli
from studio.core import write_json
from studio.stages import review

PACKAGE = {
    "contract_version": "social-intel/1.0",
    "idea": {"idea_id": "abc123", "working_title": "The Internet Is Wrong About Cam Skattebo",
             "format": "internet_vs_data", "trend_score": 71.2, "research_confidence": 0.8,
             "relevance_window": "until Sunday kickoff", "why_trending": "r/fantasyfootball thread, 85 comments/hour",
             "editorial_angle": "Crowd says bust; carries say otherwise.", "hooks": ["82%. That's the share."],
             "opposing_args": ["Two games is a small sample."], "source_links": ["https://www.reddit.com/r/x/1"]},
    "studio_handoff": {"week": 7, "player_slug": "cam-skattebo", "gsis_id": "00-0040002", "role": "riser",
                       "alternates": [{"slug": "tyrone-tracy-jr", "gsis_id": "00-0039000"}]},
}


def test_review_shows_trend_context_only_when_present(episode):
    assert review.trend_context(episode) == []
    write_json(episode.path("trend_brief.json"), PACKAGE)
    text = "\n".join(review.trend_context(episode))
    assert "not narrated" in text and "abc123" in text and "https://www.reddit.com/r/x/1" in text


def test_trend_flag_builds_the_lead_players_short(tmp_path, monkeypatch):
    pkg = tmp_path / "abc123.json"
    pkg.write_text(json.dumps(PACKAGE))
    seen = {}

    def fake_make_shorts(cfg, season, week, force, only, *rest, **kw):
        seen.update(week=week, only=only, trend=rest[-1], alternates=kw.get("alternates"))
        return 0

    monkeypatch.setattr(cli, "make_shorts", fake_make_shorts)
    assert cli.main(["--week", "7", "--trend", str(pkg)]) == 0
    assert seen == {"week": 7, "only": "cam-skattebo", "trend": pkg.resolve(), "alternates": ["tyrone-tracy-jr"]}


def test_trend_without_lead_player_is_a_manual_brief(tmp_path):
    pkg = tmp_path / "x.json"
    pkg.write_text(json.dumps({**PACKAGE, "studio_handoff": {"week": 7, "player_slug": None}}))
    assert cli.main(["--week", "7", "--trend", str(pkg)]) == 2
