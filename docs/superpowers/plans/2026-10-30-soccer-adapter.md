# Soccer adapter implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trends --sport soccer run` collects live soccer discussion, names the right clubs and players, and
produces parody, parallel-universe and explainer ideas.

**Architecture:** PR 2 of 5, stacked on the engine isolation PR. Clubs are a checked-in table, players a cache
written by `sync-entities`, and formats live beside the adapter. See
`docs/superpowers/specs/2026-10-30-soccer-shorts-account-design.md`.

**Tech Stack:** Python 3.11+, DuckDB, httpx, pytest, ruff, uv.

## Global Constraints

- No new dependencies. `social-intel/1.0` stays unchanged. NFL behaviour stays unchanged.
- Only handles, subreddits and channel ids confirmed to resolve go in the source list.
- No Understat, FBref or FotMob. Stats arrive in PR 3; `enrich` reports zero coverage until then.

---

### Task 1: Clubs, topics and recognition

**Files:** Create `sports/soccer/clubs.py`, `topics.py`, `entities.py`. Test `tests/test_soccer.py`.

- [ ] Test the club table (at least 126 clubs, no duplicate aliases), longest-alias matching, the soccer-context
  gate for ambiguous names, and conservative player matching.
- [ ] Implement `Club`, `CLUBS`, `tag_topics`, `SOCCER_CONTEXT` and `SoccerIndex`.

### Task 2: Player sync

**Files:** Create `sports/soccer/sync_entities.py`. Modify `cli.py`. Test `tests/test_soccer.py`.

- [ ] Test `parse_fpl`, `parse_asa`, `parse_wikidata` and the soccer-only command.
- [ ] Implement them with per-source failure isolation and a `sync-entities` command.

### Task 3: Adapter, formats and schedule

**Files:** Create `sports/soccer/ideas.py`, `scripts/launchd/com.fantasy-intel.social-trends-soccer.plist`. Modify
`sports/soccer/adapter.py`, `core/cluster.py`, `README.md`. Test `tests/test_soccer.py`.

- [ ] Test source config has no NFL sources, formats follow topics, parody ideas carry the content rules, and a
  ranked cluster names its club.
- [ ] Implement them and add a soccer schedule at :10, :30 and :50.

### Task 4: Live check

- [ ] Run `sync-entities` and `run` against a scratch `SOCIAL_DATA_DIR`.
- [ ] Hand-check the top 50 trends: at least 70% must name the right club or player. Record the result in the PR.
