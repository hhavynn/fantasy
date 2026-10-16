# fantasy-intel

A private, local-only fantasy football war room for one ESPN league. It combines
**your ESPN league data**, **nflverse usage data**, and **manager behaviour analytics**
to answer the question ESPN doesn't: *what is actually happening underneath the
fantasy points, and how does my league behave?*

Everything runs on your laptop: Python/FastAPI + DuckDB backend, Next.js dashboard.
No accounts, no cloud, no LLM calls. All scores are deterministic and explainable.
Optionally, a read-only copy can be hosted for free on Vercel + MotherDuck; see
[Hosting](#hosting-vercel--motherduck).

---

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/) (Python manager) and Node.js 22+.

The app lives in the `fantasy-intel/` folder of this repository; every path below is
relative to it unless it says "from the repository root".

```bash
git clone <this repo> fantasy
cd fantasy/fantasy-intel
cp .env.example .env          # then edit .env (see "ESPN credentials" below)

cd backend
uv sync                       # installs Python 3.12 + deps into backend/.venv

uv run python ../scripts/sync_espn.py   # import your league (needs .env)
uv run python ../scripts/sync_nfl.py    # import current-season nflverse data

uv run uvicorn app.main:app --reload    # API on http://127.0.0.1:8000
```

In a second terminal (from the repository root):

```bash
cd fantasy-intel/frontend
npm install
npm run dev                   # dashboard on http://localhost:3000
```

Re-run either sync script any time to refresh (both are idempotent). Each sync
recomputes all analytics at the end. To recompute analytics without hitting the
network: `uv run python ../scripts/compute_analytics.py`.

> Run sync scripts with `uv run python ...` (not bare `python`) so they use the
> project's virtualenv. If the API is mid-request while a sync writes, the sync
> waits briefly for DuckDB's file lock; if a page shows "database is busy",
> just reload after the sync finishes.

### ESPN credentials

Private ESPN leagues need two browser cookies. Log into espn.com, open DevTools →
Application (Chrome) / Storage (Firefox) → Cookies → `https://www.espn.com`, and
copy:

| `.env` key       | cookie     | notes                                     |
|------------------|------------|-------------------------------------------|
| `ESPN_S2`        | `espn_s2`  | long URL-encoded string                   |
| `SWID`           | `SWID`     | includes the curly braces `{...}`         |
| `ESPN_LEAGUE_ID` | n/a        | `leagueId=` in your league URL            |
| `ESPN_SEASON`    | n/a        | e.g. `2026`                               |
| `ESPN_MY_TEAM_ID`| n/a        | optional; auto-detected by matching SWID  |

Secrets are only read from the environment and are never logged or stored.
ESPN identifies league members by their SWID, so every member id (yours included)
is replaced with a one-way hash before anything is written to DuckDB or the
raw-payload archive; your team is matched against your SWID before hashing.

---

## What's in the box

```
fantasy-intel/
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI app (uvicorn app.main:app)
│   │   ├── config.py          .env loading
│   │   ├── db/                schema.sql + connection/idempotent write helpers
│   │   ├── espn/              client (espn-api request layer), pure JSON parsers, sync
│   │   ├── nfl/               nflreadpy sync, ESPN↔gsis ID mapping
│   │   ├── analytics/         usage, trends, player scores, manager engine, waivers
│   │   └── api/               read-only JSON routes
│   └── tests/                 pytest (fixtures shaped like real ESPN payloads)
├── frontend/                  Next.js App Router dashboard
├── scripts/                   sync_espn.py, sync_nfl.py, compute_analytics.py
└── data/                      fantasy.duckdb + raw/ (archived ESPN JSON per sync)
```

### Dashboard pages

| Route              | What it shows |
|--------------------|---------------|
| `/`                | The weekly command center: your record and standing, this week's matchup card and prioritized action items (start/sit swaps, injured starters, top waiver claim), current roster with usage + classification, injuries (ESPN + NFL report), positional needs, standings, recent league transactions |
| `/matchup`         | This week's head-to-head (any week through the switcher): both teams' best projected lineups slot by slot, and the lineups as set in ESPN, with projected totals, margin and point share (a share of projected points, not a win probability) |
| `/start-sit`       | Your submitted lineup vs the best projected lineup that respects kickoff locks, each swap with its projected gain, availability alerts, bench and reserves; next week as a preview. ESPN projections, no invented numbers |
| `/players`         | Search; per-player profile: underlying value vs box score, usage trend table, weekly snaps/targets/shares/aDOT/RZ/GL/xFP |
| `/research`        | Research lab: the whole NFL WR/RB/TE pool. Expected points (xFP/G) against production (FPOE/G or PPR/G) over paired games for season, last 5 or last 3 games; position workload maps (WR target vs air-yard share, RB backfield share vs targets, TE snaps vs target share); role momentum (change in xFP vs change in PPR, with sample badges); a sortable leaderboard with a position-percentile heatmap; red-zone work with raw totals; and a weekly timeline that keeps byes and missed games distinct from zeros. URL-backed filters and selection. Descriptive research lists, no buy/sell calls. See `docs/research_lab.md` |
| `/waivers`         | Available players ranked by local waiver score, with role trend, roster fit, league competition (LOW/MED/HIGH + likely competitors), recommendation, suggested drop. Filter by position / recommendation |
| `/trades`          | Manual trade analyzer (any player counts up to 6 per side; teams switch instantly, Evaluate runs the URL-backed selection): both teams' optimal starting lineups rebuilt every remaining week before/after the trade on ESPN's projections; ROS / regular-season / playoff deltas, weekly table, starter changes, mandatory drop / open roster spot, hypothetical FA replacement, player context. No trade grade, no acceptance probability |
| `/trades/discover` | Trade opportunity discovery: exact lineup impact for both teams across every 1-for-1 / 2-for-1 / 1-for-2 trade with every manager, sortable by projected gains, with categories, manager context and a link into the manual evaluator. No acceptance estimate |
| `/luck-lab` | Luck vs. Skill Lab: expected wins from all-play, schedule luck (actual minus expected), schedule difficulty, brutal losses and fortunate wins, scoring consistency, and counterfactual records against other managers' opponents, over completed, fully observed regular-season periods. Historical and explanatory, read-only |
| `/schedule-intelligence` | Schedule intelligence: each remaining week's opponent (bye, home/away, regular season vs fantasy playoffs from league settings) and what that defense allowed in games presumed complete before the week (EPA per eligible pass play / designed rush, rush success, position target shares) with samples, ranks and insufficient-data reasons. Descriptive only |
| `/playoff-race` | Playoff Race Lab: actual standings, all-play and points; every remaining regular-season matchup with ESPN-projected optimal lineup totals (multi-week periods supported); a deterministic projected-standings scenario, the playoff cutline, bubble teams and high-leverage games; URL-backed winner overrides. No probabilities, tiebreaks, clinching or elimination claims. Read-only |
| `/my-team/value` | My Team Value: roster strength vs the league on comparable weeks, every player's exact lineup marginal value (regular/playoff, weekly starts, IR activation upside separately), slot strength and removal-scenario depth, unused bench projection with reasons, upgrade slot-weeks, realistic add/drop and a 1-for-1 trade preview, and the Playoff Race context. Read-only |
| `/managers`        | One card per manager: record, roster construction, waiver aggression, positional add interest, streaming, trades, confidence |
| `/managers/[id]`   | The evidence: every metric with numerator/denominator, league mean, z-score, sample size, rule; weekly roster composition; full transaction history; failed claims |
| `/data`            | ID-mapping summary, unmatched players, name-matched (medium confidence) players, sync history |

Every page shows last-sync timestamps in the header. A muted **—** always means
*missing* (hover for the reason). **0** is always a measured zero.

**Navigating.** `Ctrl+K` / `⌘K` (or `/` outside a text field) opens a command palette: quick
actions ("Decide who to start"), every page, and player search against the backend; the app bar
button does the same. The app bar groups pages (My team, Players, Trades, League) with a sub-nav
for the active group, and collapses into a menu on phones. Choose **System**, **Light** or **Dark**
in the app bar (System follows the OS); the choice is stored in the browser. Wide tables scroll
sideways inside their panel with the first column pinned, so they stay readable at 375px.

---

## Data sources and what is actually available

### ESPN (via [`espn-api`](https://github.com/cwendt94/espn-api) 1.0.x request layer)

We use the library's maintained endpoint/auth handling but store **raw JSON** so no
source field or ID is lost (archived under `data/raw/espn/<run>/`).

| Data | Source view | Stored in |
|------|-------------|-----------|
| League name, settings, scoring items, roster slots, FAAB, trade deadline | `mSettings` | `leagues` |
| Teams, owners, record, standings, waiver rank, FAAB spent, ESPN transaction counters (adds/drops/trades/IR moves, adds per matchup period) | `mTeam`, `mStandings` | `fantasy_teams`, `league_members` |
| Matchups / scores | `mMatchup` | `matchups` |
| Roster + lineup slot **per scoring period** (weekly lineups) | `mRoster` + `scoringPeriodId` | `lineup_snapshots` |
| Transactions (FA adds, waiver claims, drops, trades, lineup moves incl. IR), with timestamps and bids | `mTransactions2` per period | `transactions`, `transaction_items` |
| Activity feed (adds/drops/trade legs) | `kona_league_communication` | `activity_messages` |
| Free agents / waivers with % owned, % change, injury status | `kona_player_info` | `fantasy_players`, `player_ownership_history` |
| League-scoring points per player per week | `kona_playercard` | `espn_player_weekly_points` |
| ESPN weekly projections, current week through the league's final scoring period (rostered + FA + waivers) | `kona_player_info` + `scoringPeriodId`, one request per week | `espn_player_weekly_points.projected_points`, `espn_projection_history`, `player_ros_projections` |
| Draft | `mDraftDetail` | `draft_picks` |

Investigated / caveats:

- **Unsuccessful waiver claims**: ESPN exposes processed claims with `FAILED_*`
  statuses (and `WAIVER_ERROR`) in `mTransactions2`. Whether other teams' failed
  claims are visible depends on ESPN; the app records whatever is returned and
  only shows a `failed_claims` metric when any are visible.
- **Trades**: `mTransactions2` often omits the player list for trades that don't
  involve you, so trade legs are taken from the activity feed.
- **Waiver priority**: `waiverRank` per team (in FAAB leagues FAAB remaining is used instead).
- **Transaction timestamps**: `proposedDate` / `processDate` (ms epoch) are stored.
- **Historical injury status inside ESPN roster snapshots** may reflect the
  current status, so injury-context metrics use the nflverse weekly injury report
  and IR lineup slots instead.
- Not available from ESPN: pending claims of other managers, the time an
  injury news item broke (so "reaction speed" isn't measured).

### nflverse (via [`nflreadpy`](https://github.com/nflverse/nflreadpy), current season only)

`load_player_stats`, `load_snap_counts`, `load_injuries`, `load_schedules`,
`load_rosters_weekly`, `load_depth_charts`, `load_ff_opportunity` (expected
fantasy points), `load_ff_playerids`, `load_players`, `load_pfr_advstats`
(rush/rec/pass: contact yards, broken tackles, pressures), `load_nextgen_stats`
(rushing/receiving, qualified players only), `load_ftn_charting` (read
progression; numeric codes experimental), and `load_pbp` (aggregated into player-week usage, offensive
team-week environment and defensive team-week counts; the raw play-by-play
table is not stored).

**Every NFL metric definition is specified and verified in
[`docs/nfl_metrics_verified_contract.md`](docs/nfl_metrics_verified_contract.md)**
(authoritative; `docs/nfl_fantasy_metrics_research.md` is background research).

- **Route participation / routes run: unavailable.** nflverse participation data
  (`load_participation`) is not published for the in-progress season, so the app
  shows `unavailable` rather than approximating it. Snap share is shown instead.
  It is **not** approximated from FTN read codes either. Man/zone splits are unavailable.
- **FTN `read_thrown` numeric codes are UNVERIFIED.** The nflreadr dictionary documents
  `"0"` = primary read, but observed data conflicts; clarification is pending in
  nflverse/nflreadr#325. The app stores raw per-code counts and shows the code-`"1"` share
  only as an experimental field. It's never used in scores, signals or waivers (contract §3).
- **NGS** rows exist only for qualified players; missing NGS values stay NULL.
- **PFR advanced stats** lag the games by a few days; unpublished weeks are NULL.
- nflverse data typically lags games by a day; the header shows "stats thru wk N".

### ID mapping (ESPN → nflverse gsis)

Order of evidence: espn_id crosswalk in nflverse weekly rosters → nflverse
players table → ffverse `ff_playerids` → *unique* normalised name + compatible
position (+ team when ambiguous; marked `medium` confidence). D/ST maps to a team.
Anything else is stored as `unmatched`, logged during sync, and listed on `/data`.
Players are never joined by display name when an ID exists.

---

## Derived metrics (all in `backend/app/analytics`, all unit-tested)

### Player usage (`player_week_usage`, one row per player-game)
snap share, targets, **target share (targets / team targets)**, air yards, air-yard
share, WOPR, **aDOT** (missing when 0 targets), carries (official, includes kneels),
**team_rush_share** (rush plays incl. scrambles, excl. kneels/2-pt ÷ team rush plays),
**backfield_rush_share** (RBs: designed rushes ÷ team RB designed rushes; scrambles
never count), rushing success rate and explosive-run rate (designed rushes), PFR
yards before/after contact per attempt and broken tackles, red-zone targets,
red-zone and goal-line carries (designed rushes ≤ 20 / ≤ 5), FTN designed and checkdown targets, raw
FTN read-code counts (experimental code-"1" share, not used in any score), NGS rush yards over expected / separation (NULL when NGS has no
row), opportunities, nflverse PPR, ESPN league points, nflverse xFP.

A player with snaps but no box-score line is a measured zero; a game a source
didn't cover is NULL; bye/inactive weeks are skipped, never zero.

### Underlying value vs box score (`player_scores`)
- **Opportunity basis** is stored and shown everywhere: `xfp`, or `targets+carries`
  when xFP is missing (percentiles are computed separately per basis). Waivers add
  `espn_points` (K/D/ST, unmatched players) and `unavailable`.
- **Opportunity tier** (absolute, per position): recent xFP/game HIGH / MEDIUM at
  QB 17 / 13, RB 12 / 7, WR 12 / 7, TE 9 / 5 (targets+carries basis: RB 15 / 9,
  WR 7 / 4, TE 5 / 3).
- **Role trend**: recent vs earlier games on snap share, target share / backfield
  share / team rush share, volume and xFP; UP / FLAT / DOWN (needs ≥ 4 games).
- **Value gap**: season PPR/g − xFP/g; ≥ max(2.5, 20% of xFP) → production exceeds usage.
- **Production sustainability** (`production_sustainability`): ABOVE_EXPECTATION /
  ALIGNED / BELOW_EXPECTATION / INSUFFICIENT, i.e. box-score production vs the xFP
  baseline. This is **regression evidence, not a trade call**: xFP is an average-player
  model and elite players can beat it persistently (talent, QB, target type, YAC).
- **Market signal**: BUY / SELL / HOLD / WATCH / MONITOR, i.e. is there an actionable
  market mismatch? It's conservative: HOLD beats a false SELL.
  - LOW tier: a productive box score above expectation → SELL (sell-high); a rising role
    is only **WATCH**; otherwise HOLD.
  - MEDIUM/HIGH: a rising role with production not above expectation → BUY; production
    below expectation with the role not shrinking → BUY.
  - **HIGH tier is never SELL** while its recent absolute role is still HIGH. Above-
    expectation production or a short-term usage dip means regression risk, not a sell
    (HOLD with the reason). A real role collapse drops the recent tier to MEDIUM.
  - MEDIUM: above expectation + role FLAT/DOWN + fantasy-relevant box score → SELL;
    role DOWN + fantasy-relevant box score → SELL.
  - An INSUFFICIENT role trend never drives a SELL.

### Player context (`player_context`, `team_context`, `defense_context`)
Transparent, position-aware labels with their evidence (no master score): role,
role trend, efficiency (RB: YAC/att + success-rate percentiles with a ≥ 30-carry
sample; others: PPR vs xFP), scoring opportunity, run blocking (RB YBC), offensive
environment (next implied team total), matchup (opponent's season-to-date EPA
allowed rank, 1 = most generous; ≥ 3 games required) and data confidence. Team
context: PROE, neutral pass rate, sack rate, hit-or-sack rate (never `hit + sack`),
PFR pressure rate, implied team total (`(total ± spread) / 2`, positive spread =
home favored). Defense context: pass/rush EPA allowed, rush success and explosive
runs allowed, RB/TE target share allowed, all as season-to-date sums with sample sizes.

### Manager tendencies (`manager_metrics`, `manager_classifications`)
transactions/adds/drops per week, roster churn rate (adds ÷ roster spots),
positional add shares, average QBs/RBs/WRs/TEs rostered, pickups per week, starter
turnover, distinct starters and short-hold share per position, rookie add rate,
average hold length, quick-drop rate, injury replacement rate (newest same-week
injury report), trade count (activity feed ∪ transactions, deduplicated by leg set
within 3 days, provenance kept), 2-for-1 trades, players sent/received, IR moves,
FAAB spent %.

**Streaming** (QB/TE/K/D/ST) requires the *starter* to rotate: HIGH needs starter
turnover ≥ 50%, ≥ 3 distinct starters and ≥ 2 pickups; MEDIUM needs turnover ≥ 25%
and ≥ 2 starters. Frequent speculative adds behind a fixed starter are LOW.
Other labels are league-relative (±0.75 SD with absolute floors); every label keeps
its value, league mean/std, sample size, confidence and rule.

### Waiver engine (`waiver_recommendations`, `team_position_needs`)
- **Lineup model**: each team's optimal lineup is assigned slot by slot: fixed
  slots first, then FLEX / RB-WR / WR-TE / OP by exact search. A FLEX is **one**
  slot (2 RB + 2 WR + 1 FLEX = 5 starters), never an extra RB and an extra WR.
- **Availability**: AVAILABLE 1.0, QUESTIONABLE 0.75, DOUBTFUL 0.15, OUT/IR/SUSPENDED 0
  (most severe of ESPN status, IR slot, current-week NFL report). These are risk
  discounts, not play probabilities, and they're stored with each need.
- **Positional need** = 0.55·min(1, 2·shortage of availability-weighted bodies vs
  depth target) + 0.45·gap between the team's starters at the position (every lineup starter whose
  player position matches, including FLEX/OP-assigned ones) and the league average.
- **Roster fit** = 10·(0.35·need + 0.35·clamp(lineup gain / 3) + 0.30·upgrade over
  the suggested drop × depth utility); lineup gain is the actual change in the
  optimal lineup's value if the player is added.
- **Suggested drop** = lowest-value non-starter (not IR) whose removal leaves every
  fixed slot fillable.
- **Opportunity** = player opportunity score ± 1 for role trend, minus 0.5 / 2 / 3
  for Questionable / Doubtful / Out-IR. Explanations name the true basis and
  include context lines (e.g. "82% backfield rush share over last 2 games",
  "opponent bottom-third defense by rush epa allowed").
- **League interest** = LOW / MEDIUM / HIGH with likely competitors (not a probability).
- **Recommendation**: score = 0.55·opportunity + 0.45·fit (+ scarcity bonus when he'd
  help). CLAIM needs score ≥ 6.0, fit ≥ 3.0 and must beat the drop by ≥ 0.5 value;
  WAIT ≥ 4.5; else IGNORE. Unavailable players are never CLAIM.

### Validation
Each analytics run validates the NFL pipeline. **Errors** (logical corruption:
shares outside 0..1, receptions > targets, goal-line > red-zone carries, red-zone >
carries/targets, duplicate keys, hit-or-sack outside [sacks, dropbacks], bad
implied-total arithmetic, negative broken tackles) roll the run back. **Warnings**
(odd but possible: negative xFP, extreme PROE, unusual air-yard share or play counts,
extreme contact metrics, target shares not summing to 1, the observed FTN read-code distribution (descriptive; semantics unresolved), missing
NGS rows, ESPN→GSIS mapping failures, rejected team-snap estimates) are kept and
shown on `/data`.

## External utilization (routes, situational usage)

nflverse doesn't publish routes in-season. An authorized/manual provider file in the
generic normalized CSV format can be imported (provider-neutral; no vendor code in this repo):

```bash
cd backend
uv run python ../scripts/import_external_usage.py ../data/imports/external/FILE.csv \
  --provider NAME --dataset NAME
uv run python ../scripts/map_external_player.py --provider NAME --source-player-key KEY --gsis-id ID  # fix unmatched
```

Rows are validated (a hard error rejects the whole file), identity-resolved (unmatched rows
are kept and listed on `/data`), stored with provenance, and LEFT JOINed into
`player_week_usage` (`routes`, `route_participation`, `tprr`/`yprr` from **nflverse**
targets/yards ÷ provider routes, alignment, situational shares). They appear on the player
page but **don't affect scores or recommendations yet**. Keep provider files under
`data/imports/` (git-ignored). Full spec:
[`docs/external_utilization_contract.md`](docs/external_utilization_contract.md).

## ESPN forward projections (V2 baseline)

Each ESPN sync stores ESPN's projection for every remaining fantasy week (current week through the
league's final scoring period) and builds a rest-of-season summary with a regular-season / fantasy-playoff
split from league settings, coverage and NFL-schedule byes. These are **ESPN's numbers, shown unmodified**:
they are the forward fantasy-point baseline, not our model, and they do not feed any score, signal or
waiver recommendation. Missing projections stay unknown (never 0), and a failed week keeps its previous
values. A player ESPN omits only loses a stored projection when the response provably holds ESPN's whole
universe (it matches ESPN's own player count). Details: [`docs/espn_ros_projections.md`](docs/espn_ros_projections.md).

## Lineup-based trade evaluator (V2)

`POST /api/trades/evaluate` (and the `/trades` page) values a trade by what it does to both teams' actual
starting lineups: every remaining fantasy week is optimised before and after the trade with the league's real
slots and exact FLEX rules, on ESPN's weekly projections (unmodified). Bench points never count, missing
projections are never 0, roster capacity is enforced with a least-costly forced drop, open roster spots are
never auto-filled, and hypothetical free-agent pickups are shown separately. No generic trade values, no
consolidation tax, no playoff weighting, no acceptance probability. Analytics also stores every team's weekly
optimal projected lineup (`team_week_lineup_projections`, `team_ros_lineup_projections`). Read-only helpers:
`GET /api/trades/options`, `GET /api/lineup-projections` (team summaries + replacement market),
`GET /api/teams/{id}/lineup-projection` (weekly lineups + each player's marginal lineup value).
Details: [`docs/v2_trade_evaluator.md`](docs/v2_trade_evaluator.md).

## Trade intelligence (V3)

The manual analyzer takes any trade shape up to a configurable per-side limit (`TRADE_MAX_PLAYERS_PER_SIDE`,
default 6) with exact forced drops for any number of removals, and its team/player selection is instant
(client-side; only Evaluate runs an evaluation). Trade Discovery defaults to **Practical trades**: both lineups
improve, the asset packages (points above the best available free agent, independent of bench congestion) are
not lopsided against the other manager, and injured or incomplete packages are set apart; Mutual, Maximum upside
and All candidates modes remain. K and D/ST are excluded from automatic discovery unless asked for; 2-for-2 to
3-for-3 come from a labelled heuristic beam search. NFL reserve-list eligibility (separate from ESPN status and the
fantasy IR slot) removes verified ineligible weeks from every lineup simulation, with return scenarios for injured
traded players. No acceptance probability. Details: [`docs/v3_trade_intelligence.md`](docs/v3_trade_intelligence.md).

Every evaluated or discovered trade also gets a **Trade vs. free agency** panel (V3.1): for each manager, the best
single legal acquisition of a currently available player (ESPN free agents; waiver claims shown separately as not
guaranteed), and what the trade still adds once that move is made. It never enters the canonical delta. Details:
[`docs/v3_1_trade_vs_free_agency.md`](docs/v3_1_trade_vs_free_agency.md).

Trades, discovery, My Team Value and waivers value **K and D/ST against weekly streaming**. Each week either slot
can be filled at the projection of the 3rd-best available player, so a kicker or defense counts only for what it
adds above streaming. Pass `kdst_streaming=false` to hold current K/D/ST all season instead. Details:
[`docs/kdst_streaming_baseline.md`](docs/kdst_streaming_baseline.md).

## Trade opportunity discovery (V2)

`GET /api/trades/discover` (and the `/trades/discover` page) searches every 1-for-1, 2-for-1 and 1-for-2 trade
between your team (`is_mine`, or any `team_id`) and every other roster, and returns the best ones by your
projected gain, their gain, combined gain, smaller-side gain or playoff gain, with categories (mutual
improvement, helps me/costs them, consolidation, playoff- or regular-season-focused). Every number is the
manual evaluator's exact lineup simulation (each shown result is re-run through it and checked); provable
upper bounds skip candidates that cannot help you, and a best-first search returns a provably exact top K.
Manager scouting is shown as secondary context. No trade-value chart, no new projection, no acceptance
estimate; read-only. Details: [`docs/v2_trade_discovery.md`](docs/v2_trade_discovery.md).

## Luck vs. Skill Lab (V2, historical, read-only)

`GET /api/luck-lab` (and `/luck-lab`) compares each team's weekly scoring rank with its head-to-head results
over completed, fully observed regular-season periods: expected wins (all-play win equivalents, the same pairwise
comparison as the Playoff Race Lab), schedule luck, opponent strength, notable results, consistency, and a
counterfactual explorer that keeps a team's scores and swaps in another manager's opponents. Explanatory only,
no probabilities. Details: [`docs/v2_luck_lab.md`](docs/v2_luck_lab.md).

## Schedule intelligence (V2, descriptive)

`GET /api/schedule-intelligence` (and `/schedule-intelligence`) shows, for each QB/RB/WR/TE of a fantasy team (yours
by default) and each remaining scoring period, the opponent and that defense's observed allowances in games
presumed complete **before** the week (no lookahead; a conservative timing heuristic, not an official final
status, so a suspended game with a partial score can still count; see the doc), with numerators,
denominators, games sampled, ranks and explicit missing reasons. Computed on request from `nfl_defense_week` and
`nfl_schedules`; read-only; no effect on projections, scores, waivers, scouting or trades.
Details and the source-data audit: [`docs/v2_schedule_intelligence.md`](docs/v2_schedule_intelligence.md).

## Playoff Race Lab (V2, read-only)

`GET /api/playoff-race` (and `/playoff-race`) starts from ESPN's verified standings and adds the higher
ESPN-projected side of every remaining regular-season matchup (canonical weekly lineup optimizer, missing
projections left unresolved, games already under way never projected) as one deterministic scenario. Override
any open game with `override=MATCHUP_ID:home|away|tie|none` (the page keeps them in the URL). No tiebreak,
division, clinch or elimination rule is applied or claimed. Details and the data audit:
[`docs/v2_playoff_race.md`](docs/v2_playoff_race.md).

## My Team Value (V2, read-only)

`GET /api/my-team/value` (and `/my-team/value`) turns the canonical lineup engine into decisions for one team:
league rank on comparable weeks (weeks every team's projection is complete), each player's exact lineup marginal
value (never summed), depth from explicit removal scenarios, unused bench projection, free-agent and add/drop
upgrades with roster capacity, a 1-for-1 trade preview checked against the manual evaluator, and the Playoff
Race Lab context with re-projected matchups. Details: [`docs/v2_team_value.md`](docs/v2_team_value.md).

## Upgrading an existing database (schema v6)

The DuckDB schema is now **v6** (v2: new metrics; v3: FTN read-code columns renamed to
`ftn_read*`; v4: external route columns on `player_week_usage` + `external_*` tables; v5: create-only,
adds `espn_projection_history` + `player_ros_projections`; v6: adds the derived
`team_week_lineup_projections` + `team_ros_lineup_projections`). On the first
read-write open (any sync script), derived tables are dropped and recreated (v5 drops nothing; v6 touches only
its two lineup tables); raw ESPN and nflverse tables and projection history are kept. Then rebuild:

```bash
cd backend
uv run python ../scripts/sync_nfl.py      # rebuilds PBP/PFR/FTN/NGS-derived tables + analytics
uv run python ../scripts/sync_espn.py     # optional: refresh league + recompute waivers/managers
```

Until then the API returns a 503 asking you to re-sync.

---

## Sleeper source (phase 1: library only)

`backend/app/sleeper/` reads public Sleeper leagues (league, users, rosters, NFL state, players)
by explicit league id, normalises them to the same row shapes as the ESPN parser and maps
Sleeper player ids to nflverse gsis ids. Nothing is stored in DuckDB or shown in the dashboard
yet. Caches live in `data/sleeper/` (git-ignored); `/players/nfl` is fetched at most once a day.
Details, id rules and the remaining single-league lookups: [`docs/sleeper_foundation.md`](docs/sleeper_foundation.md).

Optional live check (skipped unless the variable is set):

```bash
cd backend
SLEEPER_LIVE_LEAGUE_ID=<public league id> uv run pytest tests/test_sleeper_live.py -s
```

## Hosting (Vercel + MotherDuck)

Optional. Syncs still run on your laptop against the local DuckDB file. A publish step
copies that file to [MotherDuck](https://motherduck.com) (hosted DuckDB), and two Vercel
projects serve it: the FastAPI backend reads MotherDuck, and the Next.js dashboard calls the
backend. The free tiers of both cover this. The hosted side only reads: ESPN
cookies stay in your local `.env`.

```
laptop: sync_espn.py / sync_nfl.py -> data/fantasy.duckdb -> publish_motherduck.py
                                                                    |
Vercel: dashboard (SITE_PASSWORD) -> API (X-API-Key) -> md:fantasy_intel
```

**1. MotherDuck.** Sign up (the free Lite plan is enough) and create two access tokens under
Settings -> Access Tokens: one for your laptop and one for the API, so either can be revoked on
its own. Both are normal read/write tokens: read-only read-scaling tokens need MotherDuck's paid
Business plan. The API never writes (it only runs SELECTs, with `saas_mode` on), and only the
dashboard can call it (`FANTASY_API_KEY`); if you are on Business, a read-scaling token works too.

**2. Publish from your laptop.** Add the laptop token to `.env` as `MOTHERDUCK_TOKEN`,
then after any sync:

```bash
cd backend
uv run python ../scripts/publish_motherduck.py     # replaces md:fantasy_intel
```

Do not put `MOTHERDUCK_DATABASE` in your local `.env`: it switches the local API to
MotherDuck too.

**3. Backend project on Vercel.** Import this repository, set Root Directory to
`fantasy-intel/backend` (FastAPI is detected; `[tool.vercel] entrypoint = "app.main:app"`).
Environment variables:

| variable              | value                                             |
|-----------------------|---------------------------------------------------|
| `MOTHERDUCK_TOKEN`    | the API's access token from step 1               |
| `MOTHERDUCK_DATABASE` | `fantasy_intel`                                   |
| `FANTASY_API_KEY`     | a long random string (`openssl rand -hex 32`)     |

Vercel installs with `uv sync --no-dev`, which skips the sync-only packages (polars,
nflreadpy, espn-api live in the `sync` dependency group) and keeps the function bundle
around 310 MB, under Vercel's 500 MB limit. `tests/test_hosting.py` fails if an API route
starts importing them. Cold starts download the MotherDuck extension (a couple of seconds).
The offline gradient-boosting research (`scripts/hgb_research.py`) needs scikit-learn, which
lives in the `research` group (included by `dev`, so also skipped by `--no-dev`);
`tests/test_hgb_research.py` fails if the API ever loads it.

**4. Dashboard project on Vercel.** Import the repository again with Root Directory
`fantasy-intel/frontend`. Environment variables:

| variable        | value                                                   |
|-----------------|---------------------------------------------------------|
| `API_URL`       | the backend's production URL, e.g. `https://<backend>.vercel.app` |
| `API_KEY`       | the same value as the backend's `FANTASY_API_KEY`       |
| `SITE_PASSWORD` | the password the browser will ask for (any username)    |

`SITE_PASSWORD` matters: without it anyone with the URL sees your league. Locally none of
these are set, so the API key check and the password prompt are off.

**Deployments run for `main` only.** `backend/vercel.json` and `frontend/vercel.json` set
`git.deploymentEnabled` to `{"**": false, "main": true}`: a push to any other branch creates no Vercel
deployment (Vercel deploys when at least one matching rule is true), which keeps the free plan's daily
deployment limit for production. Pull requests get no preview link; the GitHub Actions checks still run.
Vercel reads the rule from the pushed commit, so a branch created before this change deploys until it
merges `main`.

**Refreshing the hosted data:** run the syncs, then `publish_motherduck.py`. No redeploy is
needed; the API reads the new copy on the next request.

## Scheduled refresh (macOS launchd)

ESPN projections are only stored as history (`espn_projection_history`) when a sync runs, and
the projection promotion tracker only counts snapshots taken **before kickoff**. A week without
a pregame sync has no ESPN baseline, so the refresh should run on a schedule. ESPN cookies live
in your local `.env`, so it has to run on your Mac (not in GitHub Actions).

`scripts/refresh_all.py` runs `sync_espn.py` → `sync_nfl.py` → `compute_analytics.py`, then
`publish_motherduck.py` only if `MOTHERDUCK_TOKEN` is set. Each step runs in its own process,
the first failure stops the chain and the script exits 1. Every run writes
`data/logs/refresh-<UTC stamp>.log`. At the end it checks that the current week's ESPN
projections were snapshotted before that week's first kickoff and prints a `WARNING:` line if
not (`/data` shows the same check under "Independent projections: path to promotion").

```bash
cd backend
uv run python ../scripts/refresh_all.py                # full refresh (+ publish when configured)
uv run python ../scripts/refresh_all.py --no-publish   # never touch MotherDuck
uv run python ../scripts/refresh_all.py --check-only   # only the pregame snapshot check
```

`scripts/launchd/com.fantasy-intel.refresh.plist` is a template; nothing installs it for you.
It runs the refresh on **Thursday 3pm, Sunday 8am, Monday 3pm and Tuesday 8am**, in the
Mac's local time zone (the times assume Pacific; edit them otherwise). A run missed while the
Mac sleeps fires when it wakes; one missed while it is shut down is skipped.

**Install** (from `fantasy-intel/` in the checkout that has your `.env`):

```bash
mkdir -p data/logs ~/Library/LaunchAgents
sed -e "s|__FANTASY_INTEL_DIR__|$PWD|g" -e "s|__UV__|$(command -v uv)|g" scripts/launchd/com.fantasy-intel.refresh.plist > ~/Library/LaunchAgents/com.fantasy-intel.refresh.plist
plutil -lint ~/Library/LaunchAgents/com.fantasy-intel.refresh.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fantasy-intel.refresh.plist
```

Run it once now to confirm it works under launchd: `launchctl kickstart gui/$(id -u)/com.fantasy-intel.refresh`.

**Check it ran:**

```bash
launchctl print gui/$(id -u)/com.fantasy-intel.refresh | grep -E "state|runs|last exit code"
ls -t data/logs/refresh-*.log | head -3                       # newest run first
tail -n 30 "$(ls -t data/logs/refresh-*.log | head -1)"       # FAILED / WARNING lines are at the end
cat data/logs/launchd.err.log                                 # uv or permission errors before the log opened
```

**Uninstall:**

```bash
launchctl bootout gui/$(id -u)/com.fantasy-intel.refresh
rm ~/Library/LaunchAgents/com.fantasy-intel.refresh.plist
```

After editing the template, uninstall and install again. If the repository lives under
`~/Documents`, `~/Desktop` or `~/Downloads`, macOS privacy protection can block a launchd job
from reading it (`Operation not permitted` in `launchd.err.log`, with no refresh log). Grant
Full Disk Access to `uv` and to the Python in `backend/.venv` (System Settings → Privacy &
Security), or keep the checkout somewhere else.

## Weekly learning cycle (macOS launchd)

The independent projection engine's research cycle (champion `ifp-1.0.0` refit + shadow
challenger `ifp-2.0.0`, frozen pregame forecasts, prospective evaluation) runs separately from the
refresh: `scripts/learning_cycle.py run` locks, checks its dependencies, runs `sync_nfl.py` and the
play-by-play injury sync, picks the next week that has not kicked off, checks that every earlier
game is final with box scores, snaps and nflverse expected points, evaluates earlier frozen
forecasts, trains (reusing the previous artifacts when the training data, configuration and scoring
are unchanged) and freezes pregame forecasts. It reads the production `ifp-1.0.0` files and never
writes them, the live projection tables, ESPN or MotherDuck; nothing a page or lineup reads
changes. Details: docs/independent_projection_engine.md, "Automated weekly cycle".

It needs the writable `data/fantasy.duckdb`, `data/nfl_history.duckdb` and the git-ignored
`data/models/` (production `selection.json`, `heldout_report.json`; outputs in
`data/models/weekly/`), which only exist on your Mac: GitHub Actions and Vercel cannot run it.

```bash
cd backend
uv run python ../scripts/learning_cycle.py run                # one cycle (what launchd runs)
uv run python ../scripts/learning_cycle.py run --skip-sync    # data already synced
uv run python ../scripts/learning_cycle.py status             # latest report, lock, missed weeks
uv run python ../scripts/learning_cycle.py run --force-retrain   # refit after a stat correction
uv run python ../scripts/weekly_learning.py evaluate          # prospective evaluation only
```

Exit codes: 0 frozen or already up to date, 3 data not ready (retry before kickoff), 4 another
cycle is running, 5 missing dependency (the report says what to do), 1 a stage failed.

`scripts/launchd/com.fantasy-intel.weekly-learning.plist` is a template; nothing installs it for
you. It runs **Tuesday, Wednesday and Thursday at 7am** (Mac local time; the times assume
Pacific, edit them otherwise), an hour before the Tuesday refresh (if both fire together when a
sleeping Mac wakes, the cycle's sync step fails on the DuckDB write lock and the next attempt
retries); a later attempt only redoes what the earlier one could not finish. Install from
`fantasy-intel/`:

```bash
mkdir -p data/logs ~/Library/LaunchAgents
sed -e "s|__FANTASY_INTEL_DIR__|$PWD|g" -e "s|__UV__|$(command -v uv)|g" scripts/launchd/com.fantasy-intel.weekly-learning.plist > ~/Library/LaunchAgents/com.fantasy-intel.weekly-learning.plist
plutil -lint ~/Library/LaunchAgents/com.fantasy-intel.weekly-learning.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fantasy-intel.weekly-learning.plist
launchctl kickstart gui/$(id -u)/com.fantasy-intel.weekly-learning     # optional: run once now
```

Check: `uv run python ../scripts/learning_cycle.py status` (from `backend/`), or
`data/models/weekly/status/latest-cycle.json`; `data/logs/weekly-learning.err.log` for uv errors.
Uninstall: `launchctl bootout gui/$(id -u)/com.fantasy-intel.weekly-learning` and delete the plist.

A sleeping Mac runs the missed attempt on wake; a Mac that is shut down skips it. For unattended
runs keep it plugged in and optionally schedule a wake a few minutes before the attempts, e.g.
`sudo pmset repeat wakeorpoweron TWR 06:55:00` (one `repeat` schedule per Mac; this replaces any
existing one, so check `pmset -g sched` first). If every attempt fails before a week's first
kickoff, that week has no pregame forecast: the next cycle records it under
`data/models/weekly/missed/` and nothing is backfilled.

## Tests

```bash
cd backend
uv run pytest
```

Covers: schema init + v2 migration, idempotent ESPN sync, SWID handling,
missing-vs-zero handling, ID mapping, lineup assignment (FLEX counted once; FLEX
picks the best RB/WR/TE; non-nested flex slots; superflex), team vs backfield rush
share, kneel / two-point / scramble filters, hit-or-sack without double counting,
FTN read codes kept experimental and isolated from scoring, uncharted games, latest same-week injury report,
Doubtful/Questionable availability in needs, opportunity basis in explanations,
market signals (tiny rising role → WATCH, strong rising role → BUY, etc.),
streaming requiring starter turnover, trade-source union/dedup, target-share sums,
NGS NULLs, implied-total sign/arithmetic, team/defense aggregation keys,
validation errors vs warnings, and an end-to-end API smoke test.

### Browser smoke test (Playwright)

`frontend/e2e/routes.spec.ts` loads every page in `frontend/lib/navigation.ts`, a manager detail page
(`/managers/1`), a player profile (`/players?id=201`) and `/data` in Chromium, in the Light and Dark
themes at 1440px and 375px wide. Each load must return HTTP 200 with exactly one `h1`, zero console
errors, no backend or render error state and no horizontal page scroll (the PRODUCT.md quality bar).

It needs no ESPN cookies or network: Playwright builds a synthetic DuckDB at `data/e2e.duckdb` with
`scripts/build_e2e_db.py` (the ESPN fixture league from `backend/tests/espn_fixture.py` plus the
synthetic NFL schedule from `backend/tests/synthetic_nfl.py`), serves it with uvicorn on port 8765,
and runs `next start` on port 3100, so a dev server on 3000/8000 is left alone.

```bash
cd backend && uv sync                  # once
cd ../frontend
npx playwright install chromium        # once per Playwright version
npm run build                          # the test runs the production build
npm run e2e                            # add --ui or --headed to watch
```

A failure keeps a trace in `frontend/test-results/`; open it with
`npx playwright show-trace <path>/trace.zip`. A page added to `lib/navigation.ts` is covered
automatically; a new route outside the nav goes in the `ROUTES` list of the spec.

## Continuous Integration (CI)

Every push to `main` and pull request targeting `main` automatically triggers GitHub Actions (`.github/workflows/ci.yml`).

### What CI checks

- **Backend job (`backend`)**: Runs on Ubuntu with Python 3.12 and `uv`. Installs pinned dependencies from `backend/uv.lock` and executes the full pytest suite.
- **Frontend job (`frontend`)**: Runs on Ubuntu with Node.js 22 LTS. Installs pinned dependencies from `frontend/package-lock.json` with `npm ci`, runs ESLint, verifies TypeScript types (`npm run typecheck`), runs the unit tests (`npm test`), and compiles a production build (`npm run build`).
- **E2E job (`e2e`)**: Installs both, builds the frontend, and runs the Playwright route smoke test (`npm run e2e`, see [Browser smoke test](#browser-smoke-test-playwright)) against the backend serving the synthetic DuckDB. On failure it uploads the traces and HTML report as the `playwright-traces` artifact.

### Reproducing checks locally

From the repository root:

```bash
# Backend checks
cd fantasy-intel/backend
uv sync --frozen
TZ=UTC uv run pytest

# Frontend checks
cd ../frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run e2e
```

### What to do when CI fails

1. Check the GitHub Actions run log to determine which job and step failed.
2. If backend tests fail: re-run the failing test locally with `TZ=UTC uv run pytest tests/<test_file>.py -vv`. Note that tests expect UTC timezone semantics.
3. If frontend lint fails: run `npm run lint` locally and resolve rule violations.
4. If frontend typecheck fails: run `npm run typecheck` (which runs `next typegen` followed by `tsc --noEmit`).
5. If frontend unit tests fail: run `npm test` locally (Node 22.6+ is required for `--experimental-strip-types`).
6. If frontend build fails: run `npm run build` locally and inspect Next.js build errors.
7. If the E2E smoke test fails: download the `playwright-traces` artifact and open the failing test's `trace.zip` with `npx playwright show-trace`, or run `npm run build && npm run e2e` locally. The test name says the route and theme; the project name (`desktop-1440` or `mobile-375`) says the width.

### Isolation and environment requirements

- **No live ESPN access**: CI runs without ESPN credentials (`ESPN_S2`, `SWID`) and never contacts ESPN or external network services. All tests run against deterministic in-memory synthetic league fixtures and synthetic NFL data.
- **Local-only tests**: `test_real_league_smoke_read_only` in `backend/tests/test_trade_discovery.py` checks for a local DuckDB file (`data/fantasy.duckdb`) and is skipped without one. `backend/tests/test_sleeper_live.py` calls the real Sleeper API only when `SLEEPER_LIVE_LEAGUE_ID` is set. Both are skipped in CI; Sleeper unit tests use recorded fixtures in `backend/tests/fixtures/sleeper/`.

## Troubleshooting

- **`ESPNAccessDenied`**: `ESPN_S2`/`SWID` missing, expired or copied without the braces.
- **"Your team wasn't identified"**: set `ESPN_MY_TEAM_ID` (your team's number in the ESPN URL `teamId=`).
- **Dashboard says backend offline**: start `uv run uvicorn app.main:app --reload` in `backend/`.
  The frontend reads `API_URL` (default `http://127.0.0.1:8000`).
- **Unmatched players**: see `/data`. They keep their ESPN data but have no NFL usage.

## Roadmap (V2)

Next: league-specific weekly lineup simulation + replacement value on top of the ESPN forward
baseline, then trade evaluation. Also: trade analyzer (lineup-aware, both sides), FAAB bid guidance once enough bid history
exists, claim-probability modelling after a season of failed-claim data, multi-season
manager history (ESPN `leagueHistory`), matchup-aware start/sit, and an optional LLM
layer that narrates the stored components (the deterministic scores stay the source of truth).
