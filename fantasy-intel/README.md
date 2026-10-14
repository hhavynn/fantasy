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
- **Market signal**: BUY / SELL / HOLD / WATCH / MONITOR. A rising role on a LOW
  tier is only **WATCH**; BUY needs a MEDIUM/HIGH role that is rising (without
  production outrunning it) or under-producing its role. SELL = production
  outrunning the role (on any tier if the box score is fantasy-relevant) or a
  shrinking role with a strong box score.

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

## Upgrading an existing database (schema v4)

The DuckDB schema is now **v4** (v2: new metrics; v3: FTN read-code columns renamed to
`ftn_read*`; v4: external route columns on `player_week_usage` + `external_*` tables). On the first
read-write open (any sync script), derived tables are dropped and recreated; raw
ESPN and nflverse tables are kept. Then rebuild:

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

## Troubleshooting

- **`ESPNAccessDenied`**: `ESPN_S2`/`SWID` missing, expired or copied without the braces.
- **"Your team wasn't identified"**: set `ESPN_MY_TEAM_ID` (your team's number in the ESPN URL `teamId=`).
- **Dashboard says backend offline**: start `uv run uvicorn app.main:app --reload` in `backend/`.
  The frontend reads `API_URL` (default `http://127.0.0.1:8000`).
- **Unmatched players**: see `/data`. They keep their ESPN data but have no NFL usage.

## Roadmap (V2)

Trade analyzer (lineup-aware, both sides), FAAB bid guidance once enough bid history
exists, claim-probability modelling after a season of failed-claim data, multi-season
manager history (ESPN `leagueHistory`), matchup-aware start/sit, and an optional LLM
layer that narrates the stored components (the deterministic scores stay the source of truth).
