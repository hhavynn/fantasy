# Nine-video storyboard audit (Week 6, 2026)

From each episode's `script.json` (`structure`, `shots`, `narration_segments`) and the rendered MP4s. "Mechanism" is how the video opens or ends, regardless of wording.

## As made

| # | Player | Format / storyline | Opening (mechanism) | Scenes | Ending (mechanism) | Length |
|---|---|---|---|---|---|---|
| 1 | Braelon Allen | opportunity | "95% of the backfield?" (number) | giant_number > carry_grid > carry_grid > report > fork | "If Hall sits again... if he's back..." (conditional) | 22.2 s |
| 2 | Mike Evans | dilemma | "Part-time snaps, full-time targets. Do you start...?" (contradiction) | scale > weigh > weigh > tiebreak > tip | "Start him against the Commanders." (verdict) | 24.3 s |
| 3 | Brenton Strange | breakout | "...targets just caught up to his snaps." (number) | radar > lag > lag > lag > share_split > lock | "Get him on your roster." (verdict) | 26.5 s |
| 4 | Dalton Kincaid | template / vanishing_targets | "...still on the field. So why did his targets dry up?" (question) | player_card > target_balls > split > volume > lineup > decision | "Just watch... Riding it out, or benching him?" (buttons) | 24.9 s |
| 5 | Darius Cooper | template / fill_in | "...role just jumped. But there's a catch." (teaser) | player_card > presence > teammate_out ×2 > status_stamp > lineup > decision | "Would you pick up Cooper, or wait?" (buttons) | 25.4 s |
| 6 | Emanuel Wilson | template / fill_in | same as Cooper | same as Cooper | "Would you pick up Wilson, or wait?" (buttons) | 22.6 s |
| 7 | Harold Fannin Jr. | template / vanishing_targets | same as Kincaid | player_card > split > volume > lineup > decision | same as Kincaid | 20.6 s |
| 8 | Oronde Gadsden II | mystery | "Same snaps both weeks. 8 targets, then 1." (contradiction) | mirror > which > rival > zoom_out > zoom_out | "Don't chase the big week." (verdict) | 22.0 s |
| 9 | Travis Kelce | template / vanishing_targets | same as Kincaid | same as Kincaid | same as Kincaid | 22.0 s |

**Repeated groups,** measured with `creative.shared` on `creative.from_script`, which rebuilds the scene sequence and ending from the shots of scripts that predate the fingerprint fields:
- **Kincaid and Kelce:** share all 5 dimensions (show, opening mechanism, scenes, ending, beats).
- **Cooper and Wilson:** share all 5.
- **Fannin and each of Kincaid and Kelce:** share 3 of 5 (show, opening, ending; his scenes and beats differ by one beat).
- **Evans, Strange and Gadsden:** share only an ending mechanism (a verdict) with each other.

## Under the batch editor

| Player | Show | Why (from `shows.md`) |
|---|---|---|
| Braelon Allen | contingency (kept) | a vacated backfield and a dated status: a two-way call |
| Emanuel Wilson | **heist** | Jadarian Price is on injured reserve: a window of more than one week |
| Oronde Gadsden II | detective (kept) | same playing time, very different targets in comparable games |
| Dalton Kincaid | **red_flag** | target share fell in every recent game while his snaps held; air-yard share fell in every recent game too; a 12-point drop |
| Harold Fannin Jr. | **myth** | snaps up and the team throwing more in every recent game, yet his share fell |
| Travis Kelce | **deferred** | red_flag: already this week's red_flag (Dalton Kincaid). Revisit next week if the trend holds. |
| Darius Cooper | **deferred** | heist: already this week's heist (Emanuel Wilson); his own window depends on this week's practice report |
| Mike Evans, Brenton Strange | kept (dilemma, breakout) | their formats are in uncommitted work; nothing here changes them |

## The three redesigned previews

### Dalton Kincaid: Red Flag Report (rendered)

| | Before | After |
|---|---|---|
| Opens | "Dalton Kincaid is still on the field. So why did his targets dry up?" | "There's a red flag in Dalton Kincaid's usage, and it isn't his playing time." |
| Body | per-game targets as footballs; target share vs snaps bars; team volume; "the one number to watch" | his target share game by game against his own 3-game baseline; his snaps game by game beneath; the light Week 5 (18 team targets) marked as the thing in his favor |
| Ends | "Riding it out, or benching him?" + two buttons | "2 games is a warning, not a verdict. No rebound toward 21% this week? Move on." (warning) |
| Visual | six different scenes | one chart that evolves through four stages |
| Shared dimensions with before | | 0 of 5 (at most 1 with any of the nine) |

### Emanuel Wilson: Waiver Wire Heist (rendered)

| | Before | After |
|---|---|---|
| Opens | "Emanuel Wilson's role just jumped. But there's a catch." | "Jadarian Price is on injured reserve. Look who's getting his carries." (problem first: the player comes second) |
| Body | snap-share dots; Price's weeks out; Price's earlier role; the IR stamp; a lineup | his share of the Seahawks' running back carries filling a meter (39% to 79%); Price's 38% before he got hurt; the catch: 60% of the snaps |
| Ends | "Would you pick up Wilson, or wait?" + two buttons | "With Price on injured reserve, this isn't a one-week fill-in. If Wilson's on your waiver wire, claim him." (verdict) |
| Visual | template scenes | an alert, a 0-100% meter, the old split, his card dropping into a roster slot |
| Shared dimensions with before | | 0 of 5 (at most 1 with any of the nine) |

### Harold Fannin Jr.: Myth vs Reality (previewed)

| | Before | After |
|---|---|---|
| Opens | "Harold Fannin Jr. is still on the field. So why did his targets dry up?" | "More snaps should mean more targets, right? Harold Fannin says otherwise." |
| Body | a snaps-up bar, a share-down bar, a lineup | three reality tiles: 91% of the snaps (up from 85%); the Browns' 33 and 32 targets, their busiest games of the year; his share 21% to 14% |
| Ends | "Riding it out, or benching him?" | "Snaps get you on the field. Targets get you in the lineup. Don't start him on snaps alone." (takeaway) |
| Visual | template scenes | no chart: stacked stat tiles, then the rule |
| Shared dimensions with before | | 0 of 5 (at most 1 with any of the nine) |

## Scene types worth keeping as primitives

These are reusable, data-driven, and already in `vector_formats.py` / `format_scenes.py`:
- **Carry grid** (one square per carry): any "who got the touches" split.
- **Mirror** (two games side by side): any single-game contradiction.
- **Zoom out** (the whole season after the two games everyone saw): any outlier check.
- **Fork** (the call both ways): any status-dependent decision.
- **Report** (a dated status card): any injury-driven story.

The new shows reuse `identity`, `figure`, `card`, `chip` and `inline` from that module rather than adding parallel components.
