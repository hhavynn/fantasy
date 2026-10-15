# fantasy-intel

A private, local-only fantasy football war room for one ESPN league. It combines
**your ESPN league data**, **nflverse usage data**, and **manager behaviour analytics**
to answer the question ESPN doesn't: *what is actually happening underneath the
fantasy points, and how does my league behave?*

Everything runs on your laptop: Python/FastAPI + DuckDB backend, Next.js dashboard.
No accounts, no cloud, no LLM calls. All scores are deterministic and explainable.

---

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/) (Python manager) and Node.js 20+.

```bash
git clone <this repo> fantasy-intel
cd fantasy-intel
cp .env.example .env          # then edit .env (see "ESPN credentials" below)

cd backend
uv sync                       # installs Python 3.12 + deps into backend/.venv

uv run python ../scripts/sync_espn.py   # import your league (needs .env)
uv run python ../scripts/sync_nfl.py    # import current-season nflverse data

uv run uvicorn app.main:app --reload    # API on http://127.0.0.1:8000
```

In a second terminal:

```bash
cd frontend
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
| `/`                | League, your record/standing, current roster with usage + classification, injuries (ESPN + NFL report), positional needs, standings, recent league transactions |
| `/players`         | Search; per-player profile: underlying value vs box score, usage trend table, weekly snaps/targets/shares/aDOT/RZ/GL/xFP |
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

## Continuous Integration (CI)

Every push to `main` and pull request targeting `main` automatically triggers GitHub Actions (`.github/workflows/ci.yml`).

### What CI checks

- **Backend job (`backend`)**: Runs on Ubuntu with Python 3.12 and `uv`. Installs pinned dependencies from `backend/uv.lock` and executes the full pytest suite.
- **Frontend job (`frontend`)**: Runs on Ubuntu with Node.js 22 LTS. Installs pinned dependencies from `frontend/package-lock.json` with `npm ci`, runs ESLint, verifies TypeScript types (`npm run typecheck`), and compiles a production build (`npm run build`).

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
npm run build
```

### What to do when CI fails

1. Check the GitHub Actions run log to determine which job and step failed.
2. If backend tests fail: re-run the failing test locally with `TZ=UTC uv run pytest tests/<test_file>.py -vv`. Note that tests expect UTC timezone semantics.
3. If frontend lint fails: run `npm run lint` locally and resolve rule violations.
4. If frontend typecheck fails: run `npm run typecheck` (which runs `next typegen` followed by `tsc --noEmit`).
5. If frontend build fails: run `npm run build` locally and inspect Next.js build errors.

### Isolation and environment requirements

- **No live ESPN access**: CI runs without ESPN credentials (`ESPN_S2`, `SWID`) and never contacts ESPN or external network services. All tests run against deterministic in-memory synthetic league fixtures and synthetic NFL data.
- **Local-only tests**: Exactly one test (`test_real_league_smoke_read_only` in `backend/tests/test_trade_discovery.py`) checks for a local DuckDB file (`data/fantasy.duckdb`). It is safely skipped in CI and on machines without a configured local ESPN database.

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
