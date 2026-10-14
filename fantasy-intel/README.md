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
fantasy points), `load_ff_playerids`, `load_players`, and `load_pbp` (aggregated
to red-zone / goal-line usage and team opportunity totals; the full play-by-play
table is not stored).

- **Route participation / routes run: unavailable.** nflverse participation data
  (`load_participation`) is not published for the in-progress season, so the app
  shows `unavailable` rather than approximating it. Snap share is shown instead.
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
snap share (`offense_pct`), targets, target share, receptions, receiving yards,
air yards, **aDOT** = air yards / targets (missing when 0 targets), WOPR,
carries, **rush share** = carries / team carries (designed runs + scrambles, no
kneels/2-pt), yards per carry, red-zone targets/carries (yardline ≤ 20), goal-line
carries (≤ 5), opportunities = targets + carries, nflverse PPR points, ESPN league
points, **xFP** (nflverse expected PPR points from the role).

A player with snaps but no box-score line is a measured zero; a player with no
snap record has *unknown* snap share; bye/inactive weeks are skipped, never zero.

### Underlying value vs box score (`player_scores`)
- **Trend**: recent games (last 2, or last 3 once a player has 6+ games) vs his
  earlier games. Needs ≥ 4 games. Per-metric thresholds (e.g. snap share ±8 pts,
  target share ±4 pts, xFP ±2.0). Composite = mean of clamped delta/threshold → UP/FLAT/DOWN.
- **Opportunity score** 0-10: position percentile of recent xFP/game.
- **Production score** 0-10: position percentile of recent PPR/game.
- **Value gap**: season PPR/g − xFP/g; ≥ max(2.5, 20% of xFP) → *production exceeds usage*
  (regression risk), ≤ −that → *usage exceeds production* (buy-low).
- **Classification**: BUY / HOLD / SELL / MONITOR (<3 games). Reasons are stored.
- **Value score** 0-10 = 0.75·opportunity + 0.25·production + 1.0·trend.

### Manager tendencies (`manager_metrics`, `manager_classifications`)
transactions/adds/drops per week, roster churn rate (adds ÷ roster spots),
positional add shares (QB/RB/WR/TE/K/D/ST), average QBs/RBs/WRs/TEs rostered,
streaming rate per position (pickups/week) + starter turnover, rookie add rate,
average hold length of added players, quick-drop rate (dropped within 7 days),
injury replacement rate (added while a same-position player was Out/Doubtful/IR),
trade count, 2-for-1 (uneven) trades, players sent/received, IR moves, FAAB spent %.

Labels are **league-relative**: HIGH if ≥ 0.75 SD above the league mean (with an
absolute floor for HIGH; zero is always LOW), LOW if ≤ 0.75 SD below. Trade
activity uses absolute counts. Each label carries its value, league mean/std,
z-score, sample size and **confidence** (LOW below the minimum sample →
`INSUFFICIENT`, MEDIUM below 2× minimum, else HIGH). One transaction never
establishes a tendency.

### Waiver engine (`waiver_recommendations`, `team_position_needs`)
- **Opportunity** = player opportunity score ± 1 for role trend − 3 if Out/IR.
  K/D/ST and unmapped players fall back to ESPN league-points percentile ("limited model").
- **Positional need** per team = 0.55·shortage of healthy bodies + 0.45·gap
  between that team's starters and the league-average starter.
- **Roster fit** = 10·(0.35·need + 0.35·upgrade over your weakest starter +
  0.30·upgrade over the suggested drop × depth utility), where depth utility is
  1.0 for RB/WR, 0.3 for QB/TE, 0.15 for K/D/ST.
- **Suggested drop** = your lowest-value bench player whose loss keeps every
  position startable (never IR, never a starter).
- **League interest** per manager = 0.4·their need + 0.25·their historical add rate
  at the position + 0.2·waiver aggression + 0.15·FAAB/priority remaining → bucketed
  LOW / MEDIUM / HIGH with the top 3 likely competitors and why. Not a probability.
- **Recommendation**: score = 0.55·opportunity + 0.45·fit (+ small scarcity bonus).
  CLAIM needs score ≥ 6.0, fit ≥ 3.0 and must beat the suggested drop by ≥ 0.5
  value; WAIT ≥ 4.5; else IGNORE. Injured players are never CLAIM.

---

## Tests

```bash
cd backend
uv run pytest
```

Covers: schema init, idempotent ESPN sync (re-run → identical rows; changed
status replaces, dropped player disappears from the current snapshot), SWID
team detection, missing-vs-zero handling, ID mapping (crosswalk, name fallback,
ambiguity, D/ST, conflicts), trend windows/labels, value-gap + classification
rules, manager metrics + sample-size gating, waiver needs/drop/fit/claim logic,
and an end-to-end API smoke test.

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
