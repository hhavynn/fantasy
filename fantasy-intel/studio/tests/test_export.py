"""Upload bundles: only a QC-passed Short gets one, each caption fits its platform, nothing can publish."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from studio.core import Config, Episode, read_json, sha256_file, write_json
from studio.stages import export

DESCRIPTION = ("Jalon Daniels vs the Steelers, Week 6: a passing afternoon that came apart. Final: 19-26 (lost).\n"
               "Every stat is from nflverse game data; the jokes are jokes.\n\n"
               "Narrated with an AI voice (a synthetic version of the creator's own voice).\n"
               "Data: nflverse (CC BY 4.0): game stats, play-by-play and schedules, through the game shown.\n\n"
               "#shorts #nfl #football")


def video(path: Path, size: str = "1080x1920", seconds: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s={size}:r=30:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True)


@pytest.fixture
def short(tmp_path, monkeypatch) -> Episode:
    monkeypatch.setenv("STUDIO_EPISODES", str(tmp_path / "episodes"))
    cfg = Config.load()
    cfg.raw["graphics"] = {"style": "original"}
    ep = Episode(2026, 7, "short", cfg, log=lambda *_: None, slug="flame-jalon-daniels")
    video(ep.path("final.mp4"))
    write_json(ep.path("metadata.json"), {
        "episode_id": ep.id, "title": "The NFL v. Jalon Daniels, Week 6", "description": DESCRIPTION,
        "tags": ["shorts", "nfl", "Jalon Daniels"], "category_id": "17", "privacy_status": "private",
        "self_declared_made_for_kids": False, "contains_synthetic_media": True})
    write_json(ep.path("facts.json"), {"players": [{"name": "Jalon Daniels", "team": "TB"}]})
    write_json(ep.path("story.json"), {"order": 90})
    write_json(ep.path("script.json"), {"structure": {"angle": "flame"}, "shots": [{"type": "cx_order"}]})
    write_json(ep.path("audio/timings.json"), {"segments": [{"id": "s1", "duration": 1.5}, {"id": "s2", "duration": 1.5}]})
    write_json(ep.path("qc.json"), {"passed": True, "files": {"final.mp4": sha256_file(ep.path("final.mp4"))}})
    return ep


def post(ep: Episode, platform: str) -> dict:
    return read_json(ep.path(f"upload/{platform}/post.json"))


def test_a_passed_short_gets_one_bundle_per_platform(short):
    export.run(short)
    for platform in ("tiktok", "reels", "shorts"):
        d = short.path(f"upload/{platform}")
        assert sha256_file(d / "video.mp4") == sha256_file(short.path("final.mp4"))
        assert (d / "caption.txt").read_text().strip()
        with Image.open(d / "cover.png") as im:
            assert im.size == (1080, 1920)
        assert post(short, platform)["platform"] == platform
    manifest = read_json(short.path("upload/manifest.json"))
    assert manifest["source"] == "final.mp4" and manifest["published"] is False
    assert "AI" in short.path("upload/README.md").read_text()


def test_a_failed_short_gets_no_bundle(short):
    write_json(short.path("qc.json"), {"passed": False, "files": {}})
    with pytest.raises(export.Blocked, match="QC"):
        export.run(short)
    assert not short.path("upload").exists()


def test_a_video_changed_after_qc_gets_no_bundle(short):
    video(short.path("final.mp4"), seconds=2)
    with pytest.raises(export.Blocked, match="changed after QC"):
        export.run(short)
    assert not short.path("upload").exists()


def test_a_video_that_is_not_vertical_gets_no_bundle(short):
    video(short.path("final.mp4"), size="1920x1080")
    write_json(short.path("qc.json"), {"passed": True, "files": {"final.mp4": sha256_file(short.path("final.mp4"))}})
    with pytest.raises(export.Blocked, match="1080x1920"):
        export.run(short)


def test_a_public_privacy_status_gets_no_bundle(short):
    meta = read_json(short.path("metadata.json"))
    write_json(short.path("metadata.json"), {**meta, "privacy_status": "public"})
    with pytest.raises(export.Blocked, match="private"):
        export.run(short)


def test_every_caption_keeps_the_disclosure_and_the_data_credit(short):
    export.run(short)
    for platform in ("tiktok", "reels", "shorts"):
        text = short.path(f"upload/{platform}/caption.txt").read_text()
        assert "Narrated with an AI voice" in text
        assert "nflverse (CC BY 4.0)" in text


def test_only_shorts_carries_the_shorts_hashtag(short):
    export.run(short)
    assert "#shorts" in short.path("upload/shorts/caption.txt").read_text()
    for platform in ("tiktok", "reels"):
        assert "#shorts" not in short.path(f"upload/{platform}/caption.txt").read_text()


def test_hashtags_fit_each_platform_and_name_the_story(short):
    export.run(short)
    for platform, limit in (("tiktok", 5), ("reels", 5)):
        tags = post(short, platform)["hashtags"]
        assert 0 < len(tags) <= limit and len(set(tags)) == len(tags)
        assert "#jalondaniels" in tags and "#buccaneers" in tags


def test_a_long_story_loses_its_last_lines_never_the_disclosure(short):
    meta = read_json(short.path("metadata.json"))
    filler = "\n".join(f"A long extra line about the game, number {'x' * 300}" for _ in range(10))
    write_json(short.path("metadata.json"), {**meta, "description": filler + "\n" + DESCRIPTION})
    export.run(short)
    tiktok = short.path("upload/tiktok/caption.txt").read_text()
    assert len(tiktok) <= short.cfg["export"]["tiktok"]["max_caption"]
    assert "Narrated with an AI voice" in tiktok and "nflverse" in tiktok


def test_a_title_over_the_youtube_limit_is_blocked(short):
    meta = read_json(short.path("metadata.json"))
    write_json(short.path("metadata.json"), {**meta, "title": "x" * 101})
    with pytest.raises(export.Blocked, match="title"):
        export.run(short)


def test_a_description_without_the_ai_disclosure_is_blocked(short):
    meta = read_json(short.path("metadata.json"))
    write_json(short.path("metadata.json"), {**meta, "description": DESCRIPTION.replace("Narrated with an AI voice", "Hi")})
    with pytest.raises(export.Blocked, match="disclosure"):
        export.run(short)


def test_the_post_records_the_disclosure_step_for_a_person_to_take(short):
    export.run(short)
    for platform in ("tiktok", "reels", "shorts"):
        d = post(short, platform)["disclosure"]
        assert d["ai_voice"] is True and d["ai_visuals"] is False and d["steps"]


def test_a_styled_render_that_passed_qc_is_the_one_bundled(short):
    short.cfg.raw["graphics"] = {"style": "premium"}
    styled = short.path("styles/premium/final.mp4")
    video(styled, seconds=2)
    write_json(short.path("styles/premium/qc.json"), {"passed": True, "files": {"final.mp4": sha256_file(styled)}})
    export.run(short)
    assert read_json(short.path("upload/manifest.json"))["source"] == "styles/premium/final.mp4"
    assert sha256_file(short.path("upload/reels/video.mp4")) == sha256_file(styled)


def test_a_styled_render_that_failed_qc_falls_back_to_final(short):
    short.cfg.raw["graphics"] = {"style": "premium"}
    styled = short.path("styles/premium/final.mp4")
    video(styled, seconds=2)
    write_json(short.path("styles/premium/qc.json"), {"passed": False, "files": {"final.mp4": sha256_file(styled)}})
    export.run(short)
    assert read_json(short.path("upload/manifest.json"))["source"] == "final.mp4"


def test_a_rerun_replaces_the_bundle_cleanly(short):
    export.run(short)
    short.path("upload/tiktok/stale.txt").write_text("old")
    export.run(short)
    assert not short.path("upload/tiktok/stale.txt").exists()


def test_a_variant_is_not_bundled(short):
    write_json(short.path("story.json"), {"order": 90, "variant_of": "2026-w07-short-flame-jalon-daniels"})
    assert export.run(short) is False
    assert not short.path("upload").exists()


# ------------------------------------------------------------------------------------- by hand

def test_the_readme_is_a_tick_list_with_each_apps_disclosure_step(short):
    export.run(short)
    readme = short.path("upload/README.md").read_text()
    for app, step in (("TikTok", "AI-generated content"), ("Instagram", "AI label"), ("YouTube", "AI use")):
        assert app in readme and step in readme
    assert readme.count("- [ ]") >= 12
    assert "does not publish" in readme.lower()


def test_each_folder_stands_alone_for_airdrop(short):
    export.run(short)
    assert {p.name for p in short.path("upload/tiktok").iterdir()} == {"video.mp4", "cover.png", "caption.txt", "post.json"}
    shorts = {p.name for p in short.path("upload/shorts").iterdir()}
    assert {"title.txt", "tags.txt"} <= shorts
    assert short.path("upload/shorts/title.txt").read_text().strip() == "The NFL v. Jalon Daniels, Week 6"


def test_bundling_does_not_copy_the_video_bytes_twice(short):
    export.run(short)
    assert short.path("upload/tiktok/video.mp4").stat().st_ino == short.path("final.mp4").stat().st_ino


def test_a_live_injury_status_sets_the_posting_deadline(short):
    qc = read_json(short.path("qc.json"))
    write_json(short.path("qc.json"), {**qc, "gates": [{"name": "timeliness",
                                                       "details": {"valid_until": "2026-10-25T13:00:00-04:00"}}]})
    export.run(short)
    assert read_json(short.path("upload/manifest.json"))["post_by"] == "2026-10-25T13:00:00-04:00"
    assert "2026-10-25" in short.path("upload/README.md").read_text()


def test_a_story_deadline_wins_over_no_deadline(short):
    write_json(short.path("story.json"), {"order": 90, "post_by": "2026-10-27T13:00:00-07:00"})
    export.run(short)
    assert read_json(short.path("upload/manifest.json"))["post_by"] == "2026-10-27T13:00:00-07:00"


def test_the_week_list_orders_shorts_for_posting(short):
    second = Episode(2026, 7, "short", short.cfg, log=lambda *_: None, slug="braelon-allen")
    for rel in ("metadata.json", "facts.json", "script.json", "audio/timings.json", "qc.json"):
        write_json(second.path(rel), read_json(short.path(rel)))
    second.path("final.mp4").write_bytes(short.path("final.mp4").read_bytes())
    write_json(second.path("story.json"), {"order": 1})
    export.run(short)
    export.run(second)
    text = export.week_list(short.cfg, 2026, 7).read_text()
    assert text.index(second.id) < text.index(short.id)
    assert text.count("- [ ]") == 6


def test_the_week_list_skips_a_week_with_no_bundles(short):
    assert export.week_list(short.cfg, 2026, 7) is None


def test_export_bundles_rebuilds_every_passed_short_of_the_week(short):
    assert export.main(["--week", "7"]) == 0
    assert short.path("upload/manifest.json").exists()
    assert (short.cfg.episodes_dir / "2026-w07-short" / "UPLOAD.md").exists()


def test_export_bundles_skips_a_short_that_failed_qc(short):
    write_json(short.path("qc.json"), {"passed": False, "files": {}})
    assert export.main(["--week", "7"]) == 1
    assert not short.path("upload").exists()


# -------------------------------------------------------------------------- every upload is a human decision

POSTING_APIS = re.compile(r"googleapis\.com/upload|youtube/v3|tiktokapis|graph\.(facebook|instagram)|metricool|"
                          r"createScheduledPost|videos\.insert", re.I)


def test_no_code_path_can_publish():
    source = Path(export.__file__).resolve().parents[1]
    hits = [str(p) for p in source.rglob("*.py") if POSTING_APIS.search(p.read_text())]
    assert not hits, f"a posting API is referenced in {hits}"
