# Soccer engine isolation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A soccer trend run shares no state or NFL data with the NFL run, and adapters can supply their own
video formats, with NFL behaviour unchanged.

**Architecture:** Per-sport paths in the CLI, sport limits on collectors, NFL defaults moved into the football
adapter, and optional adapter hooks in the engine and idea generator. This is PR 1 of 5; see
`docs/superpowers/specs/2026-10-30-soccer-shorts-account-design.md`. Later PRs get their own plans.

**Tech Stack:** Python 3.11+, DuckDB, httpx, pytest, ruff, uv.

## Global Constraints

- NFL paths, collectors, formats and rankings stay exactly as they are.
- No new dependencies. `social-intel/1.0` stays unchanged.
- Run from `fantasy-intel/social`: `uv run --group dev pytest -q` and `uv run --group dev ruff check social_intel tests`.

---

### Task 1: Per-sport store and history paths

**Files:** Modify `social_intel/cli.py`. Test `tests/test_store_cli.py`.

**Produces:** `cli.sport_paths(sport: str) -> tuple[Path, Path]` returning `(db_path, history_path)`.

- [ ] Write `test_sport_paths_keep_nfl_and_isolate_soccer`: NFL returns `DATA/social.duckdb` and
  `STUDIO_EPISODES/history.jsonl`; soccer returns `DATA/soccer/social.duckdb` and
  `STUDIO_EPISODES/soccer/history.jsonl`.
- [ ] Confirm it fails with `AttributeError`.
- [ ] Add `sport_paths` and use it in `make_engine`; `--db` still overrides the database path.
- [ ] Confirm the test passes, then commit.

### Task 2: Collectors carry no NFL defaults

**Files:** Modify `collectors/base.py`, `collectors/reddit.py`, `collectors/attention.py`, `collectors/video.py`,
`sports/football/adapter.py`, `sports/soccer/adapter.py`. Test `tests/test_collectors.py`.

**Produces:** `Collector.sports: ClassVar[frozenset[str] | None]`; config keys `extra["activity_subreddits"]` and
`extra["tiktok_creators"]`; wiki player keys `entity_id` and `disambiguator`.

- [ ] Write failing tests: Sleeper is skipped for soccer and runs for NFL; `reddit_activity` reads only the
  configured subreddits; TikTok with no creators fetches nothing; football config still lists its three
  activity subreddits and the `nfl` creator; a soccer wiki player tries `Name_(footballer)` then `Name`, and an
  NFL player's candidates do not change.
- [ ] Add the `sports` check to `run_collectors`, reporting `skipped`, and set `sports = frozenset({"nfl"})` on
  `SleeperTrendingCollector`.
- [ ] Replace `activity_subs` and the TikTok default with config lookups, and move the values to the football adapter.
- [ ] Make Wikipedia ids and the title suffix configurable, keeping `gsis_id` for NFL rows.
- [ ] Update `test_arctic_activity_counts_fresh_comments_per_thread` to pass the subreddit through config.
- [ ] Confirm the tests pass, then commit.

### Task 3: Adapter hooks for formats and team enrichment

**Files:** Modify `social_intel/engine.py`, `social_intel/ideas.py`, `social_intel/cli.py`, `sports/base.py`.
Test `tests/test_scoring_ideas.py`.

**Produces:** Optional adapter members: `enrich_teams: bool`, `choose_formats(cluster) -> list[str]`,
`build_idea(cluster, fmt, rotation, window, meme_bank, now) -> VideoIdea`, `format_labels() -> dict[str, str]`.

- [ ] Write failing tests: `generate` uses the adapter's `choose_formats` and `build_idea` when present and the
  module functions otherwise; a team-only cluster is enriched only when `enrich_teams` is true.
- [ ] Implement with `getattr` fallbacks, and make `trends idea` print adapter labels.
- [ ] Update the README's soccer section, run the full social suite and ruff, then commit.
