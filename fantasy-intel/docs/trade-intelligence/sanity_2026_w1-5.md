# Sanity check: trade market on 2026 weeks 1-5

**Data.** nflverse 2026 REG weeks 1-5, rebuilt in the cloud session with the repo's own
`scripts/sync_nfl.py` into a scratch DuckDB. No ESPN data is in that DB. The run happened on
2026-10-15. nflverse files can be revised after release, so rerun on the Mac against `STUDIO_DB`
before using any name in a video:

    cd backend && STUDIO_DB=/Users/havyn/Documents/CS/Fantasy/fantasy-intel/data/fantasy.duckdb \
      uv run python ../scripts/trade_market_report.py --limit 10 --offers 3

**Not run.** `dynastyprocess` ID file: 403 from the cloud proxy. It isn't used here, since IDs come
from `nfl_players`. Sleeper API: blocked, so `market_value` below is the box-score proxy, not a public price.

## Does the usage index pass the smell test?

Top 8 by usage value (usage / market):

- **RB:** Gibbs 100/100, Aaron Jones 97/80, Kenneth Walker 95/97, Achane 92/54, Kyren Williams 90/92,
  Javonte Williams 87/87, Bijan Robinson 85/95, Henry 82/90
- **WR:** Ja'Marr Chase 100/95, Nacua 97/97, Jefferson 95/70, Smith-Njigba 92/100, Evans 90/82,
  St. Brown 87/68, DeVonta Smith 85/57, Michael Wilson 82/52
- **TE:** Hockenson 100/69, Bowers 93/100, Likely 87/93, McBride 81/75, Goedert 75/63, Kittle 69/87,
  Strange 63/33, LaPorta 58/81

These are role leaders measured only by snap, target, air-yard, backfield and red-zone share.
Hockenson is TE1 on role, but he played 3% of snaps in week 5 and has no week-6 designation yet. He is
flagged `LEFT_LAST_GAME_EARLY` and kept out of the buy list until a report clears him.

## Defects found during the sanity pass (all fixed, each with a test)

1. **Partial games read as lost roles.** Chase's week 4 (20% of snaps) made his role look DOWN and
   ranked him WR22. Fix: `full_games()` drops games under half the player's median snap share from the
   role and trend windows. Chase is now WR1.
2. **Percentile compression.** Over the whole position pool every starter sat at 75-100, so small
   trend or schedule terms reordered starters. Fix: role metrics are indexed to the mean of the
   position's startable top N. Trend, context and schedule are small multipliers. Percentiles stay as
   narration evidence.
3. **Box-score proxy underrates stars.** It produced Collins for Chase, and Jefferson for Matthew Golden.
   Fix: without a public price, elite roles are never buy-lows and never sell-mode returns.
4. **Injury timing.** The week-6 report fills in during the week. Availability now follows contract
   section 8 (current week only); a week-5 Out is a risk line.
5. **Trends without numbers.** A sell-high script needs "target share 26% in weeks 1-3, 18% in
   weeks 4-5". Role moves are now evidence items.

## Remaining limitations

- `market_value` is a proxy until Sleeper ranks (`sleeper_market_ranks`) are plugged in from a machine
  that can reach the API. The candidate lists below will shift when that happens.
- Five weeks is a small sample; three-game players carry a "Small sample" risk line.
- QBs are out of scope.

## Report output

Values are 0-100 on a cross-position scale (100 = top role at the position). `usage` is our role-based view; `market` is the box-score proxy for what managers pay. Not fantasy points.

## Buy-low candidates

| # | Player | Pos | Team | Usage (rank) | Market (rank) | Trend | Flags |
|---|---|---|---|---|---|---|---|
| 1 | AJ Barner | TE | SEA | 38 (TE12) | 11 (TE19) | UP | ROLE_UP, TD_LIGHT |
| 2 | Brenton Strange | TE | JAX | 63 (TE7) | 33 (TE13) | UP | ROLE_UP |
| 3 | Tucker Kraft | TE | GB | 52 (TE9) | 29 (TE14) | UP | ROLE_UP, TD_LIGHT |
| 4 | Michael Wilson | WR | ARI | 82 (WR8) | 52 (WR21) | FLAT | TD_LIGHT |
| 5 | Courtland Sutton | WR | DEN | 37 (WR29) | 9 (WR48) | FLAT | TD_LIGHT |
| 6 | Emeka Egbuka | WR | TB | 63 (WR16) | 42 (WR26) | UP | ROLE_UP |
| 7 | Braelon Allen | RB | NYJ | 72 (RB12) | 52 (RB21) | UP | ROLE_UP |
| 8 | Jordan Addison | WR | MIN | 50 (WR22) | 35 (WR30) | UP | MISSED_LAST_GAME, ROLE_UP |
| 9 | Malik Washington | WR | MIA | 42 (WR26) | 20 (WR39) | FLAT | - |
| 10 | Carnell Tate | WR | TEN | 46 (WR24) | 26 (WR35) | FLAT | - |

### Buy-low: AJ Barner

Evidence: snap share 92% over the last 3 games; target share 18% over the last 3 games; WOPR 0.31 over the last 3 games; red-zone opportunity share 20% over the last 3 games; 1 touchdown in 5 games; 123 scrimmage yards in 5 games; 25 scrimmage yards per game

- **Opening 1-for-1** (1-for-1, fair range): you give Kendre Miller (RB, NO); you get AJ Barner (TE, SEA)
  - ranks: Kendre Miller: usage RB43 / market RB46; AJ Barner: usage TE12 / market TE19
  - position: needs TE depth: they give up a TE without getting one back; roster: even roster spots
  - their incentive: They get more touchdowns so far (2 vs 1). AJ Barner hasn't turned his red-zone work into touchdowns, so his box score looks worse than his role.
  - risks: none flagged
- note: Target is priced too low for a 2-for-1 to make sense.

### Buy-low: Brenton Strange

Evidence: snap share 82% over the last 3 games; target share 25% over the last 3 games; WOPR 0.55 over the last 3 games; red-zone opportunity share 3% over the last 3 games; 1 touchdown in 5 games; 235 scrimmage yards in 5 games; 47 scrimmage yards per game

- **Opening 1-for-1** (1-for-1, opening offer): you give Terry McLaurin (WR, WAS); you get Brenton Strange (TE, JAX)
  - ranks: Terry McLaurin: usage WR35 / market WR32; Brenton Strange: usage TE7 / market TE13
  - position: needs TE depth: they give up a TE without getting one back; roster: even roster spots
  - their incentive: They get more scrimmage yards per game (51 vs 47).
  - risks: Brenton Strange faces 2 tough matchups in the next 4 weeks.
- **Alternative** (2-for-1, fair range): you give Keon Coleman (WR, BUF) + Mike Gesicki (TE, CIN); you get Brenton Strange (TE, JAX)
  - ranks: Keon Coleman: usage WR52 / market WR36; Mike Gesicki: usage TE36 / market TE17; Brenton Strange: usage TE7 / market TE13
  - position: ok: they get a TE back for the TE they give up; roster: you open a roster spot (2 out, 1 in)
  - their incentive: They get more touchdowns so far (6 vs 1). They get more scrimmage yards per game (77 vs 47). They turn one roster spot into two playable pieces.
  - risks: Brenton Strange faces 2 tough matchups in the next 4 weeks.
- **Alternative** (2-for-1, fair range): you give Deebo Samuel Sr. (WR, SF) + Tyler Higbee (TE, LA); you get Brenton Strange (TE, JAX)
  - ranks: Deebo Samuel Sr.: usage WR84 / market WR40; Tyler Higbee: usage TE19 / market TE15; Brenton Strange: usage TE7 / market TE13
  - position: ok: they get a TE back for the TE they give up; roster: you open a roster spot (2 out, 1 in)
  - their incentive: They get more touchdowns so far (4 vs 1). They get more scrimmage yards per game (102 vs 47). They turn one roster spot into two playable pieces.
  - risks: Brenton Strange faces 2 tough matchups in the next 4 weeks.

## Sell-high candidates

| # | Player | Pos | Team | Usage (rank) | Market (rank) | Trend | Flags |
|---|---|---|---|---|---|---|---|
| 1 | Christian Watson | WR | GB | 23 (WR37) | 87 (WR6) | DOWN | ROLE_DOWN |
| 2 | Nico Collins | WR | HOU | 48 (WR23) | 90 (WR5) | INSUFFICIENT | - |
| 3 | Travis Kelce | TE | KC | 8 (TE20) | 42 (TE11) | DOWN | ROLE_DOWN |
| 4 | Brian Robinson | RB | ATL | 5 (RB52) | 35 (RB30) | FLAT | TD_HEAVY |
| 5 | Jameson Williams | WR | DET | 11 (WR46) | 40 (WR27) | FLAT | - |
| 6 | Darius Cooper | WR | PHI | 16 (WR42) | 44 (WR25) | INSUFFICIENT | - |
| 7 | Parker Washington | WR | JAX | 38 (WR28) | 59 (WR18) | DOWN | ROLE_DOWN |
| 8 | James Cook | RB | BUF | 68 (RB14) | 84 (RB7) | DOWN | ROLE_DOWN |
| 9 | Sam LaPorta | TE | DET | 58 (TE8) | 81 (TE4) | FLAT | - |
| 10 | Stefon Diggs | WR | WAS | 20 (WR39) | 38 (WR28) | FLAT | LISTED_OUT_LAST_WEEK, MISSED_LAST_GAME |

### Sell-high: Christian Watson

Evidence: snap share 76% over the last 3 games; target share 18% over the last 3 games; air-yard share 30% over the last 3 games; WOPR 0.49 over the last 3 games; red-zone opportunity share 12% over the last 3 games; 6 touchdowns in 5 games; 474 scrimmage yards in 5 games

- **Opening 1-for-1** (1-for-1, opening offer): you give Christian Watson (WR, GB); you get Tetairoa McMillan (WR, CAR)
  - ranks: Christian Watson: usage WR37 / market WR6; Tetairoa McMillan: usage WR10 / market WR4
  - position: ok: they get a WR back for the WR they give up; roster: even roster spots
  - their incentive: They get more touchdowns so far (6 vs 2).
  - risks: Tetairoa McMillan faces 2 tough matchups in the next 4 weeks.
- **Alternative** (2-for-1, fair range): you give Christian Watson (WR, GB); you get Emeka Egbuka (WR, TB) + Drake London (WR, ATL)
  - ranks: Christian Watson: usage WR37 / market WR6; Emeka Egbuka: usage WR16 / market WR26; Drake London: usage WR13 / market WR17
  - position: ok: they get a WR back for the WR they give up; roster: you need an open roster spot (1 out, 2 in)
  - their incentive: They get more touchdowns so far (6 vs 2). They consolidate two pieces into one better player and free a roster spot.
  - risks: none flagged
- **Alternative** (2-for-1, opening offer): you give Christian Watson (WR, GB); you get Malik Nabers (WR, NYG) + Cam Skattebo (RB, NYG)
  - ranks: Christian Watson: usage WR37 / market WR6; Malik Nabers: usage WR9 / market WR11; Cam Skattebo: usage RB26 / market RB28
  - position: ok: they get a WR back for the WR they give up; roster: you need an open roster spot (1 out, 2 in)
  - their incentive: They get more touchdowns so far (6 vs 3). Cam Skattebo hasn't turned his red-zone work into touchdowns, so his box score looks worse than his role. They consolidate two pieces into one better player and free a roster spot.
  - risks: none flagged

### Sell-high: Nico Collins

Evidence: snap share 71% over the last 3 games; target share 26% over the last 3 games; air-yard share 33% over the last 3 games; WOPR 0.62 over the last 3 games; red-zone opportunity share 13% over the last 3 games; 3 touchdowns in 3 games; 256 scrimmage yards in 3 games

- **Opening 1-for-1** (1-for-1, opening offer): you give Nico Collins (WR, HOU); you get Tetairoa McMillan (WR, CAR)
  - ranks: Nico Collins: usage WR23 / market WR5; Tetairoa McMillan: usage WR10 / market WR4
  - position: ok: they get a WR back for the WR they give up; roster: even roster spots
  - their incentive: They get more touchdowns so far (3 vs 2).
  - risks: Small sample: Nico Collins (you give) has 3 games. Tetairoa McMillan faces 2 tough matchups in the next 4 weeks.
- **Alternative** (2-for-1, fair range): you give Nico Collins (WR, HOU); you get Michael Wilson (WR, ARI) + Drake London (WR, ATL)
  - ranks: Nico Collins: usage WR23 / market WR5; Michael Wilson: usage WR8 / market WR21; Drake London: usage WR13 / market WR17
  - position: ok: they get a WR back for the WR they give up; roster: you need an open roster spot (1 out, 2 in)
  - their incentive: They get more touchdowns so far (3 vs 1). Michael Wilson hasn't turned his red-zone work into touchdowns, so his box score looks worse than his role. They consolidate two pieces into one better player and free a roster spot.
  - risks: Small sample: Nico Collins (you give) has 3 games. Michael Wilson faces 2 tough matchups in the next 4 weeks.
- **Alternative** (2-for-1, opening offer): you give Nico Collins (WR, HOU); you get Emeka Egbuka (WR, TB) + Malik Nabers (WR, NYG)
  - ranks: Nico Collins: usage WR23 / market WR5; Emeka Egbuka: usage WR16 / market WR26; Malik Nabers: usage WR9 / market WR11
  - position: ok: they get a WR back for the WR they give up; roster: you need an open roster spot (1 out, 2 in)
  - their incentive: They consolidate two pieces into one better player and free a roster spot.
  - risks: Small sample: Nico Collins (you give) has 3 games.

