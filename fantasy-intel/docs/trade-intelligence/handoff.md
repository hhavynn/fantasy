# Handoff: wiring trade formats into the studio

The studio (`feat/youtube-studio` @ 5607d3d) and `fantasy.duckdb` live only on the Mac. The cloud session
that built `backend/app/analytics/trade_market.py` couldn't see either, so **no studio code was
written**. Rebuilding the studio here would have duplicated it, which the brief rules out. This file is
the plan for a session on the Mac.

## Branching

- This work lives on `claude/trade-advice-shorts-p05qz0`, branched from `main`. It touches only
  `backend/app/analytics/trade_market.py`, `backend/tests/test_trade_market.py`,
  `scripts/trade_market_report.py` and `docs/trade-intelligence/`. None of these exist on `feat/youtube-studio`.
- On the Mac:
  - `git switch -c feat/trade-intelligence feat/youtube-studio`
  - `git fetch origin claude/trade-advice-shorts-p05qz0`
  - `git cherry-pick <the commits>` (or merge that branch)
  - Then add the studio pieces below on `feat/trade-intelligence`.

## Facts layer: new claim kinds in `studio/stages/facts.py`

Import like `facts.py` already imports `app.analytics.trends`. Each claim is re-derived from the
snapshot by QC.

| Claim kind | Inputs | Re-derivation | Display rule |
|---|---|---|---|
| `usage_metric` | gsis_id, metric, weeks | `trade_market.derive_metric(rows, metric, weeks)` over the snapshot's `player_week_usage` rows (plus `team_rz_plays`) | Shares: whole percent. WOPR: 2 decimals. Counts: integers. |
| `market_rank` / `usage_rank` | gsis_id, position, through_week | `build_market(load_market_inputs(con, season))[gsis].market_rank` / `.usage_rank` (deterministic) | Speak as "WR8 by role" / "WR21 by box score". Never speak `usage_value` / `market_value` (model internals). |
| `trade_offer` | target gsis_id, mode, slot (opening / alt1 / alt2) | `generate_offer(market, target, mode)`: pieces **and** fairness label must match exactly | Label text only: "opening offer", "fair range", "likely high price". No ratios, no probabilities. |
| `offer_incentive` | same plus line index | The same `generate_offer` result; each number in the line (e.g. "71 vs 58") is also a `usage_metric` claim (`yards_pg`, `touchdowns`) | As written |

`Evidence` items already carry `(gsis_id, metric, weeks, value, text)`, so a candidate's evidence maps
1:1 onto `usage_metric` claims in the ClaimBook.

## Editorial desk (`studio/editorial.py`)

- **Format A, "Who Should You Trade FOR?":** `candidates(market, "buy")` gives targets; the score is the
  signal-strength input to the 8-criterion board.
- **Format B, "Who Can You Trade This Player FOR?":** `candidates(market, "sell")`.
- **Skip rule:** skip a target when `generate_offer` returns no opening offer, or when every offer
  fails the position check. A video without a clean 1-for-1 has no payoff.
- **Repetition:** QC's repetition gate should key on target gsis_id across both formats, since the same
  player can't be a buy one week and a sell the next without a stated reason.

## Scenes (`studio/format_scenes.py`)

- `trade_card`: two headshots with an arrow, each with a position badge and one role stat.
- `offer_package`: a 2-for-1 as stacked cards, with the fairness label as a pill. No numbers on the pill.
- `role_vs_box`: one bar for the role rank, one for the box-score rank; the gap is the story.
- `risk_strip`: a single risk line for the ending, taken from `offer["risks"]`.

## Storyboards from 2026 weeks 1-5 (cloud rebuild; re-verify on the Mac DB)

Every number is a `usage_metric` / rank / `trade_offer` claim from `trade_market`. Target length is about
13-15 s, with the hook inside 2.5 s.

**A. Who should you trade FOR? Michael Wilson (ARI, WR)**

| t | Narration | Scene / on screen | Claims |
|---|---|---|---|
| 0-2.5 s | "Trade for Michael Wilson before his box score catches up." | `role_vs_box`: WR8 by role, WR21 by box score | usage_rank, market_rank |
| 2.5-6 s | "29% target share and 42% of the air yards over his last three games." | stat chips | usage_metric target_share / air_yards_share, weeks 3-5 |
| 6-8 s | "One touchdown on nine red-zone looks." | chip | touchdowns, rz_opportunities |
| 8-11 s | "Open with George Pickens straight up." | `trade_card`: "opening offer" | trade_offer opening |
| 11-13 s | "They get more yards per game: 71 vs 58." | incentive caption | yards_pg x2 |
| 13-15 s | "Buy the role, not the box score." | ending card plus `risk_strip`: two tough matchups in the next four weeks | offer risks |

Alternatives for the description or a pinned comment: Travis Kelce + Keon Coleman; Brian Robinson +
Stefon Diggs (both "fair range").

**B. Who can you trade this player FOR? Christian Watson (GB, WR)**

| t | Narration | Scene / on screen | Claims |
|---|---|---|---|
| 0-2.5 s | "Sell Christian Watson while six touchdowns still sell." | `role_vs_box`: WR6 by box score, WR37 by role | touchdowns, ranks |
| 2.5-6 s | "His target share fell from 26% to 18%." | trend chip, weeks 1-3 vs 4-5 | usage_metric target_share x2 |
| 6-9 s | "Ask for Tetairoa McMillan straight up." | `trade_card`: "opening offer" | trade_offer opening |
| 9-12 s | "Or take Emeka Egbuka plus Drake London." | `offer_package`: "fair range" | trade_offer alt1 |
| 12-15 s | "Touchdowns fade. Targets don't. Sell the box score." | ending card | — |

The ending must be a real takeaway, not a "like and subscribe" fade. Both endings above restate the
decision.

## QC gates (`studio/stages/qc.py`)

- **Data claims / narration numbers:** existing gates, plus the claim kinds above.
- **New gate, "no points language":** narration and on-screen text must not contain "points", "PPG",
  "projection" or "fantasy points" in trade formats.
- **New gate, "market source":** if `market_source == "box_score"`, the script must say "by box score",
  never "ADP" or "market".
- **Private data:** the trade formats' snapshot must not include ESPN league tables. `load_market_inputs`
  reads none.

## Open decisions for you

1. Run `sleeper_market_ranks` from the Mac (the free Sleeper API is reachable there) and pass the result
   as `adp_ranks`. It is Sleeper's `search_rank` (their default ordering), not true ADP. Use it, or keep
   the box-score proxy?
2. On-screen host: deferred until after the two renders, as you chose.
3. Recommendation ledger (later): store `generate_offer` output plus the snapshot hash per published
   video, and re-score after N weeks.
