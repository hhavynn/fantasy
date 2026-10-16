# Storyboard: 2026-w06-short-braelon-allen (injury_opportunity)

8 shots over 16.06 s. Plan seed 3345515411, shape 28f75103332a. Review this (and storyboard.png) before rendering; new media stays `pending` until a person approves it.

| # | time | narration | media | why | crop / motion | transition | overlay | caption |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.00–1.82 | 95% of the backfield? | graphic · graphic `giant_number` | the number is the hook: the beat's graphic |  | none |  | low |
| 2 | 1.82–2.44 | That's Braelon Allen, | photo: `espn-4685247` (headshot_fallback, espn_override, review approved) · FALLBACK | FALLBACK: no cleared photo of Braelon Allen in a NYJ uniform; the owner-approved ESPN headshot, labelled | medium, drift_left, card_over_blur x4.4 | cut |  | low |
| 3 | 2.44–3.67 | with Breece Hall out. | photo: `hall-vs-bills-2023` (action_photo, PD, review pending) · FILE PHOTO 2023 | a verified photo of Breece Hall (player id matches the teammate in the facts) | medium, drift, card_over_blur x3.81 | dissolve |  | low |
| 4 | 3.67–4.36 | With Hall playing? | photo: `hall-jets-2026` (interview_still, CC-BY-4.0, review pending) | a verified photo of Breece Hall (player id matches the teammate in the facts) | medium, drift_left, full_bleed x1.34 | cut |  | low |
| 5 | 4.36–6.59 | Allen got 27% of the carries. | graphic · graphic `carry_grid` | data tells this part: the beat's graphic |  | dissolve |  | low |
| 6 | 6.59–9.09 | Hall's missed Weeks 4 and 5 with a quadricep injury, | graphic · graphic `report` | the availability report on screen: the source, not generic injury footage |  | dissolve |  | low |
| 7 | 9.09–11.60 | and as of Oct 15, he was not practicing. | inset: `hall-vs-bills-2023` (action_photo, PD, review pending) · FILE PHOTO 2023 · graphic `report` (inset) | the report stays on screen as a card over Breece Hall (identification, not the injury) | tight, drift_left, card_over_blur x5.33 | cut |  | low |
| 8 | 11.60–16.06 | If Hall sits again, Allen's an easy start. If he's back, expect the old split. | graphic · graphic `fork` | the decision is the graphic; continues through 'Allen's an easy start. If he's back, expect the old split.' |  | dissolve |  | low |

## Media gaps (wanted vs used)

- shot 2 (s02) "That's Braelon Allen,": wanted **action photo of Braelon Allen (NYJ, current season)**, used photo (espn-4685247)

## Credits

- espn-4685247: ESPN (owner override) | espn_override
- hall-jets-2026: New York Jets | https://commons.wikimedia.org/wiki/File:2026_NYJ_Media_-_Breece_Hall.png | CC-BY-4.0 (https://creativecommons.org/licenses/by/4.0/)
- hall-vs-bills-2023: Jim Greenhill | https://commons.wikimedia.org/wiki/File:Breece_Hall_vs_Bills_2023.jpg | PD

## Media QC (after render)

- experimental render: **PASS**
- publishable: **NO**

| check | ok | severity | detail |
|---|---|---|---|
| review | NO | warn | shot 3: new media: needs a person to approve it before publishing (hall-vs-bills-2023) |
| review | NO | warn | shot 4: new media: needs a person to approve it before publishing (hall-jets-2026) |
| crop_face | yes | warn | shot 4: face inside the crop |
| caption_face | yes | warn | shot 4: caption (low) clear of the face |
| review | NO | warn | shot 7: new media: needs a person to approve it before publishing (hall-vs-bills-2023) |
| fallbacks | NO | warn | headshot fallback in shots [2] |
| missing_media | yes | warn | no slates |
| variety_run | yes | warn | longest run of one kind: 3 shots |
| variety_hold | yes | warn | longest shot 4.5 s |
| variety_assets | yes | warn | 3 distinct pictures in 4 picture shots |
| duplication_uses | yes | warn | no photo over-used |
| duplication_framing | yes | warn | no repeats |
| output_size | yes | block | 1080x1920 |
| output_fps | yes | block | 30.00 fps |
| output_duration | yes | block | 16.07 s video vs 16.06 s narration |
| output_audio | yes | block | audio stream present |
| output_black_frames | yes | block | no black frames |

## Blocks publishing

- review: shot 3: new media: needs a person to approve it before publishing (hall-vs-bills-2023)
- review: shot 4: new media: needs a person to approve it before publishing (hall-jets-2026)
- review: shot 7: new media: needs a person to approve it before publishing (hall-vs-bills-2023)
