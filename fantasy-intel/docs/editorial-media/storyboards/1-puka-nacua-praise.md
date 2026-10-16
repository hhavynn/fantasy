# Storyboard: 2026-w06-short-praise-puka-nacua-visual (player_hype)

9 shots over 16.73 s. Plan seed 2750432051, shape 694591993896. Review this (and storyboard.png) before rendering; new media stays `pending` until a person approves it.

| # | time | narration | media | why | crop / motion | transition | overlay | caption |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.00–0.96 | 24 targets. | photo: `puka-camp-2023` (practice_photo, CC-BY-SA-4.0, review pending) · FILE PHOTO 2023 | Puka Nacua full-bleed with the number over him | medium, pull, full_bleed x1.2 | none | stat | low |
| 2 | 0.96–2.90 | Puka Nacua against the Bills. | photo: `puka-fanduel-2024` (interview_still, CC-BY-3.0, review pending) · FILE PHOTO 2024 | a real photo of Puka Nacua as he is named | medium, push, card_over_blur x4.34 | cut |  | low |
| 3 | 2.90–5.79 | The entire Bills offense? 18. | graphic · graphic `hx_vs` | the comparison is a picture (the beat's graphic); continues through '18.' |  | flash |  | low |
| 4 | 5.79–6.76 | 17 catches, | photo: `puka-camp-2023` (practice_photo, CC-BY-SA-4.0, review pending) · FILE PHOTO 2023 | stat run: a new shot on each number (17), the number slams over him | tight, pull, full_bleed x1.74 | cut | stat | low |
| 5 | 6.76–7.96 | 138 yards, | photo: `puka-fanduel-2024` (interview_still, CC-BY-3.0, review pending) · FILE PHOTO 2024 | stat run: a new shot on each number (138), the number slams over him | tight, push, card_over_blur x4.52 | whip | stat | low |
| 6 | 7.96–9.37 | 1 touchdown. | photo: `espn-4426515` (headshot_fallback, espn_override, review approved) · FALLBACK | stat run: a new shot on each number (1), the number slams over him | medium, push, card_over_blur x4.4 | whip | stat | low |
| 7 | 9.37–13.22 | Out of 412 receiver games this season, nobody's seen more. | graphic · graphic `hx_rank` | data tells this part: the beat's graphic; continues through 'nobody's seen more.' |  | cut |  | low |
| 8 | 13.22–14.85 | The Rams lost a shootout. | photo: `rams-runout-2025` (team_photo, CC-BY-3.0, review pending) · FILE PHOTO 2025 | team result over a LA team photo, labelled as a file photo (not this game) | wide, pull, full_bleed x1.78 | flash | score | low |
| 9 | 14.85–16.73 | Nacua did not. | photo: `puka-camp-2023` (practice_photo, CC-BY-SA-4.0, review pending) · FILE PHOTO 2023 | Puka Nacua big, in his LA uniform, as the line praises him | wide, pull, full_bleed x1.1 | flash |  | low |

## Media gaps (wanted vs used)

- shot 1 (s01) "24 targets.": wanted **game action photo of Puka Nacua (LA, current season)**, used photo (puka-camp-2023)
- shot 9 (s05) "Nacua did not.": wanted **game action photo of Puka Nacua (LA, current season)**, used photo (puka-camp-2023)

## Credits

- espn-4426515: ESPN (owner override) | espn_override
- puka-camp-2023: Sewageboy | https://commons.wikimedia.org/wiki/File:Nacua_731.jpg | CC-BY-SA-4.0 (https://creativecommons.org/licenses/by-sa/4.0/)
- puka-fanduel-2024: FanDuel | https://commons.wikimedia.org/wiki/File:Puka_Nacua_FanDuel_Interview.png | CC-BY-3.0 (https://creativecommons.org/licenses/by/3.0/)
- rams-runout-2025: Tennessee Titans | https://commons.wikimedia.org/wiki/File:2025_LA_Rams_at_Tennessee_Titans.png | CC-BY-3.0 (https://creativecommons.org/licenses/by/3.0/)

## Media QC (after render)

- experimental render: **PASS**
- publishable: **NO**

| check | ok | severity | detail |
|---|---|---|---|
| review | NO | warn | shot 1: new media: needs a person to approve it before publishing (puka-camp-2023) |
| crop_face | yes | warn | shot 1: face inside the crop |
| caption_face | yes | warn | shot 1: caption (low) clear of the face |
| review | NO | warn | shot 2: new media: needs a person to approve it before publishing (puka-fanduel-2024) |
| review | NO | warn | shot 4: new media: needs a person to approve it before publishing (puka-camp-2023) |
| crop_face | yes | warn | shot 4: face inside the crop |
| caption_face | yes | warn | shot 4: caption (low) clear of the face |
| review | NO | warn | shot 5: new media: needs a person to approve it before publishing (puka-fanduel-2024) |
| review | NO | warn | shot 8: new media: needs a person to approve it before publishing (rams-runout-2025) |
| review | NO | warn | shot 9: new media: needs a person to approve it before publishing (puka-camp-2023) |
| crop_face | yes | warn | shot 9: face inside the crop |
| caption_face | yes | warn | shot 9: caption (low) clear of the face |
| fallbacks | NO | warn | headshot fallback in shots [6] |
| missing_media | yes | warn | no slates |
| variety_run | yes | warn | longest run of one kind: 3 shots |
| variety_hold | yes | warn | longest shot 3.9 s |
| variety_assets | yes | warn | 4 distinct pictures in 7 picture shots |
| duplication_uses | yes | warn | no photo over-used |
| duplication_framing | yes | warn | no repeats |
| numbers | yes | block | shot 1: overlay '24' vs claim c028 '24' |
| numbers | yes | block | shot 4: overlay '17' vs claim c029 '17' |
| numbers | yes | block | shot 5: overlay '138' vs claim c030 '138' |
| numbers | yes | block | shot 6: overlay '1' vs claim c031 '1' |
| output_size | yes | block | 1080x1920 |
| output_fps | yes | block | 30.00 fps |
| output_duration | yes | block | 16.73 s video vs 16.73 s narration |
| output_audio | yes | block | audio stream present |
| output_black_frames | yes | block | no black frames |

## Blocks publishing

- review: shot 1: new media: needs a person to approve it before publishing (puka-camp-2023)
- review: shot 2: new media: needs a person to approve it before publishing (puka-fanduel-2024)
- review: shot 4: new media: needs a person to approve it before publishing (puka-camp-2023)
- review: shot 5: new media: needs a person to approve it before publishing (puka-fanduel-2024)
- review: shot 8: new media: needs a person to approve it before publishing (rams-runout-2025)
- review: shot 9: new media: needs a person to approve it before publishing (puka-camp-2023)
