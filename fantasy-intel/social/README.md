# social: trend intelligence for the video generator

Finds the NFL and fantasy-football conversations people are engaging with right now, groups them
into topics, ranks them 0-100 with a breakdown you can read, checks each take against our own
analytics, and turns the best ones into video ideas you can hand to the studio.

```
discover (collectors) -> store (DuckDB, dedup, engagement snapshots) -> cluster -> enrich (fantasy.duckdb)
  -> score 0-100 -> ideas -> review (CLI) -> `trends select` -> `make-episode --trend` (existing studio + QC)
```

Nothing here renders or publishes. Every number an idea says out loud carries a source (a table and a
data cut, or a measured social count with its URL). Social opinions are attributed, never stated as fact.

## Setup

```bash
brew install yt-dlp            # YouTube and TikTok metadata (keep it updated: brew upgrade yt-dlp)
cd fantasy-intel/social && uv sync
uv run trends run              # collect -> rank -> ideas
```

It reads the analytics database at `../data/fantasy.duckdb` read-only (override with
`FANTASY_DB_PATH`) and writes its own `../data/social/social.duckdb` (override with
`SOCIAL_DATA_DIR`), so the analytics schema never changes and a collector run never locks the API.
No API keys, accounts or paid services are needed. Optional: `uv sync --extra embeddings` adds
model2vec static embeddings to clustering (CPU only, no torch); TF-IDF is used without it.

Scheduled collection: `scripts/launchd/com.fantasy-intel.social-trends.plist` (every 20 minutes;
each source keeps its own interval). Fill it in like the refresh agent:

```bash
sed -e "s#__FANTASY_INTEL_DIR__#$PWD/..#g" -e "s#__UV__#$(command -v uv)#g" \
  ../scripts/launchd/com.fantasy-intel.social-trends.plist > ~/Library/LaunchAgents/com.fantasy-intel.social-trends.plist
launchctl load ~/Library/LaunchAgents/com.fantasy-intel.social-trends.plist
```

## Review workflow

```bash
uv run trends list                      # ranked trends: score, platforms, independent voices, age, flags
uv run trends list --player nabers      # filter by player (or --team NYG, --min-score 40, --sort fresh)
uv run trends show 3                    # score components, original posts (links), crowd stance,
                                        #   our analytics with sources, crowd-vs-data verdict, competing angles
uv run trends ideas --full              # generate + save ideas from the latest ranking
uv run trends idea <id>                 # one idea's full research package
uv run trends select <id>               # writes studio/episodes/<season>-wNN-trends/<id>.json
cd ../studio && uv run make-episode --week 7 --trend episodes/2026-w07-trends/<id>.json
#   add --frozen-snapshot to re-make a week from its archived snapshot.json instead of today's database
uv run trends health                    # per-source status, success rate, cooldowns, last error
uv run trends export --out bundle.json  # versioned JSON contract bundle; `trends schema` prints the schema
```

The package names the player to build the Short about (for an injury idea about a player who is out,
the replacement, i.e. the studio's fill-in story) plus fallbacks the studio tries in order.
`make-episode --trend` builds that player's Short through the normal stages, claim checks and QC,
and adds a "Social trend context (not narrated)" section to its `review.md`. Social numbers never
enter the narration, so the claims/QC chain is unchanged. If the player doesn't qualify as a usage
story that week, the studio says so and the package serves as a manual script brief.

`uv run trends export --out bundle.json` also feeds the studio's AI parody show:
`make-episode --parody --trends bundle.json` nudges its picks toward what people are talking about and skips anyone
in real trouble. Social numbers never reach that script either.

## Sources (verified working 2026-10-17, no login)

| Collector | Method | Interval | Gives |
|---|---|---|---|
| `reddit` | multi-subreddit hot + rising Atom feeds | 20 min | titles, bodies, timestamps (no scores) |
| `reddit_teams` | all 32 team subreddits, one hot feed | 2 h | team-community chatter |
| `reddit_activity` | Arctic Shift archive: latest comments per thread + new posts | 15 min | **comments/hour per thread**, comment text (debate), new posts |
| `bluesky` | `api.bsky.app` searchPosts (top + latest), `since` cursor | 15 min | likes, replies, reposts, quotes |
| `x` | FxTwitter public API, curated insider/analyst timelines | 20 min | likes, replies, reposts, views |
| `youtube` | channel RSS + yt-dlp upload-date search, full metadata for the top 8 | 45 min | views, likes, comments, upload time |
| `sleeper_trending` | Sleeper trending adds/drops (24 h) | 30 min | the earliest fantasy-attention signal |
| `google_news` | Google News RSS search, `when:1d`, plus the hottest players | 20 min | confirmation, links |
| `news_feeds` | ESPN, PFT, Yahoo, CBS RSS | 20 min | confirmation, links |
| `google_trends` | Google daily trending-searches RSS | 30 min | the very biggest stories |
| `wikipedia` | pageviews for the 60 most-rostered players; emits spikes | 12 h | attention surges (1-2 day lag) |
| `tiktok` (optional) | yt-dlp on creator pages | 3 h | views, likes, comments, reposts |
| `instagram` (optional) | instaloader with a logged-in session | 6 h | blocked anonymously (401) |

Not used, because they no longer work for free: Reddit `.json` (403 since May 2026), Nitter
(dead), Twitter syndication timelines (429/404), TikTok hashtag pages and Creative Center (login),
Instagram anonymous endpoints (401), Pullpush (paid). Details: `../docs/social_trend_engine.md`.

Every collector is isolated: an exception or block is recorded in `source_state` with an
exponential cooldown (15 min doubling, x3 for a block, capped at 12 h) and the others carry on.

## Ranking

Ten components, 0-1 each, fixed weights (`social_intel/core/scoring.py`):
velocity 18% (interactions/hour from engagement snapshots or measured comment pace, against a
per-platform "hot" rate), relative popularity 12% (vs the community's 14-day median), cross-platform
momentum 10%, freshness 10% (20 h half-life), debate 10% (argument/question language, contested
votes, comments per upvote), fantasy relevance 12% (topic weights), entity popularity 8% (ESPN
roster %, scoring), analytic opportunity 10% (how much our data can say, more when it contradicts
the crowd), novelty 6% (studio history + ideas already sent), meme potential 4%. Flags multiply the
total: stale x0.55, unconfirmed rumour x0.85, sarcasm-driven x0.9, suspicious engagement x0.7,
recycled x0.6, already covered x0.45.

## Soccer and other repositories

The engine is sport-agnostic; a sport is an adapter (`social_intel/sports/base.py`). Football lives
in `sports/football` (NFL entities, fantasy topics, `fantasy.duckdb` enrichment); `sports/soccer` is
the stub that documents what the soccer generator implements. Both share one versioned JSON
contract (`social_intel/contract.py`, `trends schema`, `CONTRACT_VERSION = social-intel/1.0`):
posts, clusters with score components, ideas with sourced stats. Readers must ignore unknown keys.

## Tests

```bash
uv run --group dev pytest       # parsers run against real trimmed responses (tests/fixtures, labelled as fixtures)
uv run --group dev ruff check social_intel tests
```
