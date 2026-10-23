# #186 Player news sources and licensing: go/no-go memo

Date: 2026-10-23. Scope: research only, no code changed. Gate being resolved (`docs/roadmap-execution/DECISIONS.md`, open research gates): "#186 news: needs a free source with redistribution rights; otherwise the profile ships without a news tab."

## Recommendation

**NO-GO on a news (headline) feed. GO on a separately labelled "Availability events" feed built only from data the app already ingests under CC BY 4.0.**

- No free source found permits an app to automatically fetch, cache and display third-party player-news headlines. Every publisher or platform source either bars automated or systematic retrieval, limits use to personal non-commercial reading, or has terms that contradict its own feed page. None gives a clear grant.
- The gate's own fallback applies: the player profile ships without a news tab.
- What can ship instead is a structured, templated event list (status changes) from nflverse injury reports and weekly rosters, which carry an explicit open licence (CC BY 4.0). It must be named "Availability events", never "News", and must not claim confirmation, cause or role.

Verification limits: the sandbox could resolve only GitHub, so most provider pages could not be fetched whole. The nflverse licence text was read directly. Other terms come from search-tool excerpts of the providers' own pages (marked "primary excerpt"), not a full read. Anything not traced to a provider page is **UNVERIFIED**. Verify the rows marked "primary excerpt" against the live page before any later reversal of this decision. Nothing here is legal advice.

## What the app already ingests

| Signal | Where | Source and terms | News-like? |
|---|---|---|---|
| Weekly injury report: `report_status`, `practice_status`, primary injury, `date_modified` | `nfl/sync.py` (`nfl_injuries`), `analytics/injury_freshness.py` (append-only `injury_retrievals`, with our `retrieved_at` and the provider's `date_modified`) | nflverse `load_injuries` ([dictionary](https://nflreadr.nflverse.com/articles/dictionary_injuries.html), [loader](https://nflreadr.nflverse.com/reference/load_injuries.html)). nflverse-data is CC BY 4.0 ([LICENSE.md](https://github.com/nflverse/nflverse-data/blob/master/LICENSE.md), read directly). Updated daily 07:00 UTC in season ([schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html), primary excerpt). | Yes: structured status-change events. A missing report is not "healthy" (`injury_freshness` already enforces this). |
| Weekly roster status (ACT, RES, PUP, NFI, team) | `nfl_rosters_weekly`, `analytics/player_availability.py` | nflverse `load_rosters_weekly`, same licence. Updated daily. | Yes: reserve placement, activation and team change events. |
| ESPN `injury_status` | `fantasy_players`, the user's own ESPN sync | ESPN fantasy API with the user's credentials. It has no provider timestamp (`injury_freshness`: stays NULL). Out of scope for redistribution; it is the user's own league data shown back to them. | Status snapshot only; diffs can be recorded with our clock. |
| Depth charts | `nfl_depth_charts` | nflverse, but the upstream is probably ESPN's depth charts. The repo's own note (`fantasy-intel/docs/research/first_read_depth_chart_signals.md`) says upstream terms are not verified. | Treat as internal input only. **UNVERIFIED** for public display. |
| Owner-curated facts | `analytics/availability_news.json`, `availability_news.py` | Written in the owner's words with source URLs. Read only by live projection runs. | Already the app's only free-text availability "news"; a hand-curated file, not a feed. |
| Sleeper player objects | `sleeper/client.py` (`/players/nfl`, user-linked leagues) | See the Sleeper row below. | The app uses only `injury_status`. |

nflverse has no player-transactions dataset that I could confirm. `load_trades` is "a table of historical trades as maintained by Lee Sharpe" via PFR ([source](https://github.com/nflverse/nflreadr/blob/main/R/load_trades.R)), a draft-pick trade table, not signings or waivers. I could not open the full loader index, so absence of a transactions loader is **UNVERIFIED**. Roster-status deltas in `nfl_rosters_weekly` cover the transaction-like cases (placed on reserve, activated, team change).

nflverse caveat: the CC BY 4.0 grant is nflverse's. The project disclaims legal responsibility for commercial use, and for injuries it says only "data collected from an API", so the upstream provider and its terms are **UNVERIFIED**. The NFL's own terms bar systematic retrieval (below). The residual risk is low for a non-commercial local or hosted dashboard that downloads nflverse's releases and does not scrape the NFL. It is not zero. FTN datasets are CC BY-SA 4.0 and are out of scope.

## Source verdicts

Verdicts: ALLOWED (with conditions), UNCLEAR, NOT ALLOWED. All sources are free of charge to read; the question is rights.

| Source | Automated access | Cache and display headline + timestamp + link | Attribution, limits, cost | Verdict | Citation (evidence level) |
|---|---|---|---|---|---|
| **nflverse injuries and rosters** (structured, not news) | Yes. Public GitHub release files, already used. | Yes, as derived structured facts. CC BY 4.0 permits sharing and adaptation. | Credit nflverse, link the licence, say it was modified. Daily update, free. | **ALLOWED (with conditions)**: structured events only; upstream provider UNVERIFIED | [LICENSE.md](https://github.com/nflverse/nflverse-data/blob/master/LICENSE.md) (fetched); [org page](https://github.com/nflverse) (excerpt: most data CC BY 4.0, FTN CC BY-SA) |
| nflverse depth charts | Yes | Licence yes, upstream probably ESPN | Same | **UNCLEAR** | [first_read_depth_chart_signals.md](../../fantasy-intel/docs/research/first_read_depth_chart_signals.md) |
| **NFL.com** (site and RSS) | Systematic retrieval to compile a database is prohibited without written consent (s1.3); bots and spiders for commercial harvesting barred (s11). | Personal, non-commercial, informational use only. | Written consent needed. | **NOT ALLOWED** | [NFL terms](https://www.nfl.com/legal/terms/) (primary excerpt) |
| **Team sites' RSS** (Broncos as example) | Current ToU bars systematic retrieval; RSS "Modular Content" falls under the same agreement. | Individual non-commercial use; older RSS page asks for the credit "DenverBroncos.com" and reserves the right to stop feeds. 32 teams, 32 terms. | Written consent for commercial use. | **NOT ALLOWED** for a multi-user app; one team checked, the rest **UNVERIFIED** | [Broncos ToU](https://www.denverbroncos.com/terms-of-use/), [RSS](https://www.denverbroncos.com/rss/) (primary excerpt) |
| **ESPN public JSON news endpoints** (undocumented site APIs) | Disney terms bar extracting material by automated tools, including web scraping and data mining; the exception is search-engine indexing. | No grant | n/a | **NOT ALLOWED** | [Disney terms](https://disneytermsofuse.com/english/) (primary excerpt; newest version found about 2.4 years old) |
| **ESPN RSS** (news feeds FAQ) | Feeds are offered for aggregators. | FAQ allows showing the feed's own content only, unmodified, with links to espn.com URLs, credited to ESPN, no ads. | Feed page is old (2019-2020 era) and sits against the Disney automated-access clause. | **UNCLEAR** (leaning no: unreconciled terms, no player-level feed) | [ESPN RSS FAQ](https://www.espn.com/espn/news/story?page=rssinfo) (primary excerpt) |
| **Sleeper API**: `/players/nfl`, trending add/drop | Docs: read-only, no token, about 1000 calls/min or risk an IP block, `/players/nfl` (about 5 MB) at most once a day. Terms (s11.3, last updated 2026-10-06): no automated access or scraping without written agreement. | Trending returns player ids and counts, not news. No news text endpoint confirmed. | Docs: free for non-commercial use, "give attribution to Sleeper", contact them for commercial use. Docs and terms are not reconciled. | **UNCLEAR**; not news, so not needed for this feature | [Sleeper docs](https://docs.sleeper.com/), [General Terms](https://support.sleeper.com/en/articles/5486620-general-terms-of-use), [embed post](https://sleeper.com/blog/how-to-embed-sleepers-trending-players-on-your-website/) (primary excerpts) |
| **Google News RSS** | Feed notice: for display in a personal feed reader, personal non-commercial use only; any other use prohibited. | No | n/a | **NOT ALLOWED** | [news.google.com/rss notice](https://news.google.com/rss?ceid=US%3Aen&gl=US&hl=en-US) (primary excerpt) |
| **Reddit API** (r/fantasyfootball) | Could not fetch the terms. A secondary guide says the Data API Terms (revised 2026-07-20) require a separate agreement for commercial use or use not expressly permitted, with fees at Reddit's discretion. | Unknown. User-generated posts are not news and carry deletion and rightsholder issues. | Rate limits conflict between sources (60 vs 100 per minute). | **UNCLEAR**; **UNVERIFIED** (primary page unreachable; Reddit is also blocked for search). Not recommended. | [Reddit Data API Terms](https://www.redditinc.com/policies/data-api-terms) (unreachable); secondary only |
| **Rotowire RSS** (player news blurbs) | RSS page says feeds can be incorporated into blogs and personal websites. T&Cs bar archiving, reproducing, distributing or displaying site content and limit use to personal non-commercial. | Contradictory | n/a | **UNCLEAR** (treat as no without written permission) | [RSS](https://www.rotowire.com/rss/), [T&C](https://www.rotowire.com/termsandconditions.php) (primary excerpt) |
| **CBS Sports RSS** | Feeds are "free for individual use". CBS terms bar unauthorized spidering, scraping and harvesting. | SportsLine content cannot be republished without permission. | n/a | **UNCLEAR** (leaning no) | [RSS](https://www.cbssports.com/xml/rss), [terms](https://legal.paramount.com/us/en/cbsi/terms-of-use) (primary excerpt) |
| **Yahoo Sports RSS** | General Yahoo terms: if you use a Yahoo RSS feed you may show it as provided, unmodified, with attribution and a link to the full article, no ads; Yahoo may discontinue any feed. Commercial reuse of Services content needs permission. | Plausible for a feed's own items | Whether a player-level Yahoo Sports news feed exists is **UNVERIFIED**. | **UNCLEAR** | [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) (primary excerpt) |

Pattern: the only clauses that look like display grants (ESPN, Yahoo) say "as provided, unmodified, credited, linked, no ads", sit on old feed pages, and conflict with newer site-wide automated-access bans. A hosted multi-user app that stores and re-serves per-player headlines also does not match "personal, non-commercial" feed-reader use, which is the common limit (NFL, Google, Broncos, CBS, Rotowire). Going further needs a written agreement, which a zero-dollar product should not depend on.

## Minimal design: "Availability events" (GO)

Purpose: tell a manager what the structured data says changed, not what happened or why.

**Allowed fields only.** `gsis_id`, `event_type`, `from_value`, `to_value`, `season`, `week`, `observed_at` (our retrieval clock), `provider_published_at` (nflverse `date_modified`, else NULL), `source` (a fixed label), `source_url` (the nflverse licence or release page). No free text from any provider. No article text, no headline, no quotes.

**Event types** (each derived by diffing consecutive retrievals; names stay literal):
- Injury report game designation changed (for example Questionable to Out), from `report_status`.
- Practice participation changed, from `practice_status`, shown as "Practice participation on the report: ..." only when the report has it. Never inferred when missing (PRODUCT.md: no invented practice status).
- Primary injury listed or changed, from the report field.
- Reserve placement or activation, from the `nfl_rosters_weekly` status and the existing `RULE_CODES` mapping (that mapping still "needs confirming against real synced rosters", per `player_availability.py`; confirm before display).
- Team change, from `nfl_rosters_weekly`.

**Wording.** App-written templates, sentence case, for example "Injury report status changed from Questionable to Out (week 8 report, retrieved Oct 22, 07:12 UTC)". No cause, no timeline, no "confirmed", no role or depth claim. The empty state says "No availability changes recorded in synced data" and never "healthy". Unmapped players show no events rather than a guess.

**Attribution** (CC BY 4.0 s3(a)). Under the tab: "Injury and roster data from nflverse (CC BY 4.0), reformatted by Fantasy Intel", linked to nflverse and the licence text. Repeat it in the page's `Method` block and `ProvenanceBadge` disclosure (source, capture time, coverage). State that events are derived and modified.

**Refresh cadence.** No new fetches. The events are computed from the existing nflverse sync (the source updates once daily at 07:00 UTC in season), so there is no added load. The tab shows an "as of" time and goes stale or partial under the existing 24 h threshold in `injury_freshness`. Cost: zero.

**Tenant safety.** The events table is global public data keyed by `gsis_id`, with no `league_id` column and no per-user data. Roster filtering ("my players") happens only server side:
- The route is a `/v2/leagues/{league_id}/...` route behind `authorize_league` / `league_tx`, per `docs/agent-security-review.md`.
- It joins the authorized league's rostered players to events through `player_id_map`. The client sends no player-id list and no `team` filter that the server trusts.
- Any response cache keys on `(league_id, data_version, global_version, visibility_epoch, perspective, route, params)` (security-review checklist, caches). A player-profile events view with no league context shows the global events only, and never marks "on your roster".
- Run `agent_check.py security` plus the v2 authorization matrix and league-scope audit before merge.

**Out of scope for v1, and why.**
- Raw headlines or summaries from any source in the table above.
- Sleeper trending adds and drops (a market signal, not news, with unreconciled terms).
- Depth-chart changes (upstream unverified).
- ESPN injury-status changes. `fantasy_players` is league-owned ESPN data (`app/db/league_split.py`), so events derived from it may not enter the global events table: that would show one user's ESPN sync to every tenant. If ever wanted, they belong in league-scoped state, served only behind that league's authorization.
- Scraping NFL.com injury pages (explicitly prohibited).

**Optional, low risk, not verified.** A plain outbound link from the profile to a player page on a source site (no fetch, no stored text) avoids the licensing question. Whether any source's terms restrict deep links is **UNVERIFIED**; check before adding.

## Decision requested

1. Record in DECISIONS.md: "#186: no free source with redistribution rights found (research PR). Player profile ships without a news tab. An 'Availability events' feed from nflverse (CC BY 4.0) is the substitute, labelled as structured events." (`DECISIONS.md` is not on `origin/main`; it lives only in the primary checkout, so this PR does not edit it.)
2. Open an implementation issue for Availability events, sized as a diff of existing `nfl_injuries` and `nfl_rosters_weekly` tables plus a v2 route and a profile tab.
3. Reopen the news question only if the owner obtains written permission from a publisher, or a source with an open licence appears.
