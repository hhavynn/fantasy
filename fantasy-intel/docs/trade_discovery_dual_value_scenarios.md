# Trade Discovery 2.0: representative scenarios

**Synthetic.** Every league, team and player below is invented; no real league data is used and nothing here says anything about a real roster. Every number is produced by the real functions (`trades.evaluate_trade` -> `trade_dual_value.for_evaluation`); none is typed by hand.

Regenerate with:

```
cd fantasy-intel/backend && uv run python scripts/trade_scenarios.py > ../docs/trade_discovery_dual_value_scenarios.md
```

See `trade_discovery_dual_value.md` for the contract. Nothing here estimates whether a manager would accept a trade.

## 1. A sensible mutual positional-need swap

Team 1 has a surplus at WR and is thin at RB; Team 3 is the reverse. A WR for an RB helps both starting lineups and the asset packages are comparable.

- **You send:** WR 106 (WR, #106)
- **You receive:** RB 302 (RB, #302) (from Team 3)
- **Team 3 sends:** RB 302 (RB, #302); **receives:** WR 106 (WR, #106)
- **Phase assessed:** ros; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 36.0; they send 40.0; ratio 0.9; label `balanced` (Asset packages appear relatively balanced); imbalance none
  - WR 106 (WR) You -> Team 3: PAR 36.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - RB 302 (RB) Team 3 -> You: PAR 40.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - depth, You: even (net +0 players)
  - depth, Team 3: even (net +0 players)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 384.0 -> 412.0 (delta +28.0); regular season +21.0; playoffs +7.0; weeks improved/worsened/unchanged 4/0/0
- Team 3 (Team 3): ROS 364.0 -> 392.0 (delta +28.0); regular season +21.0; playoffs +7.0; weeks improved/worsened/unchanged 4/0/0
- Key change, You: RB 302 (RB) starts 4 of 4 weeks in your lineup, replacing RB 103 (RB) in 4.
- Key change, Team 3: WR 106 (WR) starts 4 of 4 weeks in Team 3's lineup, replacing WR 307 (WR) in 4.

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | pass | Both starting lineups improve (rest of season: You +28.0, Team 3 +28.0). |
| `asset_balance` | pass | Asset packages appear relatively balanced. |
| `positional_fit` | pass | Each side receives a player who starts at least half the remaining weeks. |
| `depth_cost` | pass | No forced drop of a player with asset value and no empty starting slot. |
| `free_agent_alternative` | pass | Even after the best free-agent move, the trade still adds +28.0 for you. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `sensible`. Sensible for both managers: both starting lineups improve, the asset packages are comparable and nothing major is given up.

**Main concern:** none

Rank axes (never summed): lineup gain of the smaller side +28.0, asset ratio 0.9, certainty 0.

## 2. Both lineups improve, but the deal is lopsided

Both starting lineups gain, yet Team 2 surrenders two strong assets (a starting RB and a TE who barely starts for it) for one WR. The numbers are positive for both; the asset check says it is unlikely to be mutually attractive.

- **You send:** Diggs (WR, #105)
- **You receive:** Jeanty (RB, #204), Bowers (TE, #209) (from Team 2)
- **Team 2 sends:** Jeanty (RB, #204), Bowers (TE, #209); **receives:** Diggs (WR, #105)
- **Phase assessed:** ros; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 48.0; they send 80.0; ratio 0.6; label `they_surrender_multiple_strong` (They surrender multiple strong assets for fewer); imbalance significant, favours You
  - Diggs (WR) You -> Team 2: PAR 48.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - Jeanty (RB) Team 2 -> You: PAR 44.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - Bowers (TE) Team 2 -> You: PAR 36.0, starter-caliber True, starts 0 of 4 weeks before / 4 after, bench asset False
  - depth, You: adds depth (net +1 players)
  - depth, Team 2: consolidates (net -1 players)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 384.0 -> 432.0 (delta +48.0); regular season +36.0; playoffs +12.0; weeks improved/worsened/unchanged 4/0/0
- Team 2 (Team 2): ROS 472.0 -> 480.0 (delta +8.0); regular season +6.0; playoffs +2.0; weeks improved/worsened/unchanged 4/0/0
- Key change, You: Jeanty (RB) starts 4 of 4 weeks in your lineup, replacing RB 103 (RB) in 4.
- Key change, Team 2: Diggs (WR) starts 4 of 4 weeks in Team 2's lineup in place of Jeanty (RB).

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | pass | Both starting lineups improve (rest of season: You +48.0, Team 2 +8.0). |
| `asset_balance` | fail | They surrender multiple strong assets for fewer (48 vs 80 points above replacement). |
| `positional_fit` | pass | Each side receives a player who starts at least half the remaining weeks. |
| `depth_cost` | pass | No forced drop of a player with asset value and no empty starting slot. |
| `free_agent_alternative` | pass | Even after the best free-agent move, the trade still adds +48.0 for you. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `unlikely_mutual`. Both starting lineups improve, but the asset packages are lopsided: unlikely to be mutually attractive.

**Main concern:** They surrender multiple strong assets for fewer (48 vs 80 points above replacement).

Rank axes (never summed): lineup gain of the smaller side +8.0, asset ratio 0.6, certainty 0.

## 3. Asset-fair, but one lineup does not improve

The packages are close in points above replacement, and Team 1 starts the TE it receives. Team 2 is stacked at WR and RB, so the WR it receives starts nowhere and its lineup does not move.

- **You send:** WR 108 (WR, #108)
- **You receive:** Bowers (TE, #209) (from Team 2)
- **Team 2 sends:** Bowers (TE, #209); **receives:** WR 108 (WR, #108)
- **Phase assessed:** ros; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 28.0; they send 36.0; ratio 0.778; label `balanced` (Asset packages appear relatively balanced); imbalance none
  - WR 108 (WR) You -> Team 2: PAR 28.0, starter-caliber True, starts 0 of 4 weeks before / 0 after, bench asset True
  - Bowers (TE) Team 2 -> You: PAR 36.0, starter-caliber True, starts 0 of 4 weeks before / 4 after, bench asset False
  - depth, You: even (net +0 players)
  - depth, Team 2: even (net +0 players)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 384.0 -> 412.0 (delta +28.0); regular season +21.0; playoffs +7.0; weeks improved/worsened/unchanged 4/0/0
- Team 2 (Team 2): ROS 552.0 -> 552.0 (delta +0.0); regular season +0.0; playoffs +0.0; weeks improved/worsened/unchanged 0/0/4
- Key change, You: Bowers (TE) starts 4 of 4 weeks in your lineup, replacing TE 110 (TE) in 4.
- Key change, Team 2: No incoming player starts in Team 2's lineup; the change is depth only.

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | fail | Team 2's starting lineup does not improve (rest of season: +28.0 / +0.0). |
| `asset_balance` | pass | Asset packages appear relatively balanced. |
| `positional_fit` | fail | Team 2: the best incoming player starts 0 of 4 weeks. |
| `depth_cost` | pass | No forced drop of a player with asset value and no empty starting slot. |
| `free_agent_alternative` | pass | Even after the best free-agent move, the trade still adds +28.0 for you. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `fair_no_lineup_gain`. Asset-fair exchange, but Team 2's starting lineup does not improve.

**Main concern:** Team 2's starting lineup does not improve (rest of season: +28.0 / +0.0).

Rank axes (never summed): lineup gain of the smaller side +0.0, asset ratio 0.778, certainty 0.

## 4. A 2-for-1 consolidation that forces a drop

Team 1 turns RB depth and a weak WR2 into one star WR (Ember); Team 2, deep at WR and thin at RB, takes two starters for one. Both lineups gain and the packages are balanced, but Team 2's roster is full: it must drop a bench RB who still has asset value, so the depth check raises a concern.

- **You send:** RB 103 (RB, #103), WR 106 (WR, #106)
- **You receive:** Ember (WR, #204) (from Team 2)
- **Team 2 sends:** Ember (WR, #204); **receives:** RB 103 (RB, #103), WR 106 (WR, #106)
- **Phase assessed:** ros; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 52.0; they send 52.0; ratio 1.0; label `balanced` (Asset packages appear relatively balanced); imbalance none
  - RB 103 (RB) You -> Team 2: PAR 36.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - WR 106 (WR) You -> Team 2: PAR 16.0, starter-caliber True, starts 4 of 4 weeks before / 0 after, bench asset True
  - Ember (WR) Team 2 -> You: PAR 52.0, starter-caliber True, starts 4 of 4 weeks before / 4 after, bench asset False
  - depth, You: consolidates (net -1 players)
  - depth, Team 2: adds depth (net +1 players); forced drop RB 203 (PAR 4.0, starter-caliber False, lineup cost +0.0)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 440.0 -> 456.0 (delta +16.0); regular season +12.0; playoffs +4.0; weeks improved/worsened/unchanged 4/0/0
- Team 2 (Team 2): ROS 408.0 -> 420.0 (delta +12.0); regular season +9.0; playoffs +3.0; weeks improved/worsened/unchanged 4/0/0
- Key change, You: Ember (WR) starts 4 of 4 weeks in your lineup in place of RB 103 (RB).
- Key change, Team 2: RB 103 (RB) starts 4 of 4 weeks in Team 2's lineup in place of Ember (WR).

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | pass | Both starting lineups improve (rest of season: You +16.0, Team 2 +12.0). |
| `asset_balance` | pass | Asset packages appear relatively balanced. |
| `positional_fit` | pass | Each side receives a player who starts at least half the remaining weeks. |
| `depth_cost` | concern | Team 2 must drop RB 203 (RB) (4 above replacement). |
| `free_agent_alternative` | pass | Even after the best free-agent move, the trade still adds +16.0 for you. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `sensible_with_concerns`. Both starting lineups improve, with concerns to weigh.

**Main concern:** Team 2 must drop RB 203 (RB) (4 above replacement).

Rank axes (never summed): lineup gain of the smaller side +12.0, asset ratio 1.0, certainty 0.

## 5. A playoff-focused trade

Team 1's RB2 is on a bye in the playoff week. Closer sits behind him all regular season (delta 0) and starts in the playoffs. Assessed with the playoff phase, as the playoff lens does.

- **You send:** Wideout (WR, #107)
- **You receive:** Closer (RB, #204) (from Team 2)
- **Team 2 sends:** Closer (RB, #204); **receives:** Wideout (WR, #107)
- **Phase assessed:** playoff; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 20.0; they send 25.0; ratio 0.8; label `balanced` (Asset packages appear relatively balanced); imbalance none
  - Wideout (WR) You -> Team 2: PAR 20.0, starter-caliber True, starts 0 of 4 weeks before / 4 after, bench asset False
  - Closer (RB) Team 2 -> You: PAR 25.0, starter-caliber True, starts 1 of 4 weeks before / 1 after, bench asset True
  - depth, You: even (net +0 players)
  - depth, Team 2: even (net +0 players)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 440.0 -> 452.0 (delta +12.0); regular season +0.0; playoffs +12.0; weeks improved/worsened/unchanged 1/0/3
- Team 2 (Team 2): ROS 372.0 -> 384.0 (delta +12.0); regular season +9.0; playoffs +3.0; weeks improved/worsened/unchanged 4/0/0
- Key change, You: Closer (RB) starts 1 of 4 weeks in your lineup, replacing Rusher (RB) in 1.
- Key change, Team 2: Wideout (WR) starts 4 of 4 weeks in Team 2's lineup, replacing WR 206 (WR) in 4.

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | pass | Both starting lineups improve (fantasy playoffs: You +12.0, Team 2 +3.0). |
| `asset_balance` | pass | Asset packages appear relatively balanced. |
| `positional_fit` | concern | You: the best incoming player starts 1 of 4 weeks. |
| `depth_cost` | pass | No forced drop of a player with asset value and no empty starting slot. |
| `free_agent_alternative` | not_applicable | The free-agent comparison is a rest-of-season figure; it does not judge this phase. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `sensible_with_concerns`. Both starting lineups improve, with concerns to weigh.

**Main concern:** You: the best incoming player starts 1 of 4 weeks.

Rank axes (never summed): lineup gain of the smaller side +3.0, asset ratio 0.8, certainty 0.

## 6. A deal whose gain is mostly available from free agency

Team 1 swaps a WR for Bowers, who would start at TE for it, and Team 2 starts the WR. But a free-agent TE (11) is nearly as good as Bowers (13), so most of the gain needs no trade.

- **You send:** WR 108 (WR, #108)
- **You receive:** Bowers (TE, #209) (from Team 2)
- **Team 2 sends:** Bowers (TE, #209); **receives:** WR 108 (WR, #108)
- **Phase assessed:** ros; weeks 10-13 (regular season through 12, playoffs after)

**Asset value** (ESPN points above replacement, all remaining weeks)

- You send 4.0; they send 8.0; ratio 0.5; label `they_give_more` (They give up more asset value (moderate imbalance)); imbalance moderate, favours You
  - WR 108 (WR) You -> Team 2: PAR 4.0, starter-caliber True, starts 0 of 4 weeks before / 4 after, bench asset False
  - Bowers (TE) Team 2 -> You: PAR 8.0, starter-caliber True, starts 0 of 4 weeks before / 4 after, bench asset False
  - depth, You: even (net +0 players)
  - depth, Team 2: even (net +0 players)

**Lineup benefit** (starters only; bench points never count)

- You (Team 1): ROS 384.0 -> 412.0 (delta +28.0); regular season +21.0; playoffs +7.0; weeks improved/worsened/unchanged 4/0/0
- Team 2 (Team 2): ROS 472.0 -> 488.0 (delta +16.0); regular season +12.0; playoffs +4.0; weeks improved/worsened/unchanged 4/0/0
- Key change, You: Bowers (TE) starts 4 of 4 weeks in your lineup, replacing TE 110 (TE) in 4.
- Key change, Team 2: WR 108 (WR) starts 4 of 4 weeks in Team 2's lineup, replacing WR 206 (WR) in 4.

**Checks**

| criterion | status | reason |
|---|---|---|
| `lineup_gain` | pass | Both starting lineups improve (rest of season: You +28.0, Team 2 +16.0). |
| `asset_balance` | concern | They give up more asset value (moderate imbalance) (4 vs 8 points above replacement). |
| `positional_fit` | pass | Each side receives a player who starts at least half the remaining weeks. |
| `depth_cost` | pass | No forced drop of a player with asset value and no empty starting slot. |
| `free_agent_alternative` | concern | Most of this projected gain is available without trading: adding TE 907 (TE) gives +20.0; with that move made, the trade adds only +8.0 of its +28.0. |
| `uncertainty` | pass | No injury, eligibility or projection gaps on the traded players. |

**Verdict:** `sensible_with_concerns`. Both starting lineups improve, with concerns to weigh.

**Main concern:** They give up more asset value (moderate imbalance) (4 vs 8 points above replacement).

Rank axes (never summed): lineup gain of the smaller side +16.0, asset ratio 0.5, certainty 0.

