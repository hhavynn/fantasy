# Social trend intelligence engine

Status: MVP, 2026-10-17. Code: `fantasy-intel/social/` (setup and commands in its README).

## 1. What existed before (audit)

Verified in the repository, not assumed:

- **Video pipeline** (`studio/`): `make-episode` (argparse, `studio/cli.py`). Shorts run
  facts -> script (deterministic, `stages/story.py`) -> Kokoro TTS -> motion render -> thumbnail ->
  metadata, then QC -> provenance -> review. Long form adds a local Qwen3 (llama.cpp) script stage with
  an LLM sentence check. No upload stage; metadata is hard-coded private.
- **Topic selection**: fully deterministic from usage data. `facts.select_players` (sustained usage
  moves via `backend/app/analytics/trends.py`) -> `pick_stories` (one per team) -> `cli.editorial_pick`
  (8-criterion editorial score) -> shows plan. **No news, trend or social input anywhere.**
- **Fact verification**: every spoken number is a claim re-derived from the hashed `snapshot.json`
  (`facts.evaluate_claim`); QC fails on any digit outside a claim. Claims have no external URL source.
- **Duplicate prevention**: `studio/episodes/history.jsonl` (rendered episodes: players, narration,
  structure) feeds recency de-prioritisation, the editorial distinctiveness score, hook rotation and the
  originality/creative QC gates. It records renders, not uploads.
- **Analytics** (`backend/`, `data/fantasy.duckdb`, schema v8): `player_week_usage` (snap share, target
  share, backfield share, air yards, red-zone opportunities, xFP; route data columns exist but the
  external import is empty), `player_scores` (trend labels, ratings 0-100, fp vs xFP),
  `independent_player_projections` (with 80% intervals), `player_ros_projections` (ESPN ROS),
  `nfl_injuries` (official reports by week), `fantasy_players` (ESPN roster %), `team_context` /
  `defense_context` (next opponent, implied totals, defensive ranks). No name/alias matcher beyond exact
  normalised names; no fuzzy matching.
- **Ingestion**: no social/RSS/scraping code existed. HTTP patterns worth copying: Sleeper client
  (injectable transport, retry/backoff, cache). No lint/type-check config in backend or studio.

**Integration decisions** (made to avoid touching working parts):

1. A separate uv project (`fantasy-intel/social`) with three dependencies (duckdb, httpx, numpy, plus
   pytz for DuckDB timestamps). The studio's torch/Kokoro stack and the backend's Vercel bundle are untouched.
2. Its own database (`data/social/social.duckdb`): no migration of `fantasy.duckdb`, which is read-only
   here; collectors can never hold the analytics write lock; nothing is published to MotherDuck.
3. Studio integration is one flag (`make-episode --trend <package>`) plus a review section. Trend
   context is reviewer-only and never narrated, so the claim/QC chain is unchanged.
4. Novelty reads `history.jsonl` (players featured in the last 12 renders) and the ideas already sent.

## 2. Public collection methods (engineering comparison)

Live-probed from this machine on 2026-10-17 with a browser User-Agent and a handful of requests each.

| Platform | Works today without login | Chosen method | Rejected / fallback |
|---|---|---|---|
| Reddit | Atom feeds (~1 req/min), Arctic Shift archive | multi-sub `hot`/`rising` `.rss` + Arctic Shift comments (counted per thread for comments/hour) and new posts | `.json` on www/old/api: 403 since late May 2026. PRAW needs an approved app (manual review since Nov 2025). Pullpush: paid. |
| X | FxTwitter `api.fxtwitter.com/2/profile/<user>/statuses` | curated insiders/analysts (likes, replies, reposts, quotes, views) | Nitter: dead (410/451, project archived). Syndication timeline: 429/404. twscrape/twikit: need logged-in accounts that get banned. No free search. |
| Bluesky | `api.bsky.app` searchPosts (top/latest, `since`) | per-query search, `since` as the incremental cursor | `public.api.bsky.app` search: 403. Cursor pagination: 403 without auth (free app password would unlock). |
| YouTube | channel RSS (views), yt-dlp search/metadata | 5 channel feeds + yt-dlp upload-date search (`sp=CAISAhAB`), full `-j` on the top 8 | Data API v3 (free key, 100 searches/day) if yt-dlp breaks. |
| TikTok | yt-dlp on creator pages | optional collector, creators list | Hashtag pages broken in yt-dlp; Creative Center API needs login; TikTokApi needs Playwright + ms_token. |
| Instagram | nothing anonymous | optional collector behind an instaloader session | web_profile_info 401, yt-dlp fails, mirrors behind Cloudflare. |
| Google Trends | daily RSS (10 items); explore with NID cookie (flagged as scraper) | RSS only | pytrends archived; explore endpoints 429 cold. |
| Sleeper | trending add/drop REST | 24 h add/drop counts, mapped to gsis via weekly rosters | - |
| Wikipedia | pageviews REST (1-2 day lag) | top 60 rostered players, spikes vs 7-day median | - |
| News | Google News search RSS, ESPN/PFT/Yahoo/CBS RSS | both | FantasyPros, Rotoworld, NFL.com, Underdog feeds 404. |

Library comparison (PyPI/GitHub, Oct 2026): yt-dlp (very active, the backbone for video), atproto
0.0.73 (active; only needed for authenticated Bluesky), twscrape 0.20.1 (active, needs accounts),
twikit 2.3.3 (stale releases), ntscraper (useless, Nitter dead), praw 8.0.3 (maintained, app approval
is the blocker), TikTokApi 7.3.3 (maintained, needs Playwright + token), instaloader 4.15.4 (active,
blocked anonymously), pytrends (archived), pytrends-modern (small, active), feedparser (stable; not
needed, stdlib XML is enough for these feeds).

## 3. Architecture

- `contract.py`: versioned dataclasses (`SocialPost`, `Engagement`, `TrendCluster`, `TrendScore`,
  `VideoIdea`, `SourcedStat`) and `json_schema()` (draft 2020-12). Missing engagement is `None`, never 0.
- `core/http.py`: one fetcher; retries with exponential backoff + jitter, Retry-After, per-host
  politeness, block detection (a second 401/403 or exhausted 429), disk cache.
- `collectors/`: one class per source, registered by name, isolated by `run_collectors` (cooldowns,
  refresh intervals, incremental cursors, health records in `collection_runs` / `source_state`).
- `core/store.py`: posts upserted by platform+id (sources merge: RSS timestamps + archive comment
  activity), an engagement snapshot per collection (velocity), normalised-text hashes (copies,
  recycled rumours), community baselines, rankings, ideas with status (new/selected/sent).
- `core/cluster.py`: entity-anchored grouping (posts about only the same player join; multi-player
  posts and team posts need text overlap) with TF-IDF cosine on text with entity names removed;
  reposts/crossposts/copies collapse; `independent_sources` counts distinct voices.
- `core/scoring.py`: the 0-100 score (weights in README). `core/signals.py`: rumour, sarcasm,
  debate, contrarian, question, hype cues.
- `sports/football/`: entity index (rosters + roster % + usage, conservative single-token rules,
  nicknames), topic lexicon/weights, `enrich.py` (point-in-time analytics with sources, crowd-vs-data
  verdict, teammate redistribution for injuries), adapter (sources, calendar, meme bank).
- `ideas.py`: formats chosen from the crowd-vs-data relation and topics; three hooks per idea (the
  format's own opening + two least-recently-used families, never the same sentence twice); every
  number from a sourced stat; banned AI-cliché phrases.
- `engine.py`, `cli.py`: orchestration and the review CLI. Studio: `--trend` + review section.

Point-in-time rule: enrichment only uses weeks whose last game was before `now`
(`nfl_schedules`), so replaying an old trend never sees later games.

## 4. Live demonstration (2026-10-17, ~19:00 PT, Week 6 pregame)

All 12 default/optional collectors that can work anonymously succeeded (Instagram reports
"needs session"): about 3,600 posts in 24 h across Reddit (RSS + Arctic Shift), Bluesky, X
(FxTwitter), YouTube, TikTok, Sleeper, Google News, outlet RSS, Google Trends and Wikipedia;
556 clusters from the last 72 h. A full fast-source refresh takes ~2.5 min (Reddit's feed limit
forces a 65 s gap; Wikipedia, every 12 h, adds ~4 min).

Point-in-time check: the season's usage table already holds Thursday-night Week 6 rows, but the
data cut was correctly limited to Weeks 1-5 (Week 6 is still being played).

Top real ideas from that run (`trends ideas --full`; each with three hooks, sources, stats):

1. **"Jalen Hurts Is Out: Is Tanner McKee the Answer?"** (score 88.4, 6 platforms, 90 voices).
   Schefter/Rapoport IR posts (20k / 9k likes), Hurts dropped in 80k Sleeper leagues, McKee the
   #4 add (210k). Hook: "Hurts is out. Tanner McKee was just added in 210,256 Sleeper leagues in 24 hours."
2. **"A.J. Brown's Return: How Much Work Comes Back?"** (83.0). Our weekly roster still listed
   Brown on IR while Schefter and outlets reported him activated: the engine caught the conflict,
   switched to the return angle and attached a "confirm the status before scripting" note.
3. **"Jacory Croskey-Merritt or Rachaad White?"** (78.1). Croskey-Merritt dropped in 122k leagues,
   White added in 114k; Croskey-Merritt's share of RB carries 60% -> 51% (Weeks 1-3 vs 4-5), White
   28% -> 32%; our projection 8.9 (80% range 3.5-15.3).
4. **"Buy Kyler Murray Now?"** (77.9). 36k Sleeper drops vs. fantasy points 5.0 -> 14.9 (with a
   note that Weeks 1-3 include his Week 2 concussion absence).
5. **"Tyler Shough or Jameis Winston?"** (77.1). The exact FantasyPros poll people were answering;
   Shough 23.1 -> 7.9 PPR points, Winston 4.3 -> 21.9, our Week 6 projection for Shough 16.1.
6. **"Roman Wilson Is Out: Is Germie Bernard the Answer?"** (73.2). Wilson out (finger) on the
   official report, dropped in 153k leagues.

`trends select e22142a13c` wrote the Hurts/McKee package to
`studio/episodes/2026-w06-trends/` with the `make-episode --week 6 --trend ...` command. No video was
rendered (as instructed).

## 5. Validation

`fantasy-intel/social`: 46 tests (pytest, no network), ruff clean. Parsers run against real trimmed
responses captured during the probe (labelled fixtures); collector failure isolation, 403/429 block
detection and cooldowns, Retry-After, caching, incremental cursors, dedup and snapshot history,
source-merge on upsert, missing engagement (None, not zero), player/team extraction (nicknames,
ambiguous surnames, common words), clustering (subject anchors, roundups, templated attention lines,
repost collapse), score bounds/weights/flags/attention gate, point-in-time enrichment against a
temporary analytics DB, crowd-vs-data verdicts, stale-status detection from reports, idea
provenance (no unsourced number in a hook), the JSON contract (validated with jsonschema) and the CLI
workflow end to end. Studio: 3 new tests for `--trend` and the review section; the full studio suite
still passes (242 passed, 21 skipped). CI gains a `social` job (lint + tests).

## 6. What works, what is blocked

Works without accounts or money: everything in section 2's "chosen method" column. Blocked or
fragile: Reddit scores (no unauthenticated JSON; we measure comment pace instead), X search (only
curated timelines via a third-party proxy that could disappear), Bluesky pagination, TikTok
hashtags/trending, Instagram (login), Google Trends explore (flagged as a scraper). Wikipedia runs
about 2 s per request in practice, so it is scheduled every 12 h and runs last.

Known quality limits of the MVP:
- Hooks and titles are deterministic templates with rotation: accurate and varied, but they read
  as drafts; a human (or the local Qwen model, see next steps) should punch them up.
- Crowd direction comes from a topic lexicon and Sleeper votes, not real stance detection.
- Clusters are player-centred; a pure tactics/coaching argument without a named player only forms
  through near-duplicate text.
- Our weekly roster/injury tables can lag reporters by a day; the status-cue check flags it but only
  from headline wording.

## 7. Highest-impact next steps

1. A one-shot local LLM pass (the studio's llama.cpp Qwen3) that rewrites the three hooks and the
   title from the sourced facts only, with the existing "no number outside a claim" check.
2. Feed the trend score into the studio's `editorial.score` (timeliness/relevance) so the weekly
   Shorts batch prefers usage stories people are already arguing about.
3. A free Bluesky app password (cursor pagination, full search) and a YouTube Data API key as a
   yt-dlp fallback: both free, both remove the most fragile parts.
4. Real stance classification on Reddit comment samples (who is for/against), replacing the lexicon.
5. Track which selected ideas were published and how they performed (`ideas.status = sent`, plus
   retention once the studio ingests analytics), and learn the score weights from that.
