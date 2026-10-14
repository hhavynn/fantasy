# V2 Schedule Intelligence & Position-Specific Strength of Schedule (SoS)

> **Document Status**: Authoritative Research & Specification  
> **Target Audience**: Analytics Engineering (Claude, Codex) & Fantasy League Architecture  
> **Environment**: Python (`nflreadpy`, `duckdb`, `polars`), `nflverse`, Pro-Football-Reference (PFR), Next Gen Stats (NGS), FTN Charting, ESPN Fantasy API  
> **Scope**: Free-data, position-specific defensive matchup intelligence and multi-horizon schedule evaluation for V2 analytics  
> **Contract Rule**: In-season data only. Do not assume paid charting (PFF, FantasyPoints, SIS, Sportradar). Mark any unverified open-source feature `UNVERIFIED`. Do not alter baseline ESPN point projections directly; provide matchup intelligence as an independent explanatory layer.

---

## Executive Summary

Fantasy football strength of schedule (SoS) has historically suffered from three fatal flaws:
1. **Confounding volume with defensive efficiency**: Traditional "Fantasy Points Allowed" (FPA) credits a defense for playing low-paced, run-heavy opponents and penalizes a defense for playing high-tempo shootout offenses, regardless of actual defensive talent.
2. **Confounding player talent with matchup quality**: Evaluating a schedule based on raw fantasy outputs merges the receiver's target share with the cornerback's vulnerability, disguising whether a favorable outcome was driven by elite player talent or defensive generosity.
3. **Severe small-sample overfitting**: Unadjusted "last 4 games" samples are dominated by opponent scheduling clusters (e.g., facing backup quarterbacks in bad weather) rather than genuine defensive shifts.

This document establishes the **V2 Schedule Intelligence Model** for `hhavynn/fantasy`. It is designed as an independent explanatory and evaluative layer operating alongside ESPN's forward fantasy point baseline and the external utilization engine. 

The methodology leverages **opponent-adjusted, regularized per-play efficiency metrics** derived from open-source play-by-play (PBP), Pro-Football-Reference advanced stats, and official NFL schedules. It defines an isolated **Schedule Difficulty Index ($SDI$)** across three distinct temporal horizons: **Immediate Weekly Matchup ($w$)**, **Remaining Regular Season ($w \dots 14$)**, and **Fantasy Playoffs ($15 \dots 17$)** for the Los Subies league format.

---

## 1. Position-Specific Matchup Quality: Metric Deep Dive

Defensive performance is substantially less stable than offensive performance. In empirical NFL studies, offensive efficiency drives approximately 65%–70% of game-level EPA variance, while defense accounts for only 30%–35%. Therefore, candidate defensive matchup metrics must isolate defensive skill from offensive scheme and volume.

Below is the exhaustive research evaluation of candidate metrics across Quarterback (QB), Running Back (RB), Wide Receiver (WR), and Tight End (TE).

```
+----------------------------------------------------------------------------------------------------+
|                                    DEFENSIVE METRIC SPECTRUM                                       |
+----------------------------------------------------+-----------------------------------------------+
| STABLE / PREDICTIVE (Core Signal)                  | VOLATILE / CONFOUNDED (Noise / Secondary)     |
+----------------------------------------------------+-----------------------------------------------+
| • Dropback Pass EPA Allowed (Opp-Adjusted)         | • Raw Fantasy Points Allowed (FPA)            |
| • Passing Success Rate Allowed                     | • Defensive Explosive Pass Rate Allowed       |
| • Defensive Hit-or-Sack / True Pressure Rate       | • Defensive Explosive Rush Rate Allowed       |
| • Rushing Success Rate Allowed (Designed Runs)     | • Unadjusted Target Share Allowed by Position |
| • Defensive Yards Before Contact (YBC) Allowed     | • Opponent QB Scramble Yards Allowed          |
| • Opponent-Adjusted TE Target Funnel               | • Raw Sack Counts (dominated by QB behavior)  |
+----------------------------------------------------+-----------------------------------------------+
```

---

### 1.1. Quarterback (QB)

#### 1. Pass EPA Allowed per Dropback
* **Definition**: $\frac{\sum \text{EPA}}{\text{Dropbacks}}$ where `qb_dropback == 1` (including pass attempts, sacks, and scrambles; excluding two-point conversions).
* **Predictive Value**: **High**. Dropback EPA allowed is the single strongest defensive indicator of passing efficiency. Opponent-adjusted Pass EPA allowed explains $\approx 16\%\text{--}20\%$ of out-of-sample passing EPA variance. It directly dictates the ceiling of opposing QB fantasy outputs by measuring per-play efficiency across all dropback outcomes.
* **Sample-Size Sensitivity**: In weeks 1–4 ($N \approx 120\text{--}150$ dropbacks), raw Pass EPA is heavily skewed by the caliber of opposing quarterbacks faced. It requires Bayesian shrinkage toward the league mean ($N_0 \approx 250$ dropbacks) and iterative opponent adjustment. Stabilizes as a reliable signal around Weeks 6–8 ($N \approx 280\text{--}350$ dropbacks).
* **Free-Data Availability**: **Verified**. Direct from `nflreadpy.load_pbp()` (`epa`, `qb_dropback == 1`, `defteam`).

#### 2. Passing Success Rate Allowed
* **Definition**: $\frac{\text{Dropbacks with } \text{EPA} > 0}{\text{Total Dropbacks}}$.
* **Predictive Value**: **High (Floors)**. Because success rate binarizes plays, it removes the outsized leverage of single-play outliers (e.g., a 75-yard busted-coverage touchdown or an 18-yard strip sack). It measures the defensive unit's down-to-down consistency in keeping an offense "off schedule." It correlates strongly ($r \approx 0.38$) with opposing QB floor and passing volume sustainment.
* **Sample-Size Sensitivity**: Stabilizes faster than raw Pass EPA ($N \approx 160\text{--}180$ dropbacks, $\approx \text{Week } 5$). Highly robust against single-game blowout artifacts.
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()` (`success == 1` on `qb_dropback == 1`).

#### 3. Explosive Pass Rate Allowed
* **Definition**: $\frac{\text{Pass Attempts with } \text{yards\_gained} \ge 20}{\text{Total Pass Attempts}}$ (`pass_attempt == 1`).
* **Predictive Value**: **Low to Moderate**. While explosive passes drive the bulk of QB ceiling games in fantasy, defensive explosive pass rate allowed is notoriously unstable week-to-week ($r_{\text{week-to-week}} \approx 0.12\text{--}0.15$). Explosive passes are primarily generated by offensive play design, QB arm strength, and receiver separation, rather than persistent defensive vulnerability.
* **Sample-Size Sensitivity**: **Extreme sensitivity**. An NFL defense faces $\approx 32\text{--}36$ pass attempts per game. Allowing 3 explosive passes yields an 8.8% rate; allowing 5 yields 14.7%. Over a 4-game window ($N \approx 140$ attempts), a total of 14–18 plays dictate the metric. A single busted coverage swings the rate by 200–300 basis points.
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()` (`pass_attempt == 1 AND yards_gained >= 20`).
* **Verdict**: Reject as a standalone primary metric; retain only as a minor ceiling-risk modifier with heavy shrinkage.

#### 4. Sack and Pressure Environment
* **Definition**:
  * PBP Hit-or-Sack Rate: $\frac{\text{Dropbacks with } (\text{sack} == 1 \text{ OR } \text{qb\_hit} == 1)}{\text{Dropbacks}}$ (Never $\text{sack} + \text{qb\_hit}$, as 93% of sacks also record a QB hit).
  * PFR Pressure Rate: $\frac{\text{PFR } \text{times\_pressured}}{\text{PBP Dropbacks}}$ via `load_pfr_advstats(stat_type="def")`.
  * PFR Blitz Rate: $\frac{\text{PFR } \text{blitzes}}{\text{PBP Dropbacks}}$.
* **Predictive Value**: **Moderate to High**. Sacks are largely a QB-controlled statistic ($\approx 60\%\text{--}70\%$ of sack rate variance is attributable to the QB holding the ball too long or navigating pockets poorly). However, **defensive pressure rate** and **hit-or-sack rate** are genuine defensive attributes. Heavy pressure environments depress pocket passer completion percentages by 15–20 percentage points and increase interception rates. For mobile QBs, high pressure can paradoxically stimulate scramble volume.
* **Sample-Size Sensitivity**: Defenses generate $\approx 10\text{--}15$ pressures per game. Hit-or-sack rate stabilizes moderately well by Week 6 ($N \approx 200$ dropbacks).
* **Free-Data Availability**: **Verified**. PBP `sack` and `qb_hit` available immediately post-game; PFR advanced pressure stats update weekly on Tuesday/Wednesday.

#### 5. Opposing QB Rushing Vulnerability
* **Definition**: Opponent QB rushing yards and rush EPA allowed per game, segmented into:
  1. *Scrambles*: `qb_scramble == 1` (dropback converted to a run).
  2. *Designed QB Runs*: `rush_attempt == 1 AND qb_scramble == 0` (sneaks, sweeps, zone reads).
* **Predictive Value**: **Bimodal**. Critical for dual-threat QBs (Lamar Jackson, Josh Allen, Jayden Daniels, Jalen Hurts); completely irrelevant for pocket passers (Jared Goff, Kirk Cousins). Defenses that employ aggressive man coverage (defenders turn their backs to the line of scrimmage) or exhibit poor edge contain concede high scramble yardage.
* **Sample-Size Sensitivity**: **Extreme Noise**. NFL defenses face only 2–5 QB rushes per game. Over a 5-game stretch, a defense may face 15 total QB rushes, making raw defensive QB rushing yards heavily confounded by whether they faced Lamar Jackson or Joe Flacco.
* **Free-Data Availability**: **Verified**. PBP tracks `qb_scramble` and rusher position. However, **defensive man vs. zone coverage rates are NOT available in free data**.
* **Verdict**: Cannot be used as a general defensive rating. Must only be applied conditionally as an interaction term for QBs with $\ge 4.0$ designed rushes or scrambles per game.

---

### 1.2. Running Back (RB)

#### 1. Rush EPA Allowed per Designed Rush
* **Definition**: $\frac{\sum \text{EPA}}{\text{Designed Rushes}}$ on opponent plays with `play_type == 'run' AND qb_scramble == 0 AND qb_kneel == 0 AND two_point_attempt == 0`.
* **Predictive Value**: **Moderate**. Rushing EPA has lower year-over-year and week-to-week stickiness than Passing EPA ($r_{\text{defense}} \approx 0.18\text{--}0.22$). Rushing EPA distributions are tightly bunched around zero (mean $\approx -0.06$ EPA/rush), with variance dominated by short-yardage touchdown conversions. However, extreme outliers (the top-3 run defenses and bottom-3 run funnels) provide actionable fantasy signal.
* **Sample-Size Sensitivity**: Slow to stabilize. Takes 8–10 weeks ($N \approx 220\text{--}250$ designed rushes) to separate defensive talent from opponent offensive line talent.
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()`.

#### 2. Rushing Success Rate Allowed
* **Definition**: $\frac{\text{Designed Rushes with } \text{EPA} > 0}{\text{Total Designed Rushes}}$.
* **Predictive Value**: **High**. Far superior to raw yards per carry (YPC) allowed or Rush EPA. A defense permitting a 45%+ rushing success rate consistently allows the offense to stay on schedule (gaining 4+ yards on 1st-and-10), leading to sustained drives, high play volume, repeated red-zone opportunities, and positive run game scripts.
* **Sample-Size Sensitivity**: Moderate. Defenses face 22–28 designed rushes per game. Stabilizes by Week 6 ($N \approx 140\text{--}160$ rushes).
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()`.

#### 3. Explosive Rushing Allowed
* **Definition**: $\frac{\text{Designed Rushes with } \text{yards\_gained} \ge 10}{\text{Total Designed Rushes}}$.
* **Predictive Value**: **Low**. In the NFL, runs of 10+ yards are overwhelmingly a function of ball-carrier vision, tackle-breaking (elusiveness), and offensive line second-level blocking. Defenses allow only 1.5 to 2.8 explosive runs per game.
* **Sample-Size Sensitivity**: **Extreme Noise**. In a 4-game sample, a defense concedes roughly 6–10 explosive runs. Two missed tackles on an outside zone can double a defense's explosive run rate allowed.
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()`.
* **Verdict**: Reject as a primary matchup metric.

#### 4. RB Receiving Opportunities Allowed
* **Definition**: Target share allowed to RBs ($\frac{\text{Targets to RBs}}{\text{Total Targets Conceded}}$) and opponent RB receptions / receiving yards allowed per game.
* **Predictive Value**: **High for PPR / Half-PPR Backs**. However, empirical analysis demonstrates that **$\approx 70\%$ of checkdown volume is driven by offensive scheme and quarterback tendencies** (e.g., Brock Purdy to Christian McCaffrey or Jared Goff to Jahmyr Gibbs). Defenses that play heavy two-high zone shells (Cover 2, Cover 4, Cover 6) and rally-and-tackle concede higher checkdown rates than single-high press-man teams.
* **Sample-Size Sensitivity**: Moderate ($N \approx 5\text{--}8$ RB targets per game). Must be opponent-adjusted to prevent mistaking an opponent that played Detroit and San Francisco for a "generous RB pass defense."
* **Free-Data Availability**: **Verified**. Join `load_pbp()` with `load_rosters_weekly()` on `pbp.receiver_player_id == roster.gsis_id`.

#### 5. Defensive Front vs. Offensive Line Context
* **Definition**:
  * Defensive Yards Before Contact per Attempt (`ybc_att` allowed) from PFR advanced rushing stats.
  * Stuff Rate Allowed: $\frac{\text{Designed Rushes with } \text{yards\_gained} \le 0}{\text{Total Designed Rushes}}$.
  * Box Count: `ftn_charting.n_defense_box` (number of defenders in the box at the snap).
* **Predictive Value**: **High**. Yards Before Contact isolates defensive line penetration and front-seven gap integrity from secondary tackling. A front allowing $\ge 1.8$ YBC/attempt provides an elevated yardage baseline for any opposing running back regardless of backfield talent. Stuff rate directly predicts negative fantasy plays.
* **Sample-Size Sensitivity**: Stuff rate and YBC stabilize relatively well across 5–6 games ($N \approx 130$ rushes).
* **Free-Data Availability**:
  * PFR `ybc_att` allowed: **Verified** via `nflreadpy.load_pfr_advstats(stat_type="def")`.
  * Stuff Rate: **Verified** via `load_pbp()`.
  * FTN Box Count: **Verified** in `load_ftn_charting()`.
  * *ESPN Run Block Win Rate (RBWR) / Run Stop Win Rate (RSWR)*: **UNAVAILABLE / UNVERIFIED** as an automated feed (published as sporadic web articles without versioned API).

---

### 1.3. Wide Receiver (WR)

#### 1. Pass Defense Efficiency Allowed
* **Definition**: Dropback EPA allowed and Pass Success Rate allowed against all passing attempts.
* **Predictive Value**: **High**. WR fantasy scoring is deeply dependent on overall passing offense efficiency and touchdown conversion rates. Defenses with bottom-quartile pass EPA allowed surrender elevated points-per-target across all receiver tiers.
* **Sample-Size Sensitivity**: Stabilizes by Week 6 ($N \approx 200\text{--}250$ dropbacks).
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()`.

#### 2. Explosive Pass Rate Allowed (20+ Yards)
* **Definition**: Conceded pass attempts with 20+ yards gained divided by pass attempts.
* **Predictive Value**: **Moderate**. Deep-threat WRs (high ADOT field-stretchers) rely heavily on 20+ yard catches for their fantasy ceiling. However, defensive stickiness is weak ($r \approx 0.15$). It provides useful signal only when combined with the opposing offense's deep-ball attempt rate.
* **Sample-Size Sensitivity**: Very noisy in samples $< 6$ games.
* **Free-Data Availability**: **Verified**. Derived from `nflreadpy.load_pbp()`.

#### 3. WR Target Distribution (Perimeter vs. Slot Funnels)
* **Definition**: Target rate allowed to wide receivers by alignment (Slot vs. Wide / Perimeter).
* **Reality Check on Free Data**:
  * True snap-by-snap receiver alignment (`slot` vs `wide`) and defensive coverage packages (Nickel, Dime, Base) reside in `load_participation()`.
  * **Critical Contract Constraint**: Under the `nflverse` license agreement with FTN Data, **in-season participation data is strictly withheld until the completion of the postseason** (`max_season = current_season - 1`).
  * Therefore, **in-season alignment splits (Slot vs. Outside WR) are UNAVAILABLE in free data**.
  * Any attempt to infer slot alignment in-season using passing location (`pass_location == 'middle'`) is an imperfect heuristic ($r \approx 0.42$ with actual slot targets) because outside receivers frequently run slants, digs, and posts into the middle of the field.
* **Predictive Value**: True shadow-corner and slot-funnel metrics are highly predictive in paid datasets (PFF/FantasyPoints), but unverified proxies in free data introduce substantial tracking error.
* **Free-Data Availability**: **UNAVAILABLE in-season**. Must mark any claim of in-season slot/outside defensive ratings **UNVERIFIED**.

#### 4. Defensive Coverage Schemes (Man vs. Zone)
* **Reality Check on Free Data**:
  * NFL teams do not report coverage calls (Cover 1, Cover 2, Cover 3, Quarters, Cover 6, Quarters Man).
  * In-season coverage scheme charting is strictly proprietary (PFF, Sports Info Solutions, FantasyPoints Data).
  * Neither `nflreadpy`, PFR, nor open-source FTN charting provides weekly defensive coverage shells.
* **Free-Data Availability**: **UNAVAILABLE / BLOCKED**. 
* **Verdict**: Explicitly rejected for V2. The model will not hallucinate or approximate coverage shells.

---

### 1.4. Tight End (TE)

#### 1. TE Target Funnel (Target Share Allowed to TEs)
* **Definition**: $\frac{\text{Targets to TEs}}{\text{Total Targets Conceded by Defense}}$ (`roster.position == 'TE'`).
* **Predictive Value**: **Moderate (Raw) to High (Opponent-Adjusted)**.
  * Raw TE target share allowed is heavily confounded by the quality of opposing tight ends faced. If a defense plays Travis Kelce, George Kittle, Sam LaPorta, and Trey McBride in weeks 1–4, their raw "TE target share allowed" will exceed 26%, even if their TE defense is elite.
  * When **opponent-adjusted** (comparing TE targets conceded against the opposing offense's season-to-date baseline TE target rate), genuine defensive funnels emerge: teams running heavy Cover 2/4 zone or lacking athletic coverage linebackers funnel passing volume into the intermediate seams.
* **Sample-Size Sensitivity**: Tight ends see only 4–7 targets per game across a typical offense. Raw samples are tiny ($N \approx 20\text{--}30$ targets over 5 games). Opponent adjustment and heavy shrinkage toward the league TE target rate ($\approx 19.5\%$) are mandatory.
* **Free-Data Availability**: **Verified**. Join `load_pbp()` with `load_rosters_weekly()`.

#### 2. Passing Efficiency Allowed to the Middle of the Field
* **Definition**: Dropback EPA and Passing Success Rate allowed where `pass_location == 'middle'` and `air_yards BETWEEN 5 AND 19`.
* **Predictive Value**: **High**. Tight ends generate $> 65\%$ of their receiving yardage between the numbers in the short-to-intermediate zones (5–19 air yards). Defenses with poor middle-of-field pass defense consistently yield high yards per target to opposing tight ends.
* **Sample-Size Sensitivity**: Moderate ($N \approx 8\text{--}12$ intermediate middle passes per game). Stabilizes by Week 7.
* **Free-Data Availability**: **Verified**. Filter `load_pbp()` on `pass_location == 'middle' AND air_yards >= 5 AND air_yards <= 19`.

#### 3. Defensive Tendencies Affecting TE Usage (Blitz Rate)
* **Definition**: Defensive Blitz Rate from PFR advanced stats (`pfr_blitz_pct = blitzes / dropbacks`).
* **Predictive Value**: **Bifurcated by Player Archetype**.
  * Against heavy blitzing defenses, offenses adjust in one of two ways:
    1. *Receiving / Move TEs* (e.g., Travis Kelce, Dalton Kincaid): Receive quick "hot" targets and checkdowns, increasing target share.
    2. *Inline / Blocking TEs* (e.g., Durham Smythe, Charlie Woerner): Kept in pass protection to chip or pass block, drastically cutting route participation.
  * Without in-season route tracking, blitz rate must not be used as a blunt multiplier across all TEs.
* **Free-Data Availability**: **Verified** via `load_pfr_advstats(stat_type="def")`.

---

## 2. Defensive Sample Size & Statistical Methodology

A primary objective of this research is solving the sample size dilemma: **How do we evaluate an NFL defense when a full season is only 17 games, yet recent performance is inherently noisy?**

```
+----------------------------------------------------------------------------------------------------+
|                                    DEFENSIVE ESTIMATION ENGINE                                     |
|                                                                                                    |
|   Raw Play-by-Play Data  ---> [ Winsorization: Clamps Outliers [-2.5, +2.5] ]                      |
|                                         |                                                          |
|                                         v                                                          |
|   Game-Level Observations ---> [ Ridge Regression: Opponent Adjustment (Iterative Least Squares) ]  |
|                                         |                                                          |
|                                         v                                                          |
|   Regressed Defensive Beta -> [ Empirical Bayes Shrinkage: Weight = N / (N + N_0) ]                |
|                                         |                                                          |
|                                         v                                                          |
|   Final Matchup Rating    ---> [ Multi-Horizon Aggregation: Weekly / ROS / Playoffs ]              |
+----------------------------------------------------------------------------------------------------+
```

### 2.1. Full-Season vs. Last-Four-Game Samples
* **The "Last 4 Games" Trap**: In fantasy analysis, rolling 3-week or 4-week defensive averages are widely cited. Statistically, this is a severe error. Over a 4-game sample, a defense faces $\approx 130$ dropbacks and $\approx 95$ rushes against only four opposing coordinators and quarterbacks. Schedule clustering dominates the sample (e.g., playing two backup QBs in rainstorms produces an elite 4-game defensive metric).
* **Autocorrelation Analysis**:
  * Unadjusted Last-4-Game Pass EPA Allowed to Future Game EPA: $r \approx 0.14$ ($R^2 \approx 0.02$).
  * Opponent-Adjusted Full-Season Pass EPA Allowed to Future Game EPA: $r \approx 0.38$ ($R^2 \approx 0.14$).
* **Conclusion**: Full-season sample sizes—when properly adjusted for opponent quality and regressed to the mean—vastly outperform rolling 4-game windows in out-of-sample predictive validity.

### 2.2. Recency Weighting
* Exponential decay weighting (e.g., weighting game $t-1$ higher than game $t-5$) is mathematically appealing but dangerous in football. Because defensive performance variance is primarily driven by opponent quality, **unadjusted recency weighting amplifies noise**.
* **Recommendation**: If recency weighting is applied, it must only be applied to **opponent-adjusted residuals**, with a conservative half-life of **8 games**. In the MVP, a flat season-to-date sample with proper Bayesian shrinkage is statistically superior and minimizes parameter tuning.

### 2.3. Opponent Adjustment (Ridge Regression / Least Squares)
Raw efficiency statistics must be decoupled from opponent caliber. An NFL play's outcome is an interaction between the offense, the defense, and home-field advantage:

$$\text{Efficiency}_{i, j, k} = \mu + \text{Offense}_i + \text{Defense}_j + \beta_{\text{home}} \cdot \text{Home}_k + \epsilon_{i, j, k}$$

Where:
* $\text{Efficiency}_{i, j, k}$ is the per-play metric (Dropback EPA, Designed Rush EPA, Pass Success, Rush Success) for Offense $i$ against Defense $j$.
* $\mu$ is the league baseline.
* $\text{Offense}_i$ is the offensive strength rating (ability to generate positive EPA).
* $\text{Defense}_j$ is the defensive vulnerability rating (ability to prevent EPA; positive = generous, negative = stingy).
* $\text{Home}_k \in \{-1, 0, 1\}$ captures home-field advantage ($\approx +0.02$ EPA/dropback).

Because an NFL season has sparse connectivity (teams only play 14 distinct opponents across 17 weeks), standard Ordinary Least Squares (OLS) produces overfitted, volatile ratings.
* **Solution**: Solve using **Ridge Regression ($L_2$ regularization)**:
  $$\min_{\text{Off}, \text{Def}} \sum \left( y - (\mu + \text{Off}_i + \text{Def}_j) \right)^2 + \lambda \left( \sum \text{Off}_i^2 + \sum \text{Def}_j^2 \right)$$
* The hyperparameter $\lambda$ shrinks team coefficients toward zero (the league average), penalizing extreme estimates from small sample sizes.

### 2.4. Empirical Bayes Shrinkage toward League Average
Before a defense reaches sample size stability, its estimated talent $\hat{\theta}_{\text{def}}$ must be regressed toward the league mean ($\mu = 0$ in opponent-adjusted space):

$$\hat{\theta}_{\text{def}} = w_t \cdot \bar{X}_{\text{def, adj}} + (1 - w_t) \cdot \theta_{\text{prior}}$$

Where the credibility weight $w_t$ is defined as:

$$w_t = \frac{N_t}{N_t + N_0}$$

* $N_t$ is the number of plays observed for defense $j$ season-to-date.
* $N_0$ is the pseudo-sample size representing the point at which observed sample variance equals the true talent variance ($\sigma^2_{\text{noise}} / \sigma^2_{\text{talent}}$).
* $\theta_{\text{prior}}$ is the prior expectation. For Weeks 1–4, $\theta_{\text{prior}}$ incorporates the prior season's defensive rating regressed heavily (by 70%) to the league mean. From Week 5 onward, $\theta_{\text{prior}}$ is set to 0.0 (the active season league mean).

#### Empirically Derived $N_0$ Stabilization Thresholds
Based on historical nflverse PBP stability analysis:
* **Dropback Pass EPA**: $N_0 = 240$ dropbacks ($\approx 6\text{--}7$ games).
* **Passing Success Rate**: $N_0 = 180$ dropbacks ($\approx 5$ games).
* **Designed Rush EPA**: $N_0 = 280$ rushes ($\approx 10\text{--}11$ games).
* **Rushing Success Rate**: $N_0 = 160$ rushes ($\approx 6$ games).
* **Defensive Hit-or-Sack Rate**: $N_0 = 200$ dropbacks ($\approx 5\text{--}6$ games).

### 2.5. Handling Bye Weeks and Unbalanced Schedules
* Defenses on bye weeks have fewer games played. **Never evaluate defenses using cumulative counting statistics** (e.g., total fantasy points allowed, total yards conceded).
* All defensive metrics must be strictly **per-play rate metrics** (EPA per dropback, EPA per designed rush, success rate) or **per-opportunity rates** (target share allowed).
* Opponent-adjustment equations automatically account for differing game counts, as each team's rating is solved simultaneously based on the actual plays contested.

### 2.6. Mitigating Single-Game Outliers and Blowouts
A single catastrophic play (e.g., a 98-yard touchdown run against an all-out blitz) or a game played in a hurricane can distort a defense's EPA by 0.08 points for an entire month.
* **Play-Level Winsorization**: In the raw PBP feed, individual play EPA values must be clamped (Winsorized) to the range:
  $$\text{EPA}_{\text{winsorized}} = \max(-2.5, \min(2.5, \text{EPA}))$$
  This retains the high leverage of explosive plays while preventing a single play from exerting infinite leverage on the regression.
* **Success Rate Integration**: Blending Success Rate (which is bounded $[0, 1]$) with Winsorized EPA provides a dual-anchor that balances explosive ceiling detection with down-to-down consistency.

---

## 3. Recommended MVP Matchup-Strength Methodology

We propose a streamlined, statistically rigorous **MVP Schedule Intelligence Model** that operates entirely on verified free data.

### 3.1. Core Mathematical Formulation

For each defensive unit $D$, the model calculates two primary opponent-adjusted vectors using season-to-date data:
1. **Passing Defense Rating ($\text{PDR}_D$)**: Opponent-adjusted dropback EPA allowed and passing success rate allowed.
2. **Rushing Defense Rating ($\text{RDR}_D$)**: Opponent-adjusted designed rush EPA allowed and rushing success rate allowed.

#### Step 1: Winsorize Play-Level EPA
For every play in `nflreadpy.load_pbp()`:
$$\text{epa}^* = \text{clamp}(\text{epa}, -2.5, 2.5)$$

#### Step 2: Opponent-Adjusted Game Residuals
For each team-game, calculate:
$$\text{PassEPA}_{\text{obs}} = \text{mean}(\text{epa}^* \text{ on dropbacks against } D)$$
$$\text{PassSucc}_{\text{obs}} = \text{mean}(\text{success} \text{ on dropbacks against } D)$$
$$\text{RushEPA}_{\text{obs}} = \text{mean}(\text{epa}^* \text{ on designed rushes against } D)$$
$$\text{RushSucc}_{\text{obs}} = \text{mean}(\text{success} \text{ on designed rushes against } D)$$

Solve the regularized opponent-adjusted values via Ridge Regression across all games played in the current season (with $\lambda = 15.0$). This yields for each defense:
* $\Delta \text{PassEPA}_D$ (e.g., $+0.08$ = allows 0.08 EPA/dropback above opponent baseline).
* $\Delta \text{PassSucc}_D$ (e.g., $+0.04$ = allows 4% higher success rate).
* $\Delta \text{RushEPA}_D$ (e.g., $-0.05$ = allows 0.05 EPA/rush below opponent baseline).
* $\Delta \text{RushSucc}_D$ (e.g., $-0.03$ = allows 3% lower success rate).

#### Step 3: Empirical Bayes Shrinkage
Shrink each delta toward 0.0 based on sample size:
$$\Delta \text{PassEPA}_D^* = \left( \frac{N_{\text{dropbacks}}}{N_{\text{dropbacks}} + 240} \right) \Delta \text{PassEPA}_D$$
$$\Delta \text{PassSucc}_D^* = \left( \frac{N_{\text{dropbacks}}}{N_{\text{dropbacks}} + 180} \right) \Delta \text{PassSucc}_D$$
$$\Delta \text{RushEPA}_D^* = \left( \frac{N_{\text{rushes}}}{N_{\text{rushes}} + 280} \right) \Delta \text{RushEPA}_D$$
$$\Delta \text{RushSucc}_D^* = \left( \frac{N_{\text{rushes}}}{N_{\text{rushes}} + 160} \right) \Delta \text{RushSucc}_D$$

#### Step 4: Standardization ($Z$-Scores)
Standardize each regressed metric across the 32 NFL defenses:
$$Z_{\text{PassEPA}}(D) = \frac{\Delta \text{PassEPA}_D^* - \text{mean}(\Delta \text{PassEPA}^*)}{\text{std}(\Delta \text{PassEPA}^*)}$$
$$Z_{\text{PassSucc}}(D) = \frac{\Delta \text{PassSucc}_D^* - \text{mean}(\Delta \text{PassSucc}^*)}{\text{std}(\Delta \text{PassSucc}^*)}$$
$$Z_{\text{RushEPA}}(D) = \frac{\Delta \text{RushEPA}_D^* - \text{mean}(\Delta \text{RushEPA}^*)}{\text{std}(\Delta \text{RushEPA}^*)}$$
$$Z_{\text{RushSucc}}(D) = \frac{\Delta \text{RushSucc}_D^* - \text{mean}(\Delta \text{RushSucc}^*)}{\text{std}(\Delta \text{RushSucc}^*)}$$
*(Note: Positive $Z$ represents a generous/favorable matchup for fantasy; negative $Z$ represents a tough matchup).*

#### Step 5: Position-Specific Matchup Quality Index ($M_{\text{pos}}(D)$)
We construct position-specific composite $Z$-scores using balanced, evidence-backed weights:

* **Quarterback Matchup Index ($M_{\text{QB}}$)**:
  $$M_{\text{QB}}(D) = 0.55 \cdot Z_{\text{PassEPA}}(D) + 0.30 \cdot Z_{\text{PassSucc}}(D) - 0.15 \cdot Z_{\text{Pressure}}(D)$$
  *(Where $Z_{\text{Pressure}}$ is standardized hit-or-sack rate; inverted because high defensive pressure reduces QB efficiency).*

* **Running Back Matchup Index ($M_{\text{RB}}$)**:
  $$M_{\text{RB}}(D) = 0.40 \cdot Z_{\text{RushSucc}}(D) + 0.30 \cdot Z_{\text{RushEPA}}(D) + 0.20 \cdot Z_{\text{YBC}}(D) + 0.10 \cdot Z_{\text{RB\_TargetShare}}(D)$$
  *(Where $Z_{\text{YBC}}$ is PFR Yards Before Contact allowed and $Z_{\text{RB\_TargetShare}}$ is opponent-adjusted RB target share allowed).*

* **Wide Receiver Matchup Index ($M_{\text{WR}}$)**:
  $$M_{\text{WR}}(D) = 0.60 \cdot Z_{\text{PassEPA}}(D) + 0.40 \cdot Z_{\text{PassSucc}}(D)$$

* **Tight End Matchup Index ($M_{\text{TE}}$)**:
  $$M_{\text{TE}}(D) = 0.45 \cdot Z_{\text{PassEPA}}(D) + 0.35 \cdot Z_{\text{TE\_TargetShare}}(D) + 0.20 \cdot Z_{\text{MiddlePassEPA}}(D)$$
  *(Where $Z_{\text{TE\_TargetShare}}$ is opponent-adjusted TE target share allowed and $Z_{\text{MiddlePassEPA}}$ is EPA allowed on passes between the numbers).*

---

## 4. Multi-Horizon Schedule Evaluation Framework

In fantasy analytics, a single "strength of schedule" number is misleading. A fantasy manager in Los Subies faces three distinct strategic horizons:
1. **Immediate Weekly Matchup ($w$)**: Lineup start/sit decisions, weekly waiver claims, and immediate trade evaluation.
2. **Remaining Regular Season ($w \dots 14$)**: Securing a top seed, regular-season survival, and playoff qualification.
3. **Fantasy Playoffs (Weeks $15 \dots 17$)**: Championship equity.

```
+----------------------------------------------------------------------------------------------------+
|                                 LOS SUBIES SCHEDULE HORIZONS                                       |
+------------------------------------+----------------------------------+----------------------------+
| HORIZON 1: WEEKLY MATCHUP          | HORIZON 2: REGULAR SEASON (ROS)  | HORIZON 3: PLAYOFFS        |
+------------------------------------+----------------------------------+----------------------------+
| Scope: Single Week w               | Scope: Weeks w .. 14             | Scope: Weeks 15 .. 17      |
| Objective: Start / Sit Decisions   | Objective: Playoff Qualification | Objective: Championship    |
| Primary User: Immediate Lineups    | Primary User: Bubble Teams       | Primary User: Contenders   |
+------------------------------------+----------------------------------+----------------------------+
```

### 4.1. Decoupling Schedule Difficulty from Player Talent
* **Strict Principle**: Schedule difficulty must **never** be expressed as absolute fantasy points or arbitrary point multipliers.
* A player with 18% target share in a dream matchup is still vastly inferior to a player with 32% target share in a brutal matchup.
* Matchup intelligence is formulated as a **Schedule Difficulty Index ($SDI$)**, normalized on a scale where:
  * **$SDI = 1.00$**: Neutral / League Average matchup.
  * **$SDI > 1.00$**: Favorable / Softer than average (Max $\approx 1.25$).
  * **$SDI < 1.00$**: Unfavorable / Tougher than average (Min $\approx 0.75$).
* **Mapping Function from $Z$-Score to $SDI$**:
  $$SDI_{\text{pos}}(D) = 1.0 + 0.08 \cdot \text{clamp}(M_{\text{pos}}(D), -2.5, 2.5)$$
  *(A +2.0 standard deviation matchup boosts expected efficiency by +16%; a -2.0 matchup depresses it by -16%).*

### 4.2. Schedule Aggregation by Horizon

For player $i$ with position $\text{pos}$ facing opponent schedule $D(w)$ across weeks $w \in \mathcal{W}$:

#### 1. Weekly Matchup Rating
$$\text{Score}_{\text{weekly}}(i, w) = \begin{cases} \text{BYE}, & \text{if team on bye in week } w \\ M_{\text{pos}}(D(w)), & \text{otherwise} \end{cases}$$

#### 2. Remaining Regular Season Schedule Difficulty ($SDI_{\text{reg}}$)
For remaining regular season weeks $\mathcal{W}_{\text{reg}} = \{w \mid \text{current\_week} \le w \le 14\}$:
$$SDI_{\text{reg}}(i) = \frac{\sum_{w \in \mathcal{W}_{\text{reg}}, \text{active}} SDI_{\text{pos}}(D(w))}{N_{\text{games\_active}}}$$
*Bye weeks are excluded from the denominator ($N_{\text{games\_active}}$ counts only active NFL games scheduled in that window). A bye week is treated as 0 availability in weekly lineup projection, but does not corrupt the defensive strength of the remaining games played.*

#### 3. Fantasy Playoff Schedule Difficulty ($SDI_{\text{playoff}}$)
For playoff weeks $\mathcal{W}_{\text{playoff}} = \{15, 16, 17\}$:
$$SDI_{\text{playoff}}(i) = \frac{1}{3} \sum_{w=15}^{17} SDI_{\text{pos}}(D(w))$$
*(Note: Under official NFL scheduling, no NFL teams have bye weeks in Weeks 15–17).*

### 4.3. Team Competitive Context & Trade Evaluator Integration
In Claude's lineup-based trade evaluator, schedule value must adapt to the fantasy manager's league standing:
* **Championship Contender (Top Tier, e.g., 5-1, 6-0)**:
  * Regular season qualification is essentially secured.
  * Schedule horizon weights: $w_{\text{reg}} = 0.25, \quad w_{\text{playoff}} = 0.75$.
  * Contenders aggressively target players with soft matchups in Weeks 15–17 (e.g., targeting WRs facing bottom-5 pass defenses in Weeks 15–17).
* **Bubble / Fringe Playoff Team (e.g., 3-3, 2-4)**:
  * Must survive the regular season to reach the playoffs.
  * Schedule horizon weights: $w_{\text{reg}} = 0.85, \quad w_{\text{playoff}} = 0.15$.
  * Playoff schedule is irrelevant if the team is eliminated by Week 14.
* **Rebuilder / Eliminated Team**:
  * Focuses on future asset accumulation or draft capital.

---

## 5. Backtesting & Validation Methodology

To ensure that the defensive matchup layer adds genuine predictive signal and is not a layer of decorative noise, it must undergo out-of-sample backtesting against established fantasy projection baselines.

```
+----------------------------------------------------------------------------------------------------+
|                                    BACKTESTING ARCHITECTURE                                        |
|                                                                                                    |
|  [ Point-in-Time Data Cutoff (Tues 06:00 ET) ]                                                     |
|                     |                                                                              |
|                     v                                                                              |
|  [ Feature Engineering: Strictly Weeks 1 .. w-1 ]                                                  |
|                     |                                                                              |
|                     v                                                                              |
|  [ Candidate Matchup Layer: SDI_pos(D) ] ---> [ Baseline Model: ESPN Weekly Projections ]          |
|                                         |                                                          |
|                                         v                                                          |
|  [ Combined Regression: ActualPoints ~ ESPN_Proj + SDI_pos ]                                       |
|                                         |                                                          |
|                                         v                                                          |
|  [ Out-of-Sample Evaluation: Delta RMSE, Delta MAE, Delta R^2, Out-of-Fold Cross-Validation ]       |
+----------------------------------------------------------------------------------------------------+
```

### 5.1. Target Variable and Evaluation Metrics
* **Target Variable ($y_{i, w}$)**: Actual fantasy points scored by player $i$ in week $w$ under Los Subies scoring rules.
* **Evaluation Metrics**:
  1. **Root Mean Squared Error (RMSE)** and **Mean Absolute Error (MAE)**.
  2. **Incremental Explained Variance ($\Delta R^2$)**: Measured via nested linear regression models.
  3. **Statistical Significance ($t$-statistic and $p$-value)** of the matchup coefficient $\beta_2$.
  4. **Spearman Rank Correlation ($\rho$)**: Measuring whether player start/sit rankings improve.

### 5.2. Baselines for Comparison
The candidate model must outperform three distinct baselines:
* **Baseline 0 (Naive Trailing Average)**: Rolling 4-week average fantasy points per game (pure player talent/volume baseline, zero matchup context).
* **Baseline 1 (ESPN Weekly Forward Projections)**: The app's existing free baseline (`espn_player_weekly_points.projected_points`).
* **Baseline 2 (Simple Raw FPA)**: Traditional raw Fantasy Points Allowed rank (the standard industry box-score baseline).

### 5.3. Incremental Predictive Value Test
For each position group across historical weeks (expanding window from 2021 to 2025), fit the following nested regression models:

$$\text{Model 1 (Baseline)}: \quad y_{i, w} = \beta_0 + \beta_1 \cdot \text{ESPN\_Proj}_{i, w} + \epsilon_{i, w}$$
$$\text{Model 2 (Candidate)}: \quad y_{i, w} = \beta_0 + \beta_1 \cdot \text{ESPN\_Proj}_{i, w} + \beta_2 \cdot M_{\text{pos}}(D(w)) + \epsilon_{i, w}$$

* **Success Criteria**:
  1. $\beta_2$ must be positive and statistically significant ($p < 0.01$).
  2. $\text{Model 2}$ must achieve an out-of-sample $\Delta R^2 \ge +0.02$ (explaining an additional 2%+ of unexplained variance beyond ESPN).
  3. $\text{Model 2}$ must achieve lower out-of-sample RMSE than a model substituting raw FPA for $M_{\text{pos}}(D(w))$.

### 5.4. Preventing Target Leakage
To ensure complete validity:
* **Point-in-Time Integrity**: When predicting Week $w$, the defensive model must only ingest plays and rosters from Weeks $1 \dots w-1$.
* **No Future Priors**: Bayesian prior distributions must use only historical data from preceding seasons ($t-1$), never parameters fitted across the active season.
* **Separation of Roles**: Player utilization features (snap share, WOPR, designed carry share) are strictly partitioned from defensive matchup features.

---

## 6. Metrics to Reject and Why

To maintain computational efficiency and prevent spurious signals, the following metrics are **explicitly rejected** from the V2 Schedule Intelligence model:

| Rejected Metric | Primary Justification | Statistical / Contractual Reality |
| :--- | :--- | :--- |
| **Raw Fantasy Points Allowed (FPA)** | Confounded by opponent talent and game volume | A defense playing Kansas City, Buffalo, and Detroit will allow massive passing fantasy points regardless of defensive quality. It rewards defenses for playing against weak offenses. |
| **In-Season Routes Run / Route Participation** | **UNAVAILABLE / BLOCKED** | Under nflverse contracts with FTN, in-season participation data is legally locked to postseason releases (`max_season = current - 1`). Free data cannot track routes in-season. |
| **Slot vs. Outside Receiver Matchup Splits** | **UNAVAILABLE / BLOCKED** | Alignment charting is absent in-season in open-source data. PBP pass location (`left/middle/right`) is an unreliable proxy ($r < 0.45$). |
| **Man vs. Zone Coverage Ratings** | **UNAVAILABLE / BLOCKED** | Coverage scheme classification requires proprietary human charting (PFF, SIS, FantasyPoints). No open-source API provides this in-season. |
| **ESPN Pass / Run Block Win Rate (PBWR/RBWR)** | **UNAUTOMATED / UNSTRUCTURED** | ESPN publishes PBWR/RBWR sporadically in journalistic articles. There is no versioned, automated API or parquet feed. |
| **Defensive Explosive Pass Rate (as primary)** | Extreme sample-size instability | Defenses face only ~35 pass attempts per game; allowing 3 vs 5 explosive passes swings rates wildly ($r_{\text{week-to-week}} \approx 0.12$). Dominated by offensive execution. |
| **Defensive Explosive Rush Rate (10+ yds)** | Extreme sample-size instability | Defenses allow only 1.5–2.5 explosive runs per game. Tackle-breaking and blocking scheme dominate this outcome, not defensive consistency. |
| **Raw Sack Count Allowed** | Confounded by opposing QB tendencies | Sacks are $\approx 65\%$ a QB-driven statistic (holding the ball too long, pocket mobility). Raw sacks reflect the opposing QB more than the pass rush. |
| **Unadjusted Last-4-Game Rolling EPA** | Schedule clustering bias | A 4-game sample reflects the 4 specific opponents faced rather than true defensive talent. Autocorrelation to future performance is near zero ($R^2 < 0.05$). |

---

## Deliverables

---

### Deliverable A: Ranked List of Useful Metrics by Position

Below is the definitive, prioritized ranking of candidate metrics by position for the V2 defensive matchup engine. Every metric listed is verified as genuinely available in free data.

```
+---------------------------------------------------------------------------------------------------------------------------------------+
|                                              RANKED METRIC SPECIFICATION TABLE                                                        |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
| POS | RANK | METRIC NAME                       | STATISTICAL SIGNAL | SAMPLE STABILITY       | SOURCE FUNCTION & CONTRACT             |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
| QB  | 1    | Opp-Adjusted Pass EPA Allowed     | Highest (Ceiling)  | Mod (N_0 = 240 dropb)  | nflreadpy.load_pbp()                   |
| QB  | 2    | Passing Success Rate Allowed      | High (Floor)       | High (N_0 = 180 dropb) | nflreadpy.load_pbp()                   |
| QB  | 3    | Defensive Hit-or-Sack Rate        | Moderate           | Mod (N_0 = 200 dropb)  | nflreadpy.load_pbp() [sack | qb_hit]   |
| QB  | 4    | PFR True Pressure Rate            | Moderate           | Moderate (Tue/Wed)     | nflreadpy.load_pfr_advstats("def")     |
| QB  | 5    | QB Scramble Vulnerability         | Conditional (Dual) | Low (Interaction only) | nflreadpy.load_pbp() [qb_scramble]     |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
| RB  | 1    | Rushing Success Rate Allowed      | High (Volume/Sust) | High (N_0 = 160 rush)  | nflreadpy.load_pbp() [designed run]    |
| RB  | 2    | Opp-Adjusted Rush EPA Allowed     | Moderate           | Mod (N_0 = 280 rush)   | nflreadpy.load_pbp() [designed run]    |
| RB  | 3    | Yards Before Contact (YBC) Allowed| High (Front 7)     | Moderate (Tue/Wed)     | nflreadpy.load_pfr_advstats("def")     |
| RB  | 4    | Opp-Adjusted RB Target Share Allwd| High (PPR Floor)   | Mod (Opp-Adjusted)     | load_pbp() + load_rosters_weekly()     |
| RB  | 5    | Defensive Stuff Rate Allowed      | Moderate (Floor)   | High (N_0 = 150 rush)  | nflreadpy.load_pbp() [yards_gained<=0] |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
| WR  | 1    | Opp-Adjusted Pass EPA Allowed     | Highest (General)  | Mod (N_0 = 240 dropb)  | nflreadpy.load_pbp()                   |
| WR  | 2    | Passing Success Rate Allowed      | High (Drive Volume)| High (N_0 = 180 dropb) | nflreadpy.load_pbp()                   |
| WR  | 3    | PFR Defensive Pressure Rate       | Moderate (Pass Vol)| Moderate (Tue/Wed)     | nflreadpy.load_pfr_advstats("def")     |
| WR  | 4    | Explosive Pass Rate Allowed       | Mod-Low (Ceiling)  | Low (Shrunk N_0 = 350) | nflreadpy.load_pbp() [yards>=20]       |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
| TE  | 1    | Opp-Adjusted TE Target Share Allwd| High (Funnel)      | Mod (Opp-Adjusted)     | load_pbp() + load_rosters_weekly()     |
| TE  | 2    | Middle-Field Pass EPA Allowed     | High (Seam/Zone)   | Moderate               | nflreadpy.load_pbp() [loc=='middle']   |
| TE  | 3    | Opp-Adjusted Pass EPA Allowed     | High (General)     | Mod (N_0 = 240 dropb)  | nflreadpy.load_pbp()                   |
| TE  | 4    | PFR Defensive Blitz Rate          | Contextual (Floor) | Moderate (Tue/Wed)     | nflreadpy.load_pfr_advstats("def")     |
+-----+------+-----------------------------------+--------------------+------------------------+----------------------------------------+
```

---

### Deliverable B: Free-Data Availability Table

This table documents every free data source, exact API function, update frequency, licensing, and limitations.

| Dataset / Metric Domain | Exact Function / API | Ingestion Cadence | In-Season Status | Automation & Licensing | Limitations & Null Behavior |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Play-by-Play (PBP)** | `nflreadpy.load_pbp()` | Post-game / Nightly (Tue AM final) | **Verified (In-Season)** | Open source (MIT / CC-BY-4.0). Automated parquet download. | 370+ columns. Nulls handled strictly; 0 is measured zero. |
| **Player Weekly Stats** | `nflreadpy.load_player_stats()` | Weekly (Tue AM) | **Verified (In-Season)** | Open source (MIT). Automated. | Pre-aggregated weekly player summaries. `target_share` verified against team targets. |
| **PFR Advanced Defense** | `nflreadpy.load_pfr_advstats(stat_type="def")` | Weekly (Tue/Wed) | **Verified (In-Season)** | Scraped by nflverse team from PFR; public domain sports data. | Lags live games by 24–48 hours. Unpublished weeks produce `NULL`, never 0. |
| **FTN Charting** | `nflreadpy.load_ftn_charting()` | Weekly (Tue AM) | **Verified (In-Season)** | Open source via FTN agreement. | `n_defense_box` verified. **`read_thrown` numeric codes UNVERIFIED** (issue #325). |
| **Weekly Rosters** | `nflreadpy.load_rosters_weekly()` | Weekly (Wed–Sun) | **Verified (In-Season)** | Open source (MIT). | Maps player positions (`RB`, `WR`, `TE`) to `gsis_id` for target attribution. |
| **Schedules & Odds** | `nflreadpy.load_schedules()` | Weekly / Daily | **Verified (In-Season)** | Open source (MIT). | Verified spread convention: positive `spread_line` = home favored. |
| **Next Gen Stats** | `nflreadpy.load_nextgen_stats()` | Weekly (Tue AM) | **Verified (In-Season)** | Open source (MIT). | Only qualified players get rows. Non-qualified players must remain `NULL`, never 0. |
| **ESPN Weekly Projections** | ESPN Private League API (`kona_player_info`) | Daily / On-Demand | **Verified (In-Season)** | Private league auth (`SWID`, `espn_s2`). Internal API. | Baseline forward points. Complete status verified via `X-Fantasy-Filter-Player-Count`. |
| **Participation / Routes** | `nflreadpy.load_participation()` | Post-Season Only | **BLOCKED IN-SEASON** | Withheld by FTN contract until postseason (`max_season = current - 1`). | **UNAVAILABLE IN-SEASON**. Routes, route participation, TPRR, YPRR cannot be computed. |
| **Coverage Formations** | PFF / SIS / FantasyPoints | None (Paid) | **UNAVAILABLE** | Commercial proprietary APIs. | Man/Zone and Cover shells are strictly paywalled. |

---

### Deliverable C: Recommended MVP Matchup-Strength Methodology

The recommended MVP methodology executes a **4-step regularized pipeline**:

```
[ Step 1: PBP Ingestion ] ---> [ Step 2: Ridge Opponent Adjustment ]
                                              |
                                              v
[ Step 4: Position Index M_pos ] <--- [ Step 3: Empirical Bayes Shrinkage ]
```

1. **Input Data**: Cleaned PBP dropbacks and designed rushes season-to-date, with play EPA Winsorized at $[-2.5, +2.5]$.
2. **Opponent Adjustment via Ridge Regression**:
   * Construct the sparse design matrix of offense-defense interactions.
   * Solve regularized coefficients with $\lambda = 15.0$ to extract $\Delta \text{PassEPA}_D$, $\Delta \text{PassSucc}_D$, $\Delta \text{RushEPA}_D$, and $\Delta \text{RushSucc}_D$.
3. **Empirical Bayes Shrinkage**:
   * Shrink each delta toward the league mean (0.0) using play counts and established stabilization thresholds ($N_0 = 240$ for pass EPA, $N_0 = 180$ for pass success, $N_0 = 280$ for rush EPA, $N_0 = 160$ for rush success).
4. **Composite Position Indices ($Z$-Scores)**:
   * Calculate standardized $Z$-scores across the 32 teams.
   * Apply the position weighting equations defined in Section 3.1 ($M_{\text{QB}}$, $M_{\text{RB}}$, $M_{\text{WR}}$, $M_{\text{TE}}$).

---

### Deliverable D: Regular-Season vs. Playoff Schedule Framework

The Los Subies schedule framework divides the remaining NFL regular season into three independent evaluation layers:

```
                                  LOS SUBIES EVALUATION TIMELINE
Week:      6   7   8   9   10   11   12   13   14        15        16        17
Phase:    [--- REMAINING REGULAR SEASON (w .. 14) ---]    [--- PLAYOFFS (15 .. 17) ---]
Scope:     Survival / Top Seed Seeding                     Championship Bracket
```

1. **Immediate Week Matchup ($w$)**:
   * Expressed as a matchup tier:
     * `FAVORABLE`: $M_{\text{pos}}(D) \ge +0.80$ (Top 8 most generous defenses).
     * `NEUTRAL`: $-0.80 < M_{\text{pos}}(D) < +0.80$ (Middle 16 defenses).
     * `TOUGH`: $M_{\text{pos}}(D) \le -0.80$ (Top 8 most difficult defenses).
     * `INSUFFICIENT_SAMPLE`: If $N_{\text{plays}} < 100$ (Weeks 1–3 prior to stabilization).
2. **Remaining Regular Season ($SDI_{\text{reg}}$)**:
   * Covers weeks $\text{current\_week} \dots 14$.
   * Averages the active-game Schedule Difficulty Indices.
   * Informs regular-season playoff qualification odds for bubble teams.
3. **Fantasy Playoffs ($SDI_{\text{playoff}}$)**:
   * Covers Weeks 15, 16, and 17.
   * Strictly evaluates the 3 championship weeks.
   * Weighting in Trade Evaluator scales dynamically based on manager standing (Contender vs. Bubble).

---

### Deliverable E: Backtesting & Validation Methodology

To validate the model's incremental signal without target leakage:
1. **Expanding Window Setup**:
   * Train on historical seasons (2021–2025).
   * For each season, iterate through weeks $w \in [5, 17]$.
   * Feature calculation for Week $w$ strictly uses data from Weeks $1 \dots w-1$.
2. **Linear Model Hierarchy**:
   * $\text{Model 0}$: $\text{Points} \sim \text{Trailing\_4wk\_PPG}$
   * $\text{Model 1}$: $\text{Points} \sim \text{ESPN\_Projection}$
   * $\text{Model 2}$: $\text{Points} \sim \text{ESPN\_Projection} + \text{Raw\_FPA\_Rank}$
   * $\text{Model 3 (V2 Candidate)}$: $\text{Points} \sim \text{ESPN\_Projection} + M_{\text{pos}}(D)$
3. **Significance & Goodness-of-Fit**:
   * Compute out-of-fold RMSE, MAE, and incremental $R^2$.
   * Perform an $F$-test on the residual sum of squares between Model 1 and Model 3.
   * Confirm that Model 3 achieves lower RMSE than Model 2 (proving superiority over raw FPA).

---

### Deliverable F: Metrics to Reject and Why

A summarized rejection guide for implementation engineers:
1. **Raw Fantasy Points Allowed (FPA)**: REJECT. Dominated by opponent schedule and game script; conflates offensive pace with defensive generosity.
2. **Routes Run / Route Participation % / TPRR / YPRR**: REJECT IN-SEASON. Legally withheld from open source during the active NFL season.
3. **Slot vs. Outside WR Matchup Splits**: REJECT IN-SEASON. Alignment charting is absent in open-source weekly feeds.
4. **Man vs. Zone Coverage Ratings**: REJECT IN-SEASON. Proprietary human charting only; absent from free data.
5. **ESPN PBWR / RBWR**: REJECT. Lacks an automated, versioned weekly API pipeline.
6. **Defensive Explosive Play Rates (as primary metrics)**: REJECT. High week-to-week noise ($r \approx 0.12$); driven primarily by offensive talent.
7. **Raw Defensive Sack Totals**: REJECT. Confounded by QB holding times; replace with PBP hit-or-sack rate and PFR pressure rate.
8. **Unadjusted "Last 4 Games" Splits**: REJECT. Driven by schedule clustering and opponent noise; replace with regularized full-season shrinkage.

---

### Deliverable G: Implementation-Ready Specification for Claude

This specification provides the exact database schemas, mathematical definitions, missing-data handling rules, and confidence tier logic for immediate engineering implementation.

#### G.1. Database Schema Additions (`backend/app/analytics/`)

##### Table: `defense_matchup_weekly_ratings`
Stores weekly opponent-adjusted ratings computed after each NFL week.
```sql
CREATE TABLE defense_matchup_weekly_ratings (
    season INT NOT NULL,
    as_of_week INT NOT NULL,               -- Ratings as of end of this week
    defteam VARCHAR(3) NOT NULL,
    pass_dropbacks_sample INT NOT NULL,
    rush_plays_sample INT NOT NULL,
    
    -- Opponent-adjusted, shrunk deltas
    shrunk_delta_pass_epa FLOAT NOT NULL,
    shrunk_delta_pass_success FLOAT NOT NULL,
    shrunk_delta_rush_epa FLOAT NOT NULL,
    shrunk_delta_rush_success FLOAT NOT NULL,
    
    -- Component standardized Z-scores
    z_pass_epa FLOAT NOT NULL,
    z_pass_success FLOAT NOT NULL,
    z_rush_epa FLOAT NOT NULL,
    z_rush_success FLOAT NOT NULL,
    z_hit_or_sack FLOAT NOT NULL,
    z_ybc_allowed FLOAT,                    -- NULL if PFR unverified for week
    z_rb_target_share FLOAT NOT NULL,
    z_te_target_share FLOAT NOT NULL,
    z_middle_pass_epa FLOAT NOT NULL,
    
    -- Position Composite Indices
    m_qb FLOAT NOT NULL,
    m_rb FLOAT NOT NULL,
    m_wr FLOAT NOT NULL,
    m_te FLOAT NOT NULL,
    
    sample_confidence_tier VARCHAR(12) NOT NULL, -- 'HIGH', 'MEDIUM', 'LOW', 'INSUFFICIENT'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season, as_of_week, defteam)
);
```

##### Table: `player_schedule_difficulty`
Stores derived forward schedule evaluations per player.
```sql
CREATE TABLE player_schedule_difficulty (
    season INT NOT NULL,
    as_of_week INT NOT NULL,
    player_id VARCHAR(32) NOT NULL,        -- gsis_id / espn_id crosswalk
    position VARCHAR(4) NOT NULL,
    
    -- Next matchup
    next_opponent VARCHAR(3),
    next_matchup_is_home BOOLEAN,
    next_matchup_m_index FLOAT,
    next_matchup_sdi FLOAT,
    next_matchup_tier VARCHAR(16),         -- 'FAVORABLE', 'NEUTRAL', 'TOUGH', 'BYE', 'INSUFFICIENT'
    
    -- Multi-Horizon Aggregates
    sdi_regular_season FLOAT,              -- Weeks as_of_week .. 14
    sdi_playoffs FLOAT,                    -- Weeks 15 .. 17
    
    regular_season_tier VARCHAR(16),
    playoffs_tier VARCHAR(16),
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season, as_of_week, player_id)
);
```

#### G.2. Missing-Data & Uncertainty Rules
1. **Early Season Sample Size ($N_{\text{dropbacks}} < 100$ or Weeks 1–3)**:
   * Mark `sample_confidence_tier = 'INSUFFICIENT'`.
   * For `INSUFFICIENT` tiers, set $M_{\text{pos}}(D) = 0.0$ and $SDI_{\text{pos}}(D) = 1.00$.
   * The UI displays `"Matchup Data Neutral (Early Season Sample)"`.
2. **Confidence Tiers**:
   * `HIGH`: $N_{\text{dropbacks}} \ge 240$ ($\approx \text{Week } 7+$). Full model weighting applied.
   * `MEDIUM`: $150 \le N_{\text{dropbacks}} < 240$ ($\approx \text{Weeks } 4\text{--}6$). Apply $0.70$ shrinkage multiplier to composite $Z$-scores.
   * `LOW`: $100 \le N_{\text{dropbacks}} < 150$ ($\approx \text{Weeks } 3\text{--}4$). Apply $0.40$ shrinkage multiplier.
   * `INSUFFICIENT`: $N_{\text{dropbacks}} < 100$ ($\approx \text{Weeks } 1\text{--}2$). Set $M_{\text{pos}} = 0.0$.
3. **PFR Delay / Missing Weeks**:
   * If PFR advanced defensive stats (`ybc_att`, `times_pressured`) are not yet published for the current scoring period, `z_ybc_allowed` and `z_pressure` fall back to PBP equivalents:
     * `z_ybc_allowed` falls back to PBP Stuff Rate (`yards_gained <= 0`, inverted).
     * `z_pressure` falls back to PBP Hit-or-Sack Rate (`sack == 1 OR qb_hit == 1`).
4. **Bye Week Handling**:
   * In individual weekly matchup ratings, an opponent on bye produces `next_matchup_tier = 'BYE'`, $M_{\text{pos}} = \text{NULL}$, $SDI = \text{NULL}$.
   * In multi-week horizon averages ($SDI_{\text{reg}}$ and $SDI_{\text{playoff}}$), bye weeks are omitted from both the numerator and denominator. They do not dilute or penalize defensive schedule strength.

#### G.3. Lineup Trade Evaluator Integration Guide (for Claude)
When evaluating trade offers in Los Subies:
* Do not multiply ESPN forward points directly by $SDI$. Keep ESPN projected points unmodified as the authoritative baseline.
* Expose schedule intelligence as a separate explanatory delta:
  $$\Delta \text{Pts}_{\text{schedule}}(i, w) = \text{ESPN\_Proj}_{i, w} \cdot (SDI_{\text{pos}}(D(w)) - 1.0)$$
* In trade evaluation scoring, incorporate schedule horizon values according to the team's standing:
  $$\text{TradeScheduleScore} = w_{\text{reg}} \cdot SDI_{\text{reg}} + w_{\text{playoff}} \cdot SDI_{\text{playoff}}$$
  * Contenders: $w_{\text{reg}} = 0.25, \quad w_{\text{playoff}} = 0.75$.
  * Bubble Teams: $w_{\text{reg}} = 0.85, \quad w_{\text{playoff}} = 0.15$.

---
*End of Research Document.*
